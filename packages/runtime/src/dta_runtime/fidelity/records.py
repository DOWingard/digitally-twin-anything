"""The records fidelity keeps on the live line, their names, and the JSON inside them.

Uids:

    twin_fidelity:state                                   the twin's state (one record)
    twin_fidelity_check:<YYYYMMDDTHHMMSS[.ffffff]Z>       one check, by its time
    twin_incident:<kind>-<opened YYYYMMDDTHHMMSS[.ffffff]Z>-<10 hex>[-<n>]
                                                          one incident; the hex is of the
                                                          SHA-256 of its subject, n counts
                                                          incidents of one subject opened at
                                                          one time
    twin_signal:<20 hex>                                  one signal: the SHA-256 of
                                                          `<Type.property>|<object uid>`

Times inside the JSON are ISO 8601 in UTC (`2026-01-05T12:00:00Z`); every JSON field is
written with sorted keys and no spaces.

`TwinFidelityCheck.questions`, per question id with a horizon and a model specification:

    model            uid of the model the origin was predicted with, or null
    fitted_to        end of the data that model was fitted on, or null; only origins
                     predicted by the active model as fitted now count towards the
                     window error, the evidence of an incident and the bias test
    metric, tolerance, horizon_steps
    origin           the origin of this check
    scored           whether this check scored it
    reason           null, or {"code", "message"} saying why nothing was scored:
                     no_specification, no_active_model, bad_target, horizon_not_on_grid,
                     no_tolerance, not_fitted,
                     already_scored, before_fit, no_version, ahead_of_data, deadline,
                     predictor_refused, predictor_failed, bad_prediction,
                     no_observations, metric_undefined
    error, n         the origin's error and its number of points (when scored)
    series           {uid: d}: the mean signed residual (observed less predicted) per
                     series (when scored)
    estimates        {uid: value}: the last predicted value of every series predicted,
                     at `estimates_at` (when predicted)
    estimates_at     the time of those values: the origin plus the horizon on the grid
    left_out         uids of the series left out because they were suspect in the span
    interval_ratio   mean width of the 80 % interval over the tolerance, or null
    window           {"error", "n", "breach"}: the window error over origins the model
                     had not seen, and whether it breaches
    warning          {"sum", "raised", "last", "request", "fitted_to"}: the warning sum
                     after this check, whether a warning was raised, the time of the
                     latest warning, the pre-emptive correction asked for (or null), and
                     the end of the data of the model the sum was summed for

`TwinFidelityCheck.freshness`: {"questions": {id: {"fresh", "age_s", "staleness_s",
"source"}}, "sources": {id: {"status", "watermark", "last_success_at"}}}.

`TwinFidelityCheck.signals`:

    tested     {"stuck": count of signals tested, "biased": count of series tested}
    flagged    [{"signal", "property", "object", "reason", "detail"}]
    cleared    [signal uid]
    suspect    [signal uid]: suspect after the check
    inferred   [{"signal", "property", "object", "value", "t", "question", "model"}]
    alarming   {question id: {"tested", "alarming", "biased"}}: "biased" are the series
               the question's own test calls; one is flagged only when the question
               would infer it and every other question on its property bears it out
    uncorroborated
               [{"signal", "property", "object", "question", "by", "why"}]: a series
               the inferring `question` called, not flagged because question `by`
               did not bear it out; `why` is `not_tested` (too few scored origins, or
               not scored), `no_alarm` or `other_side`
    skipped    [{"signal", "reason"}]: inferred values that could not be written

`TwinFidelityCheck.coverage`: {"validated": [ids], "provisional": [ids], "failing": [ids],
"rows_rejected": rows set aside by the sync runs that ended inside the window,
"runs": their number}.

`TwinIncident.detail`:

    question, spec, tolerance    (breach and stale)
    opened     what was measured at the opening: {"error", "n", "check"} (breach),
               {"age_s", "staleness_s", "source", "check"} (stale), the test (signal),
               what the caller gave (reconcile)
    now        the same, as of the latest check that looked at it
    pending    {"key", "rung", "rung_number", "attempt", "cycle", "at"}: the correction
               asked for and not answered yet, or null (breach); `cycle` is the cycle of
               its key
    after_remodel   rung 2 was brought back by a re-model in this cycle (breach)
    cycle_offset    cycles begun before the latest retry: the cycle of a correction's
               key is this plus `cycle`, so that no key is used twice (breach)
    next_cycle_at   when the next cycle starts, once the current one has spent its
               rungs; null while one runs or none is left (breach)
    cycles_spent    no further cycle is left and the incident waits on its re-model
               request (breach)
    last_result     the latest correction reported back: {"key", "accepted", "model",
               "fitted_to", "error", "detail"} (breach)
    remodel    {"requested_at", "models": {uid: identity}}: the candidates of the
               specification when the re-model was asked for (breach)
    failed_at  when it failed
    signal, property, object     (signal)
    resolved_by                  who resolved it by hand, or null

`TwinIncident.steps`: every step in order, each {"at", "action", ...}; the actions are
opened, request {key, rung, attempt, cycle}, result {key, accepted, model, fitted_to,
error}, held, not_held, recovered {n, error}, remodel_requested {timeout_s},
remodelled, cycle {cycle}, cycle_ended {cycle, next_cycle}, cycles_spent {cycles},
failed {reason}, resolved, stale {count}, fresh {count}, flagged, cleared, retry {by,
note}, resolve {by, note}, closed. A step taken at a check carries its uid as `check`;
a step of a breach incident's ladder carries the cycle it belongs to as `cycle` and
when the next cycle is due as `next_cycle_at` (null when none is).

`TwinSignal.value_at_flag` and `reading` hold the value as JSON. `TwinSignal.detail`:
{"test": the numbers of the test that flagged it, "question": the question whose
residuals did (biased), "episodes": [{"since", "until", "reason", "cleared_by"}]: every
time it was suspect, "estimate": {"value", "t", "question", "model", "tolerance"}: the
latest inferred value}.

`opened_version` of an incident is the version its opening was decided at: the id of
the version that writes it is not known before it is written.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from dta_core.log import Log
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, TimeRegression, VersionRef
from dta_core.store.model import iso_utc

from ..twin import Twin
from .errors import BAD_REQUEST, refused
from .ladder import RUNG_NAMES, Pending, Position

log = Log("runtime.fidelity")

# Held from reading the incidents to writing what was decided about them, by every
# writer of this package in this process: two decisions on one incident must not both
# rest on the same read.
WRITES = threading.Lock()

FIDELITY_TYPE = "TwinFidelity"
CHECK_TYPE = "TwinFidelityCheck"
INCIDENT_TYPE = "TwinIncident"
SIGNAL_TYPE = "TwinSignal"
QUESTION_TYPE = "TwinQuestion"
MODEL_TYPE = "TwinModel"
SOURCE_TYPE = "TwinSource"
RUN_TYPE = "TwinSyncRun"
ACTION_TYPE = "TwinAction"
ACTION_RECORD_TYPE = "TwinActionRecord"
TYPES = (FIDELITY_TYPE, CHECK_TYPE, INCIDENT_TYPE, SIGNAL_TYPE)

STATE_UID = "twin_fidelity:state"
CHECK_SCOPE = "twin_fidelity_check"
INCIDENT_SCOPE = "twin_incident"
SIGNAL_SCOPE = "twin_signal"
QUESTION_SCOPE = "twin_question"
# Recorded as the actor of every version this package writes and the origin of its records.
ACTOR = "fidelity"

BREACH, STALE, SIGNAL, RECONCILE = "breach", "stale", "signal", "reconcile"
KINDS = (BREACH, STALE, SIGNAL, RECONCILE)
SUSPECT, OK = "suspect", "ok"
STUCK, BIASED = "stuck", "biased"

INCIDENT_COLUMNS = [
    "kind",
    "subject",
    "status",
    "opened_at",
    "opened_version",
    "resolved_at",
    "rung",
    "attempt",
    "cycle",
    "awaiting",
    "requested_at",
    "detail",
    "steps",
]
SIGNAL_COLUMNS = [
    "property",
    "object",
    "status",
    "reason",
    "since",
    "value_at_flag",
    "reading",
    "reading_at",
    "clear_checks",
    "detail",
]
CHECK_COLUMNS = [
    "at",
    "at_version",
    "state",
    "questions",
    "freshness",
    "signals",
    "coverage",
    "duration_ms",
    "trace_id",
]


# -- names -------------------------------------------------------------------------------


def stamp(moment: datetime) -> str:
    """`20260105T120000Z`, with microseconds only when there are any."""
    moment = moment.astimezone(UTC)
    text = moment.strftime("%Y%m%dT%H%M%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def check_uid(now: datetime) -> str:
    return f"{CHECK_SCOPE}:{stamp(now)}"


def _digest(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def incident_uid(kind: str, subject: str, opened_at: datetime, number: int = 1) -> str:
    suffix = "" if number == 1 else f"-{number}"
    return f"{INCIDENT_SCOPE}:{kind}-{stamp(opened_at)}-{_digest(subject, 10)}{suffix}"


def signal_uid(property_name: str, object_uid: str) -> str:
    """The uid of the signal of one property (`Type.property`) of one object."""
    return f"{SIGNAL_SCOPE}:{_digest(f'{property_name}|{object_uid}', 20)}"


def question_uid(question_id: str) -> str:
    return f"{QUESTION_SCOPE}:{question_id}"


def warning_id(question: str, now: datetime) -> str:
    return f"warning-{question}-{stamp(now)}"


# -- JSON --------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no JSON form")


def dumps(document: Any) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_plain
    )


def loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        log.warn("a fidelity record holds text that is not JSON; it is read as absent")
        return default


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else iso_utc(moment)


def parse_time(text: Any) -> datetime | None:
    if text is None:
        return None
    if isinstance(text, datetime):
        return text.astimezone(UTC)
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC)


def utc(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(UTC)


def aware(moment: Any, name: str = "now") -> datetime:
    """A time a caller gave, in UTC; refused when it has no time zone, which would
    otherwise be read as the host's."""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        log.warn("a time without a time zone was given", argument=name)
        raise refused(BAD_REQUEST, f"{name} is a datetime with a time zone", argument=name)
    return moment.astimezone(UTC)


