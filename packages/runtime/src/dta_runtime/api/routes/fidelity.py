"""Whether the twin keeps its promises: its fidelity state, the checks that measure it,
the incidents and suspect signals they raise, and the steps an operator takes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.store import LIVE_BRANCH, VersionRef
from fastapi import APIRouter, Body, Path, Query, Request
from starlette.responses import Response

from ... import fidelity
from ...fidelity.measures import in_window
from ...fidelity.records import CHECK_SCOPE
from ...filters import repair_plus
from ..deps import ServicesDep
from ..docs import API_PREFIX, errors, tabular, versioned
from ..fidelity_work import CHECK_KIND, by_hand, check_time, check_work, emit
from ..formats import json_response, negotiate
from ..listing import JOB_RESPONSES, LIST_LIMIT_DEFAULT, ComputeKey, ListLimit, start_job
from ..models import (
    CorrectionAsked,
    FidelityCheckList,
    FidelityCheckOne,
    FidelityOut,
    IncidentFilter,
    IncidentKind,
    IncidentList,
    IncidentOne,
    IncidentStepped,
    JobAccepted,
    OperatorStep,
    Page,
    SignalCleared,
    SignalList,
    SignalStatus,
)
from ..paging import next_cursor, offset_of, paged_response, start
from ..params import At, Cursor, FormatParam, Limit, page_limit
from ..problems import ApiError
from ..services import Services
from ..tables import fingerprint, iso, version_json

router = APIRouter(prefix=f"{API_PREFIX}/fidelity", tags=["fidelity"])
log = Log("api.fidelity")

UNSUPPORTED_NOTE = (
    "The twin's ontology lacks the fidelity records (`fidelity.unsupported`): it was "
    "installed before they existed"
)
CheckUid = Annotated[
    str,
    Path(
        description="Uid of the check",
        max_length=200,
        examples=["twin_fidelity_check:20260119T001500Z"],
    ),
]
IncidentUid = Annotated[
    str,
    Path(
        description="Uid of the incident",
        max_length=200,
        examples=["twin_incident:breach-level_ahead-20260119T001500Z"],
    ),
]
SignalUid = Annotated[
    str,
    Path(description="Uid of the signal", max_length=400, examples=["twin_signal:Gauge.level/g-1"]),
]
QuestionId = Annotated[
    str, Path(description="Id of the question", max_length=190, examples=["level_ahead"])
]
Since = Annotated[
    str | None,
    Query(
        alias="from",
        description="Only what was measured at or after this ISO 8601 time with an offset",
        examples=["2026-01-19T00:00:00Z"],
        max_length=100,
    ),
]
Until = Annotated[
    str | None,
    Query(
        alias="to",
        description="Only what was measured at or before this ISO 8601 time with an offset",
        examples=["2026-01-20T00:00:00Z"],
        max_length=100,
    ),
]

ERRORS_SCHEMA = pa.schema(
    [
        ("origin", pa.timestamp("us", tz="UTC")),
        ("scored_at", pa.timestamp("us", tz="UTC")),
        ("error", pa.float64()),
        ("n", pa.int64()),
        ("model", pa.string()),
        ("in_window", pa.bool_()),
        ("check", pa.string()),
    ]
)


def _time(text: str | None, name: str) -> datetime | None:
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(repair_plus(text))
    except ValueError as exc:
        raise ApiError(f"{name} is not an ISO 8601 time: {text!r}", code="api.bad_request") from exc
    if moment.tzinfo is None:
        raise ApiError(f"{name} needs an offset, such as Z or +01:00", code="api.bad_request")
    return moment


def _check_at(uid: str | None) -> datetime | None:
    """The time a check's uid is stamped with."""
    if not uid or not uid.startswith(f"{CHECK_SCOPE}:"):
        return None
    try:
        return datetime.fromisoformat(uid.split(":", 1)[1])
    except ValueError:
        return None


def check_ref(uid: str | None) -> dict[str, Any] | None:
    return None if uid is None else {"uid": uid, "at": _check_at(uid)}


def _brief_incident(document: dict[str, Any]) -> dict[str, Any]:
    return {name: document.get(name) for name in ("uid", "kind", "subject", "status", "rung")}


