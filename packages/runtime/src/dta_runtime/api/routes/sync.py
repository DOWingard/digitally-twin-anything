"""Keeping a live twin in step with its sources: sync runs, the rows they set aside, and
what the twin knows about its sources."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from dta_core.log import Log
from dta_core.schema import UID_MAX_LENGTH
from dta_core.store import LIVE_BRANCH, VersionRef
from fastapi import APIRouter, Body, Header, Path, Query
from starlette.responses import Response

from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..formats import json_response
from ..live import source_document
from ..models import JobAccepted, Page, RejectsOut, SourceList, SyncRunOne, SyncRunRequest
from ..paging import next_cursor, start
from ..params import JSON_LIMIT_DEFAULT, JSON_LIMIT_MAX, Branch, Cursor
from ..problems import ApiError
from ..rejectfile import locate
from ..runs import list_runs, position_of, read_run
from ..services import Services
from ..syncing import SyncParams, check_params
from ..tables import decode_cursor, encode_cursor, fingerprint, version_json

router = APIRouter(prefix=f"{API_PREFIX}/sync", tags=["sync"])
log = Log("api.sync")

# The values of `TwinRunStatus` and `TwinRunKind` in the meta-model.
RunStatus = Literal["running", "succeeded", "partial", "failed"]
RunKind = Literal["backfill", "incremental", "replay"]

REJECTS_LIMIT_DEFAULT = 500
REJECTS_LIMIT_MAX = 5_000
# The width of a window of runs per run asked for, when the manifest sets no interval.
FALLBACK_INTERVAL = timedelta(minutes=15)
JOBS_PATH = f"{API_PREFIX}/lifecycle/jobs"

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Names the run asked for, 1 to 200 printable ASCII characters. Asking "
        "again with the same key gives back the job made for it; with other parameters the "
        "request is refused (`sync.key_reused`). The last 1000 keys are remembered",
        min_length=1,
        max_length=200,
        pattern=r"^[\x20-\x7e]+$",
        examples=["sync:2026-01-19T00:15:00+00:00"],
    ),
]
RunUid = Annotated[
    str,
    Path(
        description="Full uid of the run",
        max_length=UID_MAX_LENGTH,
        examples=["twin_sync_run:20260119T001500Z-1a2b3c4d"],
    ),
]
_EMPTY_REJECTS: dict[str, Any] = {"data": [], "next_cursor": None, "total": 0, "skipped": 0}


@router.post(
    "/runs",
    status_code=202,
    summary="Start a sync run",
    description="Starts one run of the sync pipeline as a job and answers at once, with the "
    "job and its address in `Location`; follow it at `/lifecycle/jobs/{id}`. Runs never "
    "overlap. While a sync job is queued or running, asking for the same run (the same "
    "`sources` and `replay`) gives back that job with 200, and asking for another is refused "
    "with 409 `sync.busy`, the problem document carrying the job in progress. The run reads "
    "the manifest and the clock in force when it starts, and takes the intervals that have "
    "ended by then.",
    response_model=JobAccepted,
    responses={
        200: {
            "model": JobAccepted,
            "description": "The job already made for this key or for this same run",
            "headers": {"Location": {"description": "Address of the job"}},
        },
        202: {
            "description": "A job was made for the run",
            "headers": {"Location": {"description": "Address of the job"}},
        },
        **errors(
            400,
            409,
            notes={
                400: "The body or the key is malformed, or names a source or dataset the "
                "manifest does not have (`sync.unknown_source`, `sync.replay_unknown`)",
                409: "The twin is not live (`twin.not_live`), the key was used with other "
                "parameters (`sync.key_reused`), or another run is in progress (`sync.busy`)",
            },
        ),
    },
)
def start_run(
    services: ServicesDep,
    body: Annotated[SyncRunRequest | None, Body()] = None,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    version = services.resolve("head", LIVE_BRANCH)
    state = services.head_state(version)
    if not state.live:
        raise ApiError(
            "the twin is not live: its manifest sets no sync interval", code="twin.not_live"
        )
    replay = (body.replay.source, body.replay.dataset) if body and body.replay else None
    params = SyncParams.of(body.sources if body else None, replay)
    check_params(state.manifest, params)
    document, created = services.sync_jobs.start(params, idempotency_key)
    return json_response(
        {"job": document},
        headers={"Location": f"{JOBS_PATH}/{document['id']}"},
        status=202 if created else 200,
    )


@router.get(
    "/runs",
    summary="The twin's sync runs, newest first",
    description="Each run's record (`TwinSyncRun`) as of the head: its stored values and its "
    "uid, newest first by `started_at`, runs that started together by uid. Paged with "
    "`cursor`, which keeps reading the version of the first page.",
    response_model=Page,
    responses=versioned(400, 404),
)
def runs(
    services: ServicesDep,
    status: Annotated[RunStatus | None, Query(description="Only runs with this status")] = None,
    kind: Annotated[RunKind | None, Query(description="Only runs of this kind")] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=JSON_LIMIT_MAX,
            description=f"Most runs returned, at most {JSON_LIMIT_MAX}",
        ),
    ] = JSON_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("sync_runs", status, kind)
    version, position = start(services, cursor, "sync_runs", query, "head", LIVE_BRANCH)
    state = services.head_state(version)
    filters = [(name, "=", value) for name, value in (("status", status), ("kind", kind)) if value]
    page = list_runs(
        services.store,
        state.ontology,
        version,
        filters=filters,
        limit=limit,
        position=position_of(position),
        span=(state.interval or FALLBACK_INTERVAL) * (limit + 1),
        floor=state.first_sync_t,
    )
    return json_response(
        {
            "data": page.data,
            "next_cursor": next_cursor("sync_runs", query, version, page.next_position),
            "version": version_json(version),
        },
        version,
    )


def _run_or_404(services: Services, uid: str) -> tuple[dict[str, Any], VersionRef]:
    version = services.resolve("head", LIVE_BRANCH)
    found = read_run(services.store, services.ontology(version), version, uid)
    if found is None:
        raise ApiError(
            f"no sync run {uid!r} is recorded at version #{version.seq} of {version.branch}",
            code="sync.run_not_found",
        )
    return found, version


@router.get(
    "/runs/{uid}",
    summary="One sync run's record",
    response_model=SyncRunOne,
    responses=versioned(400, 404, notes={404: "No run with this uid is recorded"}),
)
def one_run(services: ServicesDep, uid: RunUid) -> Response:
    found, version = _run_or_404(services, uid)
    return json_response({"data": found, "version": version_json(version)}, version)


@router.get(
    "/runs/{uid}/rejects",
    summary="The rows a sync run set aside",
    description="Read from the file the run's record names, in file order: which row, which "
    "rule it broke and why, never the row's values. `total` counts the lines that pass the "
    "filters; lines that are not JSON objects are skipped and counted in `skipped`. A run "
    "that set nothing aside answers with no lines.",
    response_model=RejectsOut,
    responses=versioned(
        400,
        404,
        notes={
            404: "No run with this uid is recorded (`sync.run_not_found`), or the file its "
            "record names is missing, unreadable or outside the artifacts directory "
            "(`sync.rejects_missing`)"
        },
    ),
)
def run_rejects(
    services: ServicesDep,
    uid: RunUid,
    rule: Annotated[
        str | None,
        Query(
            description="Only rows set aside for this rule",
            examples=["type"],
            max_length=100,
        ),
    ] = None,
    mapping: Annotated[
        str | None,
        Query(
            description="Only rows of this mapping, written `<source>.<mapping>`",
            examples=["telemetry.levels"],
            max_length=400,
        ),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=REJECTS_LIMIT_MAX, description="Most lines returned")
    ] = REJECTS_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("rejects", uid, rule, mapping)
    position = decode_cursor(cursor, "rejects", query).get("p") if cursor else None
    found, version = _run_or_404(services, uid)
    location = found.get("rejects_location")
    if not location:
        return json_response(_EMPTY_REJECTS, version)
    refused = ApiError("the rows this run set aside cannot be read", code="sync.rejects_missing")
    path = locate(services.settings.artifacts_dir, str(location))
    if path is None:
        log.warn(
            "a run's record names a rejects file outside the artifacts directory; refused",
            run=uid,
        )
        raise refused
    try:
        page = services.rejects.page(
            path, rule=rule, mapping=mapping, limit=limit, position=position
        )
    except OSError as exc:
        log.warn(
            "the rejects file of a run is missing or cannot be read",
            run=uid,
            location=str(location),
            error_type=type(exc).__name__,
        )
        raise refused from exc
    following = None
    if page.next_position is not None:
        following = encode_cursor({"k": "rejects", "q": query, "p": page.next_position})
    return json_response(
        {
            "data": page.lines,
            "next_cursor": following,
            "total": page.total,
            "skipped": page.skipped,
        },
        version,
    )


@router.get(
    "/sources",
    summary="What the twin knows about its sources",
    description="Each source of the manifest at the head, in the manifest's order, with what "
    "syncing recorded about it: status, last error, watermark (latest data time taken), last "
    "successful read, freshness on the twin's clock, its datasets and how far each was read, "
    "and its mappings with their kind (`static` without a valid time, `event` for an event "
    "type, else `state`). A source the manifest no longer names is listed last, disabled. "
    "Where a source lives is never shown.",
    response_model=SourceList,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(400, 404),
    },
)
def sources(services: ServicesDep, branch: Branch = LIVE_BRANCH) -> Response:
    version = services.resolve("head", branch)
    state = services.head_state(version)
    reading = services.clocks.read(state.manifest, state.manifest_hash)
    return json_response(
        {
            "data": [source_document(source, reading.now) for source in state.sources],
            "clock": reading.document(),
            "version": version_json(version),
        },
        version,
    )
