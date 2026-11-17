"""The running stack: the log lines of its services and the jobs of the API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Path, Query
from starlette.responses import Response

from ...filters import repair_plus
from .. import registry
from ..deps import ServicesDep
from ..docs import API_PREFIX, errors, versioned
from ..formats import json_response
from ..jobs import KEEP
from ..logfiles import LogQuery, read_logs
from ..models import JobList, JobOne, JobState, LogsOut, Registered, RegisterRequest
from ..params import split_list
from ..problems import ApiError
from ..tables import version_json

router = APIRouter(prefix=f"{API_PREFIX}/lifecycle", tags=["lifecycle"])

LOGS_LIMIT_DEFAULT = 200
LOGS_LIMIT_MAX = 5_000
JOBS_LIMIT_DEFAULT = 50


@router.post(
    "/register",
    summary="Bring the twin's functions, models and questions in line with its manifest",
    description="The manifest declares what the twin computes; what it runs is what its "
    "graph records. This writes, in one version (cause `registration`), a `TwinFunction` "
    "for each function of the manifest, a `TwinModel` for each candidate of each model "
    "specification and a `TwinQuestion` for each question, and nothing when nothing "
    "differs: asking again changes nothing. A function the manifest no longer has is "
    "`disabled`, a candidate it no longer has `retired`; records made while the twin ran "
    "are left alone. A candidate whose family, code, settings, target or features changed "
    "is a `candidate` again without its fitted state. A new question is `provisional` "
    "until it is measured. `code` is the SHA-256 of the packed code the manifest's "
    "functions and families of its own are found in, as `POST /artifacts` kept it.",
    response_model=Registered,
    responses=versioned(
        400,
        404,
        409,
        notes={
            404: "No artifact is kept under the hash given (`artifact.not_found`)",
            409: "The manifest names code of the twin's own and no `code` was given "
            "(`register.code_missing`)",
        },
    ),
)
def register(
    services: ServicesDep, body: Annotated[RegisterRequest | None, Body()] = None
) -> Response:
    found = registry.register(services, body.code if body else None)
    version = found["version"]
    return json_response({**found, "version": version_json(version)}, version)


@router.get(
    "/jobs",
    summary="The API's background jobs, newest first",
    description=f"Jobs live in the memory of the API process; the last {KEEP} are kept, the "
    "oldest finished ones dropped first. A sync run's durable record is its `TwinSyncRun` "
    "(`/sync/runs`), not its job.",
    response_model=JobList,
    responses=errors(400),
)
async def jobs(
    services: ServicesDep,
    kind: Annotated[
        str | None, Query(description="Only jobs of this kind", examples=["sync"], max_length=100)
    ] = None,
    state: Annotated[JobState | None, Query(description="Only jobs in this state")] = None,
    lane: Annotated[
        Literal["sync", "compute"] | None, Query(description="Only jobs of this lane")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=KEEP, description="Most jobs returned")
    ] = JOBS_LIMIT_DEFAULT,
) -> Response:
    # A coroutine: it reads memory only, and needs no worker thread.
    return json_response(
        {"data": services.jobs.list(kind=kind, state=state, lane=lane, limit=limit)}
    )


@router.get(
    "/jobs/{id}",
    summary="One background job",
    response_model=JobOne,
    responses=errors(400, 404, notes={404: "No job with this id is kept (`job.not_found`)"}),
)
async def job(
    services: ServicesDep,
    id: Annotated[str, Path(description="The job's id", max_length=64, examples=["9f2c" * 8])],
) -> Response:
    found = services.jobs.get(id)
    if found is None:
        raise ApiError(f"no job {id!r} is kept by this API process", code="job.not_found")
    return json_response({"data": found})


@router.get(
    "/logs",
    summary="Structured log lines of the stack's services, newest first",
    description="Read from `<logs dir>/<service>.jsonl`. Each line keeps its fields "
    "(timestamp, level, context, message, trace_id, error, stack, data) and gains `service`. "
    "Lines that are not JSON log records are skipped and counted.",
    response_model=LogsOut,
    responses=errors(400),
)
def logs(
    services: ServicesDep,
    level: Annotated[
        Literal["debug", "info", "warn", "error"] | None,
        Query(description="Lines at this level or a more severe one"),
    ] = None,
    since: Annotated[
        str | None,
        Query(
            description="Lines at or after this ISO 8601 time with an offset",
            examples=["2026-01-01T00:00:00Z"],
            max_length=100,
        ),
    ] = None,
    context: Annotated[
        str | None,
        Query(description="Lines whose context starts with this", max_length=200),
    ] = None,
    trace_id: Annotated[str | None, Query(description="Lines of this trace", max_length=64)] = None,
    service: Annotated[
        list[str] | None,
        Query(description="Services (log file names without .jsonl), repeated or comma-separated"),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=LOGS_LIMIT_MAX, description="Most lines returned")
    ] = LOGS_LIMIT_DEFAULT,
) -> Response:
    moment = None
    if since is not None:
        try:
            moment = datetime.fromisoformat(repair_plus(since))
        except ValueError as exc:
            raise ApiError(
                f"since is not an ISO 8601 time: {since!r}", code="api.bad_request"
            ) from exc
        if moment.tzinfo is None:
            raise ApiError("since needs an offset, such as Z or +01:00", code="api.bad_request")
    query = LogQuery(
        level=level,
        since=moment,
        context=context,
        trace_id=trace_id,
        services=split_list(service),
        limit=limit,
    )
    found, skipped = read_logs(services.settings.logs_dir, query)
    return json_response({"data": found, "skipped": skipped})