def _brief_signal(document: dict[str, Any]) -> dict[str, Any]:
    return {name: document.get(name) for name in ("uid", "property", "object", "reason", "since")}


def _question_entry(qid: str, entry: dict[str, Any]) -> dict[str, Any]:
    window = entry.get("window") or {}
    warning = entry.get("warning") or {}
    return {
        "id": qid,
        "model": entry.get("model"),
        "origin": entry.get("origin"),
        "scored": bool(entry.get("scored")),
        "error": entry.get("error"),
        "n": entry.get("n"),
        "window_error": window.get("error"),
        "window_n": window.get("n"),
        "tolerance": entry.get("tolerance"),
        "metric": entry.get("metric"),
        "breach": bool(window.get("breach")),
        "warning_sum": warning.get("sum"),
        "reason": entry.get("reason"),
    }


def _live(services: Services, at: str) -> VersionRef:
    return services.resolve(at, LIVE_BRANCH)


def overview(services: Services, version: VersionRef) -> dict[str, Any]:
    """The fidelity document at a version: the state record, the record of the check it
    names, the incidents not resolved and the suspect signals; nothing is measured."""
    state = fidelity.state(services.twin, version)
    record = None
    moment = _check_at(state["check"])
    if moment is not None:
        found = fidelity.checks(services.twin, at=version, since=moment, until=moment, limit=1)
        record = found[0] if found and found[0]["uid"] == state["check"] else None
    questions = (record or {}).get("questions") or {}
    return {
        "state": state["state"],
        "since": state["since"],
        "check": None if record is None else {"uid": record["uid"], "at": record["at"]},
        "questions": [_question_entry(qid, entry) for qid, entry in sorted(questions.items())],
        "freshness": (record or {}).get("freshness") or {},
        "coverage": (record or {}).get("coverage") or {},
        "incidents": [
            _brief_incident(found)
            for found in fidelity.incidents(services.twin, at=version, status="unresolved")
        ],
        "suspect_signals": [
            _brief_signal(found)
            for found in fidelity.signals(services.twin, at=version, status="suspect")
        ],
        "version": version_json(version),
    }


@router.get(
    "",
    summary="The twin's fidelity: its state and what the last check found",
    description="`state` is `unknown` before a check has completed, `healthy`, `correcting` "
    "while an incident is open or waits for a re-model, or `failed` while one has failed; "
    "`since` is the data time it began. Per predictive question, what the last check "
    "measured: the origin's error, the mean error over the window and how many origins it "
    "rests on, the tolerance ε and its metric, whether the window is in breach, the drift "
    "warning's sum, and why nothing was scored when nothing was. Then the freshness and "
    "coverage the check measured, the incidents not resolved and the suspect signals. Read "
    "from the records as they are; nothing is measured.",
    response_model=FidelityOut,
    responses=versioned(400, 404),
)
def fidelity_overview(services: ServicesDep, at: At = "head") -> Response:
    version = _live(services, at)
    return json_response(overview(services, version), version)


@router.post(
    "/checks",
    status_code=202,
    summary="Check the twin's fidelity now",
    description="Starts a check as a job of the compute lane and answers at once; follow it "
    "at `/lifecycle/jobs/{id}`. The check is for the newest multiple of the fidelity "
    "interval at or before the twin's clock. It predicts each predictive question's origin "
    "through the worker from the version at the origin (as `POST /models/{uid}/predict` "
    "does, with the 80 % interval), scores it against what was then observed, measures "
    "each question's freshness and tests the observed signals; it opens, steps and resolves "
    "incidents, and writes it all as one version at the check's time (cause `derived`). "
    "What it decided goes to the twin's notifications, and each correction it asks for "
    "starts as a job of its own (kind `correction`); a correction still pending on an "
    "incident from before starts again. A check of a time that has its record answers with "
    "it and does nothing. The scheduler sends the key `fidelity:<boundary>`.",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            409,
            notes={
                409: "The twin is not live or sets no fidelity interval and window "
                "(`fidelity.not_live`), or the key was used for another request "
                "(`job.key_reused`)",
                503: "The twin's clock cannot be read now (`clock.unavailable`), or the store "
                "cannot be reached",
            },
        ),
    },
)
def start_check(services: ServicesDep, idempotency_key: ComputeKey = None) -> Response:
    version = _live(services, "head")
    at = check_time(services, services.head_state(version))
    return start_job(
        services, CHECK_KIND, {"at": iso(at)}, check_work(services, at), idempotency_key
    )