# -- documents ---------------------------------------------------------------------------


def incident_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        **{name: row.get(name) for name in INCIDENT_COLUMNS if name not in ("detail", "steps")},
        "opened_at": utc(row.get("opened_at")),
        "resolved_at": utc(row.get("resolved_at")),
        "awaiting": utc(row.get("awaiting")),
        "requested_at": utc(row.get("requested_at")),
        "detail": loads(row.get("detail"), {}),
        "steps": loads(row.get("steps"), []),
    }


def signal_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "property": row.get("property"),
        "object": row.get("object"),
        "status": row.get("status"),
        "reason": row.get("reason"),
        "since": utc(row.get("since")),
        "value_at_flag": loads(row.get("value_at_flag")),
        "reading": loads(row.get("reading")),
        "reading_at": utc(row.get("reading_at")),
        "clear_checks": row.get("clear_checks") or 0,
        "detail": loads(row.get("detail"), {}),
    }


def check_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "at": utc(row.get("at")),
        "at_version": row.get("at_version"),
        "state": row.get("state"),
        "questions": loads(row.get("questions"), {}),
        "freshness": loads(row.get("freshness"), {}),
        "signals": loads(row.get("signals"), {}),
        "coverage": loads(row.get("coverage"), {}),
        "duration_ms": row.get("duration_ms"),
        "trace_id": row.get("trace_id"),
    }


