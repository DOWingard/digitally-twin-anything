"""The fidelity check and the corrections it asks for, as jobs of the compute lane.

A check measures the twin at a boundary `c` of the fidelity interval on the twin's
clock: it predicts each question's origin through the worker, exactly as the predict
route does, and hands the library the freshness of each question at `c`. Once it is
written, what it decided is told to the notifier and every correction it asked for is
queued as a job of its own; the check never waits for one.

A correction runs the recent evaluation of the question's specification for its rung
and always reports back, accepted or not. One that never reported (the process ended
with it queued or running, or the report could not be written) is still pending on its
incident: every check that is not a replay starts the pending corrections again under
their keys, and a key whose job has ended runs anew.

Steps written outside a check (a correction's result, an operator's retry, resolve or
clear) are stamped with the head's time, so no version is placed before the data the
live line already holds.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log, current_trace_id, new_trace_id
from dta_core.store import VersionRef

from .. import fidelity
from ..fidelity import CorrectionRequest, CorrectionResult, Freshness
from ..fidelity.outcome import corrections_of
from ..fidelity.reading import floor_to
from ..protocol import InterventionRef
from . import evaluating, predicting
from .jobs import JobsClosed, Outcome, Work
from .live import ClockReading, HeadState, sync_document
from .problems import ApiError
from .tables import iso, version_json

if TYPE_CHECKING:
    from .services import Services

log = Log("api.fidelity")

CHECK_KIND = "fidelity"
CORRECTION_KIND = "correction"
CHECK_BUDGET_S = 300.0
CORRECTION_BUDGET_S = 900.0
# The interval the check compares observations with.
CHECK_LEVELS = (80.0,)
BREACH = "breach"
UNRESOLVED = "unresolved"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def emit(services: Services, events: Sequence[Mapping[str, Any]], at: datetime) -> None:
    """Tell the notifier what a write of the fidelity library returned, in order."""
    try:
        services.notifier.emit_all(events, time=at)
    except Exception as exc:
        # The records are written; a notice that cannot be queued is not a reason to undo.
        log.error("fidelity events could not be queued", exc=exc, count=len(events))


# -- the check ---------------------------------------------------------------------------


def check_time(services: Services, state: HeadState) -> datetime:
    """The boundary the check of now is for: the newest multiple of the fidelity interval
    at or before the twin's clock. Refuses a twin that is not live and a clock that
    cannot be read."""
    found = state.manifest.fidelity
    if not state.live or found is None or found.interval is None or found.window is None:
        log.info("fidelity check refused: the twin is not live or sets no fidelity interval")
        raise ApiError(
            "the twin has no sync interval or no fidelity interval and window: a static twin "
            "has no fidelity checks",
            code="fidelity.not_live",
        )
    reading = services.clocks.read(state.manifest, state.manifest_hash)
    if not reading.available or reading.now is None:
        log.warn("fidelity check refused: the twin's clock cannot be read", clock=reading.kind)
        raise ApiError(
            "the twin's clock cannot be read now, so the check's time is not known; ask again "
            "once it can",
            code="clock.unavailable",
            detail={"clock": reading.kind},
        )
    return floor_to(reading.now, found.interval)


def freshness_at(state: HeadState, at: datetime) -> dict[str, Freshness]:
    """How old each question's data is at `at`, as the health document measures it."""
    document = sync_document(state, ClockReading("check", True, at), None)
    if document is None:
        return {}
    allowed = {limit.id: limit.staleness.total_seconds() for limit in state.limits}
    return {
        found["id"]: Freshness(
            fresh=found["fresh"],
            age_s=found["freshness_s"],
            staleness_s=allowed.get(found["id"]),
            source=found["source"],
        )
        for found in document["questions"]
    }