@router.get(
    "/checks",
    summary="The fidelity checks, newest first",
    description="Each check's record: its time, the head it measured, the state it left, "
    "and per question, freshness, signal and coverage what it found.",
    response_model=FidelityCheckList,
    responses=versioned(400, 404),
)
def list_checks(
    services: ServicesDep,
    since: Since = None,
    until: Until = None,
    at: At = "head",
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    lower, upper = _time(since, "from"), _time(until, "to")
    query = fingerprint("fidelity-checks", since, until)
    version, position = start(services, cursor, "fidelity-checks", query, at, LIVE_BRANCH)
    offset = offset_of(position)
    found = fidelity.checks(
        services.twin, at=version, since=lower, until=upper, limit=offset + limit + 1
    )
    following = offset + limit if len(found) > offset + limit else None
    return json_response(
        {
            "data": found[offset : offset + limit],
            "next_cursor": next_cursor("fidelity-checks", query, version, following),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/checks/{uid}",
    summary="One fidelity check's record",
    description="What the check measured at its time, as it was written: per question the "
    "origin scored and why not, the window and the warning; freshness, signals and coverage.",
    response_model=FidelityCheckOne,
    responses=versioned(400, 404, notes={404: "No such check (`fidelity.not_found`)"}),
)
def one_check(services: ServicesDep, uid: CheckUid, at: At = "head") -> Response:
    version = _live(services, at)
    moment = _check_at(uid)
    found = (
        fidelity.checks(services.twin, at=version, since=moment, until=moment, limit=1)
        if moment is not None
        else []
    )
    if not found or found[0]["uid"] != uid:
        raise ApiError(f"no check {uid!r} at this version", code="fidelity.not_found")
    return json_response({"data": found[0], "version": version_json(version)}, version)


@router.post(
    "/corrections",
    status_code=202,
    summary="Run a correction by hand",
    description="Starts the job a correction of the fidelity ladder is (kind `correction`): "
    "the recent evaluation of the question's model specification, over the history of the "
    "backtest's initial window plus the fidelity window, of the active model "
    "(`recalibrate`) or of every candidate that is not retired (`reselect`). An accepted "
    "one is fitted on that history and made active, and the question's record follows; "
    "otherwise the models are left as they are. With the key of the correction an "
    "incident of the question waits for (`detail.pending.key` of the incident), it is that "
    "correction, reported on the incident, and run anew when its job has ended; with any "
    "other key, or none, it changes the models and no incident.",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            404,
            409,
            notes={
                400: "The body is malformed, or the question has no model specification "
                "(`fidelity.bad_request`)",
                404: "The contract has no such question (`fidelity.not_found`)",
                409: "The key is the pending correction of another rung (`fidelity.conflict`), "
                "or was used for another request (`job.key_reused`)",
            },
        ),
    },
)
def correct_by_hand(
    services: ServicesDep,
    body: Annotated[CorrectionAsked, Body()],
    idempotency_key: ComputeKey = None,
) -> Response:
    document, created = by_hand(services, body.question, body.rung, idempotency_key)
    return json_response(
        {"job": document},
        headers={"Location": f"{API_PREFIX}/lifecycle/jobs/{document['id']}"},
        status=202 if created else 200,
    )