def state_document(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"uid": STATE_UID, "state": "unknown", "since": None, "check": None}
    return {
        "uid": STATE_UID,
        "state": row.get("state") or "unknown",
        "since": utc(row.get("since")),
        "check": row.get("check"),
    }


def incident_row(document: Mapping[str, Any]) -> dict[str, Any]:
    """The stored row of an incident document."""
    row = {"uid": document["uid"]}
    for name in INCIDENT_COLUMNS:
        if name in ("detail", "steps"):
            row[name] = dumps(document.get(name) or ({} if name == "detail" else []))
        else:
            row[name] = document.get(name)
    return row


def signal_row(document: Mapping[str, Any]) -> dict[str, Any]:
    row = {"uid": document["uid"]}
    for name in SIGNAL_COLUMNS:
        value = document.get(name)
        if name in ("value_at_flag", "reading"):
            value = None if value is None else dumps(value)
        elif name == "detail":
            value = dumps(value or {})
        row[name] = value
    return row


# -- incidents as ladder positions ---------------------------------------------------------


def _pending(document: Any) -> Pending | None:
    if not isinstance(document, Mapping) or not document.get("key"):
        return None
    moment = parse_time(document.get("at"))
    assert moment is not None
    return Pending(
        key=str(document["key"]),
        rung=int(document["rung_number"]),
        attempt=int(document["attempt"]),
        cycle=int(document["cycle"]),
        at=moment,
    )