def predictor_for(services: Services, known: VersionRef) -> fidelity.Predictor:
    """What predicts an origin for the check: the worker, asked the way the predict
    route asks, with the 80 % interval."""

    def predict(
        model: Mapping[str, Any],
        *,
        origin: VersionRef,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        budget = max(0.001, min(predicting.PREDICT_BUDGET_S, deadline_s))
        _, table = predicting.predict(
            services,
            model,
            origin,
            horizon=horizon,
            levels=CHECK_LEVELS,
            interventions=interventions,
            step=step,
            budget_s=budget,
            wait_until=time.monotonic() + budget,
            known=known,
        )
        return table

    return predict


def _uids(documents: Sequence[Mapping[str, Any]]) -> list[str]:
    return [document["uid"] for document in documents]


def check_work(services: Services, at: datetime) -> Work:
    """The check at `at`, then its notices and its corrections."""

    def work() -> Outcome:
        started = time.perf_counter()
        head = services.store.head()
        state = services.head_state(head)
        trace_id = current_trace_id() or new_trace_id()
        predictor = (services.check_predictor or predictor_for)(services, head)
        outcome = fidelity.check(
            services.twin,
            at,
            predictor=predictor,
            freshness=freshness_at(state, at),
            trace_id=trace_id,
            deadline_s=services.check_budget_s,
        )
        summary: dict[str, Any] = {
            "check": outcome.check["uid"],
            "at": iso(at),
            "replayed": outcome.replayed,
            "state_before": outcome.state_before,
            "state": outcome.state_after,
            "version": version_json(outcome.version),
            "opened": _uids(outcome.opened),
            "changed": _uids(outcome.changed),
            "resolved": _uids(outcome.resolved),
            "flagged": _uids(outcome.flagged),
            "cleared": _uids(outcome.cleared),
            "events": [found["type"] for found in outcome.events],
            "corrections": [],
        }
        if outcome.replayed:
            # A check of a time that has its record: nothing is told or started again.
            log.info("fidelity check answered from its record", check=summary["check"])
            summary["events"] = []
            summary["record"] = outcome.check
            return Outcome(result=summary)
        emit(services, outcome.events, at)
        summary["corrections"] = start_corrections(services, outcome.corrections)
        log.info(
            "fidelity check job finished",
            check=summary["check"],
            state=outcome.state_after,
            events=len(outcome.events),
            corrections=len(summary["corrections"]),
            elapsed_ms=_ms(started),
        )
        return Outcome(result=summary)

    return work


# -- corrections -------------------------------------------------------------------------


def correction_params(request: CorrectionRequest, *, by_hand: bool = False) -> dict[str, Any]:
    """The parameters of a correction's job: the request without its key, which is the
    job's Idempotency-Key."""
    params = {name: value for name, value in request.document().items() if name != "key"}
    if by_hand:
        params["by_hand"] = True
    return params


def pending_requests(services: Services) -> list[CorrectionRequest]:
    """The corrections unresolved breach incidents wait for."""
    found: list[CorrectionRequest] = []
    for incident in fidelity.incidents(services.twin, kind=BREACH, status=UNRESOLVED):
        found.extend(corrections_of([(None, incident)]))
    return found


def start_correction(
    services: Services, request: CorrectionRequest, *, again: bool, by_hand: bool = False
) -> tuple[dict[str, Any], bool]:
    """Queue one correction under its key; the job document and whether it was made."""
    return services.compute_jobs.start(
        CORRECTION_KIND,
        correction_params(request, by_hand=by_hand),
        correction_work(services, request, services.correction_budget_s),
        request.key,
        again=again,
    )


def start_corrections(
    services: Services, asked: Sequence[CorrectionRequest]
) -> list[dict[str, Any]]:
    """Queue what a check asked for and every correction still pending on an incident.
    A pending one whose job has ended without reporting starts anew under its key."""
    requests = {request.key: request for request in asked}
    try:
        for request in pending_requests(services):
            requests.setdefault(request.key, request)
    except DtaError as exc:
        log.error(
            "pending corrections could not be read; only those the check asked for are started",
            exc=exc,
            code=exc.code,
        )
    started: list[dict[str, Any]] = []
    for request in requests.values():
        try:
            document, created = start_correction(services, request, again=not request.warning)
        except (JobsClosed, ApiError) as exc:
            # The incident still waits for it: the next check starts it.
            log.warn(
                "a correction could not be queued; the next check asks again",
                key=request.key,
                code=exc.code,
                reason=exc.message,
            )
            continue
        if created:
            log.info(
                "correction queued",
                key=request.key,
                incident=request.incident,
                rung=request.rung,
                job=document["id"],
            )
        started.append(
            {
                "key": request.key,
                "incident": request.incident,
                "question": request.question,
                "rung": request.rung,
                "job": document["id"],
                "created": created,
            }
        )
    return started


def _refused(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, DtaError):
        code = exc.code
        reason = f"{exc.code}: {exc.message}"
        if code.startswith("worker."):
            code = "worker_refused"
    else:
        code, reason = "unexpected", f"the correction failed: {type(exc).__name__}"
    return {
        "accepted": False,
        "model": None,
        "fitted_to": None,
        "error": None,
        "reason": reason,
        "reason_code": code,
        "version": None,
    }


def report(
    services: Services, request: CorrectionRequest, result: CorrectionResult
) -> dict[str, Any] | None:
    """Record a correction's result on its incident and tell what that changed. None when
    it could not be written: the incident then still waits, and the next check runs it
    again."""
    started = time.perf_counter()
    try:
        moment = services.store.head().t
        answered = fidelity.corrected(services.twin, request, result, now=moment)
    except DtaError as exc:
        log.error(
            "a correction's result could not be recorded; the next check runs it again",
            exc=exc,
            key=request.key,
            incident=request.incident,
            code=exc.code,
        )
        return None
    emit(services, answered.get("events") or [], moment)
    log.debug("correction reported back", key=request.key, elapsed_ms=_ms(started))
    return answered


def correction_work(services: Services, request: CorrectionRequest, budget_s: float) -> Work:
    """Run a correction and report it back, whatever it came to."""

    def work() -> Outcome:
        started = time.perf_counter()
        log.info(
            "correction started",
            key=request.key,
            incident=request.incident,
            question=request.question,
            rung=request.rung,
            budget_s=budget_s,
        )
        try:
            found = evaluating.recent_evaluation(
                services,
                spec=request.spec,
                rung=request.rung,
                budget_s=budget_s,
                note={"correction": request.key, "incident": request.incident},
            )
        except Exception as exc:
            log.error(
                "correction not accepted: its evaluation failed",
                exc=exc,
                key=request.key,
                rung=request.rung,
            )
            found = _refused(exc)
        result = CorrectionResult(
            accepted=bool(found["accepted"]),
            model=found.get("model"),
            fitted_to=found.get("fitted_to"),
            error=found.get("error"),
            detail={
                "rung": request.rung,
                "scope": evaluating.RECENT,
                "reason": found.get("reason"),
                "reason_code": found.get("reason_code"),
                "frame": _plain(found.get("frame")),
                "version": found.get("version"),
                "duration_ms": found.get("duration_ms"),
            },
        )
        answered = report(services, request, result)
        document = {
            "key": request.key,
            "incident": request.incident,
            "question": request.question,
            "rung": request.rung,
            "accepted": result.accepted,
            "model": result.model,
            "fitted_to": iso(result.fitted_to) if result.fitted_to else None,
            "error": result.error,
            "reason": found.get("reason"),
            "reason_code": found.get("reason_code"),
            "version": found.get("version"),
            "frame": _plain(found.get("frame")),
            "reported": answered is not None,
            "incident_status": (answered or {}).get("status"),
        }
        log.info(
            "correction finished",
            key=request.key,
            accepted=result.accepted,
            model=result.model,
            reason_code=found.get("reason_code"),
            reported=answered is not None,
            elapsed_ms=_ms(started),
        )
        if answered is None:
            return Outcome(
                result=document,
                error={
                    "code": "correction.unreported",
                    "message": "the correction ran and its result could not be recorded",
                },
            )
        return Outcome(result=document)

    return work


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Mapping):
        return {name: _plain(found) for name, found in value.items()}
    return value