@router.get(
    "/questions/{id}/errors",
    summary="The errors measured of one question, oldest first",
    description="One row per origin a check scored: the origin, when it was scored, its "
    "error in the question's metric, the points it was measured on, the model that "
    "predicted it, and whether it counts in the window of the latest check (inside its "
    "window, and predicted by the model active then after the end of its data). `from` and "
    "`to` bound the time it was scored. JSON, Arrow or Parquet, paged.",
    response_model=Page,
    responses={**tabular(400, 404), **errors(400, 404)},
)
def question_errors(
    request: Request,
    services: ServicesDep,
    id: QuestionId,
    since: Since = None,
    until: Until = None,
    at: At = "head",
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    lower, upper = _time(since, "from"), _time(until, "to")
    query = fingerprint("fidelity-errors", id, since, until)
    version, position = start(services, cursor, "fidelity-errors", query, at, LIVE_BRANCH)
    manifest = services.twin.manifest(at=version)
    declared = dict((manifest.contract.questions if manifest.contract else None) or {})
    if id not in declared:
        raise ApiError(f"the contract has no question {id!r}", code="fidelity.not_found")
    rows = [
        row
        for row in fidelity.errors(services.twin, id, at=version, since=lower, until=upper)
        if row["scored"]
    ]
    counted = _window_rule(services, version, manifest, declared[id].horizon, id)
    table = pa.Table.from_pylist(
        [
            {
                "origin": row["origin"],
                "scored_at": row["at"],
                "error": row["error"],
                "n": row["n"],
                "model": row["model"],
                "in_window": counted(row),
                "check": row["check"],
            }
            for row in rows
        ],
        schema=ERRORS_SCHEMA,
    )
    return paged_response(table, chosen, version, per_page, position, "fidelity-errors", query)


def _window_rule(
    services: Services,
    version: VersionRef,
    manifest: Manifest,
    horizon: timedelta | None,
    question: str,
) -> Any:
    """Whether a scored origin counts in the window of the latest check."""
    latest = fidelity.checks(services.twin, at=version, limit=1)
    window = manifest.fidelity.window if manifest.fidelity is not None else None
    entry = ((latest[0]["questions"] or {}).get(question) if latest else None) or {}
    if not latest or horizon is None or window is None or not entry.get("model"):
        return lambda row: False
    now = latest[0]["at"]
    fitted_to = entry.get("fitted_to")
    cut = datetime.fromisoformat(fitted_to.replace("Z", "+00:00")) if fitted_to else None

    def counted(row: dict[str, Any]) -> bool:
        origin = row["origin"]
        return (
            origin is not None
            and row["model"] == entry["model"]
            and in_window(origin, now=now, horizon=horizon, window=window)
            and (cut is None or origin >= cut)
        )

    return counted


@router.get(
    "/incidents",
    summary="The incidents, newest first",
    description="Every incident the checks and operators opened: `breach` (a question's "
    "error over the window above its tolerance), `stale` (its data older than its "
    "staleness), `signal` (an observed signal stuck or biased) and `reconcile`; newest "
    "first by the time it was opened. `status` `unresolved` gives every one not resolved.",
    response_model=IncidentList,
    responses=versioned(400, 404),
)
def list_incidents(
    services: ServicesDep,
    status: Annotated[
        IncidentFilter | None,
        Query(description="Only incidents in this status; `unresolved`: every one not resolved"),
    ] = None,
    kind: Annotated[IncidentKind | None, Query(description="Only incidents of this kind")] = None,
    subject: Annotated[
        str | None, Query(description="Only incidents about this", max_length=400)
    ] = None,
    at: At = "head",
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("fidelity-incidents", status, kind, subject)
    version, position = start(services, cursor, "fidelity-incidents", query, at, LIVE_BRANCH)
    found = fidelity.incidents(services.twin, at=version, kind=kind, status=status, subject=subject)
    found.reverse()
    offset = offset_of(position)
    following = offset + limit if len(found) > offset + limit else None
    return json_response(
        {
            "data": found[offset : offset + limit],
            "next_cursor": next_cursor("fidelity-incidents", query, version, following),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/incidents/{id}",
    summary="One incident and every step it went through",
    description="With `resolved_version`, the version that resolved it, when it is.",
    response_model=IncidentOne,
    responses=versioned(400, 404, notes={404: "No such incident (`fidelity.not_found`)"}),
)
def one_incident(services: ServicesDep, id: IncidentUid, at: At = "head") -> Response:
    version = _live(services, at)
    found = fidelity.incident(services.twin, id, at=version)
    if found is None:
        raise ApiError(f"no incident {id!r} at this version", code="fidelity.not_found")
    return json_response({"data": found, "version": version_json(version)}, version)


def _stepped(services: Services, answered: dict[str, Any]) -> Response:
    """The answer of an operator's step: the record, what was told, the version written."""
    events = answered.pop("events", [])
    written = answered.pop("version", None)
    version = services.store.resolve(written)[0] if written else None
    if version is not None:
        emit(services, events, version.t)
    return json_response(
        {
            "data": answered,
            "events": events,
            "version": None if version is None else version_json(version),
        },
        version,
    )


STEP_ERRORS = errors(
    400,
    404,
    409,
    notes={
        400: "The body is malformed (`api.bad_request`), or the step does not apply to an "
        "incident of this kind (`fidelity.bad_request`)",
        404: "No such incident (`fidelity.not_found`)",
        409: f"The incident is not in a status the step applies to (`fidelity.conflict`). "
        f"{UNSUPPORTED_NOTE}",
    },
)


@router.post(
    "/incidents/{id}/retry",
    summary="Put an incident back to its start",
    description="A breach incident goes back to cycle 1, rung 1, with nothing pending or "
    "awaited; a stale one counts no stale check. The next check takes it on. One version "
    "at the head's time; the step names who took it and why.",
    response_model=IncidentStepped,
    responses={200: {"description": "The incident as it is now"}, **STEP_ERRORS},
)
def retry_incident(
    services: ServicesDep, id: IncidentUid, body: Annotated[OperatorStep, Body()]
) -> Response:
    moment = services.store.head().t
    answered = fidelity.retry(services.twin, id, by=body.by, note=body.note, now=moment)
    log.info("incident retried by hand", incident=id, by=body.by)
    return _stepped(services, answered)


@router.post(
    "/incidents/{id}/resolve",
    summary="Resolve an incident by hand",
    description="For a `signal` incident (its signal is cleared with it, and sync writes its "
    "value again) or a `reconcile` one. One version at the head's time; the step names who "
    "took it and why.",
    response_model=IncidentStepped,
    responses={200: {"description": "The incident as it is now"}, **STEP_ERRORS},
)
def resolve_incident(
    services: ServicesDep, id: IncidentUid, body: Annotated[OperatorStep, Body()]
) -> Response:
    moment = services.store.head().t
    answered = fidelity.resolve(services.twin, id, by=body.by, note=body.note, now=moment)
    log.info("incident resolved by hand", incident=id, by=body.by)
    return _stepped(services, answered)


@router.get(
    "/signals",
    summary="The observed signals ever found suspect",
    description="By property, then object: `suspect` (stuck or biased; sync leaves its value "
    "out and keeps the raw reading here, and the twin's inferred value is in force) or `ok` "
    "(cleared).",
    response_model=SignalList,
    responses=versioned(400, 404),
)
def list_signals(
    services: ServicesDep,
    status: Annotated[SignalStatus | None, Query(description="Only signals in this status")] = None,
    at: At = "head",
) -> Response:
    version = _live(services, at)
    found = fidelity.signals(services.twin, at=version, status=status)
    return json_response({"data": found, "version": version_json(version)}, version)


@router.post(
    "/signals/{uid}/clear",
    summary="Clear a suspect signal by hand",
    description="The signal is `ok` again and its incident is resolved with it; the next sync "
    "run writes its value again. One version at the head's time; the step names who took it "
    "and why.",
    response_model=SignalCleared,
    responses={
        200: {"description": "The signal as it is now"},
        **errors(
            400,
            404,
            409,
            notes={
                404: "No such signal (`fidelity.not_found`)",
                409: f"The signal is not suspect (`fidelity.conflict`). {UNSUPPORTED_NOTE}",
            },
        ),
    },
)
def clear_signal(
    services: ServicesDep, uid: SignalUid, body: Annotated[OperatorStep, Body()]
) -> Response:
    moment = services.store.head().t
    answered = fidelity.clear_signal(services.twin, uid, by=body.by, note=body.note, now=moment)
    log.info("signal cleared by hand", signal=uid, by=body.by)
    return _stepped(services, answered)