def pending_document(pending: Pending | None, rung_name: str | None) -> dict[str, Any] | None:
    if pending is None:
        return None
    return {
        "key": pending.key,
        "rung": rung_name,
        "rung_number": pending.rung,
        "attempt": pending.attempt,
        "cycle": pending.cycle,
        "at": iso_utc(pending.at),
    }


def position_of(document: Mapping[str, Any]) -> Position:
    detail = document.get("detail") or {}
    return Position(
        status=document["status"],
        rung=int(document.get("rung") or 1),
        attempt=int(document.get("attempt") or 0),
        cycle=int(document.get("cycle") or 1),
        awaiting=utc(document.get("awaiting")),
        pending=_pending(detail.get("pending")),
        after_remodel=bool(detail.get("after_remodel")),
        requested_at=utc(document.get("requested_at")),
        failed_at=parse_time(detail.get("failed_at")),
        offset=int(detail.get("cycle_offset") or 0),
        due=parse_time(detail.get("next_cycle_at")),
        spent=bool(detail.get("cycles_spent")),
    )


def with_position(document: dict[str, Any], position: Position) -> dict[str, Any]:
    """The incident document with the ladder's fields of a position."""
    detail = dict(document.get("detail") or {})
    detail["pending"] = pending_document(
        position.pending, RUNG_NAMES.get(position.pending.rung) if position.pending else None
    )
    detail["after_remodel"] = position.after_remodel
    detail["failed_at"] = iso(position.failed_at)
    detail["cycle_offset"] = position.offset
    detail["next_cycle_at"] = iso(position.due)
    detail["cycles_spent"] = position.spent
    return {
        **document,
        "status": position.status,
        "rung": position.rung,
        "attempt": position.attempt,
        "cycle": position.cycle,
        "awaiting": position.awaiting,
        "requested_at": position.requested_at,
        "detail": detail,
    }


# -- writing -----------------------------------------------------------------------------


def write(
    twin: Twin,
    upserts: Mapping[str, list[dict[str, Any]]],
    *,
    now: datetime,
    cause: str,
    note: str,
    extra: Mapping[str, Any] | None = None,
    inferred: Mapping[str, Mapping[str, Provenance]] | None = None,
    trace: str | None = None,
) -> VersionRef:
    """One version of the live line holding fidelity's records (and inferred values with
    their own provenance), at `now` or at the head's time when that is later."""
    store = twin.store
    for attempt in (1, 2):
        head = store.head()
        changes = ChangeSet(
            upserts={name: list(rows) for name, rows in upserts.items() if rows},
            provenance=Provenance(source=ACTOR, kind=FactKind.inferred, t_valid=now),
            property_provenance={uid: dict(found) for uid, found in (inferred or {}).items()},
        )
        meta = CommitMeta(
            cause=cause,
            actor=ACTOR,
            t=max(now, head.t),
            trace=trace,
            note=note,
            extra=dict(extra or {}),
        )
        try:
            return store.commit(changes, meta)
        except TimeRegression as exc:
            # A sync wrote a later version between reading the head and committing.
            if attempt == 2:
                log.error(
                    "fidelity records could not be written: the live line keeps moving",
                    exc=exc,
                    note=note,
                )
                raise
            log.warn(
                "the live line moved on while fidelity records were being written; writing "
                "them again at the new head",
                note=note,
                attempt=attempt,
            )
    raise AssertionError("unreachable")