def by_hand(
    services: Services, question: str, rung: str, key: str | None
) -> tuple[dict[str, Any], bool]:
    """A correction asked for by hand. With the key an incident of the question waits
    for, it is that correction (the same job, run anew when its job ended); otherwise a
    correction of its own, whose result changes the models and no incident."""
    head = services.store.head()
    manifest = services.twin.manifest(at=head)
    questions = dict((manifest.contract.questions if manifest.contract else None) or {})
    declared = questions.get(question)
    if declared is None:
        raise ApiError(f"the contract has no question {question!r}", code="fidelity.not_found")
    if not declared.model:
        raise ApiError(
            f"{question} is answered by no model specification; there is nothing to correct",
            code="fidelity.bad_request",
        )
    open_incident = next(
        iter(
            fidelity.incidents(
                services.twin, at=head, kind=BREACH, status=UNRESOLVED, subject=question
            )
        ),
        None,
    )
    if open_incident is not None and key is not None:
        for pending in corrections_of([(None, open_incident)]):
            if pending.key != key:
                continue
            if pending.rung != rung:
                raise ApiError(
                    f"{key} is the {pending.rung} correction {open_incident['uid']} waits for, "
                    f"not a {rung}",
                    code="fidelity.conflict",
                    detail={"incident": open_incident["uid"], "rung": pending.rung},
                )
            log.info("the pending correction of an incident is asked for by hand", key=key)
            return start_correction(services, pending, again=True)
    stamp = f"{head.t:%Y%m%dT%H%M%S}Z"
    request = CorrectionRequest(
        incident=open_incident["uid"] if open_incident else f"manual-{question}-{stamp}",
        question=question,
        spec=declared.model,
        rung=rung,  # type: ignore[arg-type]
        cycle=0,
        attempt=0,
        key=key or f"correction:manual-{question}-{stamp}:{rung}:{new_trace_id()[:8]}",
        # Reported back as a warning's correction is: no incident is stepped by it.
        warning=True,
    )
    return start_correction(services, request, again=False, by_hand=True)
