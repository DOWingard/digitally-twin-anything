"""The twin acting on the real system, as the API does it.

The actions library judges, records and sends; here it is handed what it needs at the
moment of each request: the twin's fidelity state and, for a proposal or a loop's run,
the incidents it has not resolved, both as one version (the head) holds them; the
twin's clock for `now`, the executor built at start and, for a control loop, its policy
run in the worker exactly as a function run is, reading the head and writing nothing.
An autonomous action is executed while the twin is healthy or correcting, no
unresolved incident is `stale` or `reconcile` and its target's value is not unknown;
a policy whose inputs are unknown is refused like any function run. What its
decisions mean for others is told to the notifier: a supervised proposal waiting for an
approval, an action the interlock blocked, an executed action whose effect was not
observed. A mismatch is also an incident of kind `reconcile` on its record, kept by the
fidelity library, and a later record that had its effect on the same control and object
closes it.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.store import VersionRef

from .. import actions, fidelity
from ..actions import catalog, gate
from ..actions.errors import BAD_POLICY_ANSWER, LOOP_DISABLED, POLICY_FAILED, refused
from ..actions.records import ACTIVE, PROPOSED, SUPERVISED, bare
from ..actions.records import BLOCKED as BLOCKED_STATUS
from ..actions.records import MISMATCH as MISMATCH_OUTCOME
from .fidelity_work import emit
from .jobs import Outcome, Work
from .problems import ApiError
from .tables import iso, version_json

if TYPE_CHECKING:
    from .services import Services

log = Log("api.actions")

APPROVAL_PENDING = "twin.action.approval_pending"
BLOCKED = "twin.action.blocked"
MISMATCH = "twin.action.mismatch"
RECONCILE = "reconcile"
LOOP_KIND = "loop"
# Who closes a reconcile incident when a later action had its effect.
RECONCILER = "reconciliation"
ANSWERS_KEPT = 1_000


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


# -- what the library is handed ------------------------------------------------------------


def clock_now(services: Services, head: VersionRef) -> datetime:
    """The twin's clock now, by the manifest at `head`; refused when it cannot be read."""
    record = services.twin.manifest_record(at=head)
    manifest = services.twin.manifest(at=head)
    reading = services.clocks.read(manifest, record["manifest_hash"])
    if not reading.available or reading.now is None:
        log.warn("refused: the twin's clock cannot be read", clock=reading.kind)
        raise ApiError(
            "the twin's clock cannot be read now, so the time of the decision is not known; "
            "ask again once it can",
            code="clock.unavailable",
            detail={"clock": reading.kind},
        )
    return reading.now


def state_at(services: Services, head: VersionRef) -> str:
    """The twin's fidelity state as the head holds it."""
    return str(fidelity.state(services.twin, head)["state"])


def standing_at(services: Services, head: VersionRef) -> tuple[str, list[dict[str, Any]]]:
    """The twin's fidelity state and the `kind` and `uid` of every incident it has not
    resolved, with the `withheld` cell of a signal incident whose value is unknown, as
    the head holds them: what the interlock judges a proposal by."""
    found = fidelity.standing(services.twin, head)
    incidents = [
        {key: item[key] for key in ("kind", "uid", "withheld") if key in item}
        for item in found["incidents"]
    ]
    return str(found["state"]), incidents


def type_document(
    action: Mapping[str, Any],
    has_executor: bool,
    failures: Mapping[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """An action type as the routes hand it out: whether the twin could execute it, and
    what its control sends when the twin fails (from `failures`, by control)."""
    control = action.get("control")
    on_failure = (failures or {}).get(control) if isinstance(control, str) else None
    return {
        **action,
        "executable": gate.executable(action, has_executor),
        "on_failure": on_failure,
    }


def failures_at(services: Services, version: VersionRef) -> dict[str, dict[str, Any] | None]:
    """The `on_failure` of every control of the manifest at `version`, by control; empty
    when the manifest cannot be read there."""
    try:
        manifest = services.twin.manifest(at=version)
    except DtaError as exc:
        log.warn(
            "the manifest cannot be read at this version; controls are listed without "
            "what they send on failure",
            exc=exc,
            code=exc.code,
            seq=version.seq,
        )
        return {}
    controls = dict(manifest.contract.controls) if manifest.contract else {}
    found: dict[str, dict[str, Any] | None] = {}
    for control_id, control in controls.items():
        if control.on_failure is None:
            found[control_id] = None
            continue
        document = control.on_failure.model_dump(mode="json", exclude_none=True)
        # A safe value the manifest sends by the control's own action names it here.
        sent = control.on_failure.action_of(control.action)
        if sent:
            document["action"] = sent
        found[control_id] = document
    return found


def action_or_404(services: Services, action_id: str, version: VersionRef) -> dict[str, Any]:
    found = catalog.find(services.twin, action_id, version)
    if found is None:
        log.info("no such action type", action=action_id, seq=version.seq)
        raise ApiError(
            f"no action type {action_id!r} at version #{version.seq}", code="action.not_found"
        )
    return found


def head_after(services: Services) -> VersionRef:
    return services.store.head()


# -- what is told ------------------------------------------------------------------------


def _event_data(record: Mapping[str, Any], action: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "record": record.get("uid"),
        "action": record.get("action"),
        "object": record.get("object"),
        "status": record.get("status"),
        "source": record.get("source"),
        "params": record.get("params"),
        "reasons": record.get("reasons"),
        "fidelity": record.get("fidelity"),
        "control": (action or {}).get("control"),
        "autonomy": (action or {}).get("autonomy"),
    }


def tell(
    services: Services, record: Mapping[str, Any], action: Mapping[str, Any] | None, at: datetime
) -> str | None:
    """Tell what a decision means for others: a supervised proposal the twin could
    execute waits for an approval, or the interlock blocked an action. The type told."""
    status = record.get("status")
    told: str | None = None
    if status == BLOCKED_STATUS:
        told = BLOCKED
    elif (
        status == PROPOSED
        and action is not None
        and action.get("autonomy") == SUPERVISED
        and gate.executable(action, services.executor is not None)
    ):
        told = APPROVAL_PENDING
    if told is None:
        return None
    try:
        services.notifier.emit(told, record.get("uid"), _event_data(record, action), time=at)
    except Exception as exc:
        # The record is written; a notice that cannot be queued is not a reason to undo.
        log.error("an action's notice could not be queued", exc=exc, record=record.get("uid"))
        return None
    log.info("action notice queued", type=told, record=record.get("uid"))
    return told


# -- proposals and decisions -------------------------------------------------------------


def propose(
    services: Services,
    action_id: str,
    *,
    object_uid: str,
    params: Mapping[str, Any],
    reason: str | None,
    source: str,
    key: str | None,
) -> tuple[dict[str, Any], VersionRef]:
    """A proposal judged with the state, the unresolved incidents and the clock of this
    moment; the record (with `created`) and the head once it is decided."""
    started = time.perf_counter()
    head = services.store.head()
    now = clock_now(services, head)
    state, incidents = standing_at(services, head)
    made = actions.propose(
        services.twin,
        action_id,
        object=object_uid,
        params=params,
        reason=reason,
        source=source,
        state=state,
        incidents=incidents,
        now=now,
        executor=services.executor,
        key=key,
    )
    if made.get("created"):
        tell(services, made, catalog.find(services.twin, made["action"]), now)
    log.info(
        "proposal answered",
        record=made["uid"],
        action=made["action"],
        status=made["status"],
        created=made.get("created"),
        state=state,
        unresolved=[found["kind"] for found in incidents],
        keyed=key is not None,
        elapsed_ms=_ms(started),
    )
    return made, head_after(services)


def approve(
    services: Services, uid: str, *, by: str, note: str | None
) -> tuple[dict[str, Any], VersionRef]:
    head = services.store.head()
    now = clock_now(services, head)
    state = state_at(services, head)
    made = actions.approve(
        services.twin, uid, by=by, note=note, state=state, now=now, executor=services.executor
    )
    tell(services, made, catalog.find(services.twin, made["action"]), now)
    return made, head_after(services)


def reject(
    services: Services, uid: str, *, by: str, note: str | None
) -> tuple[dict[str, Any], VersionRef]:
    head = services.store.head()
    now = clock_now(services, head)
    made = actions.reject(services.twin, uid, by=by, note=note, now=now)
    return made, head_after(services)


# -- reconciliation ----------------------------------------------------------------------


def _incident_detail(found: actions.Reconciled) -> dict[str, Any]:
    return {
        "record": found.record,
        "action": found.action,
        "control": found.control,
        "object": found.object,
        "target": found.target,
        "value": found.value,
        "observed": found.observed,
        "observed_at": iso(found.observed_at) if found.observed_at else None,
        "observed_source": found.observed_source,
        "tolerance": found.tolerance,
        "effective_at": iso(found.effective_at),
    }


def _when(text: Any) -> datetime | None:
    if isinstance(text, datetime):
        return text
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _about(services: Services, incident: Mapping[str, Any]) -> tuple[Any, Any, datetime | None]:
    """The control, object and effective time of the record a reconcile incident is on:
    from what it was opened with, else from the record itself."""
    opened = (incident.get("detail") or {}).get("opened") or {}
    if opened.get("control") and opened.get("object"):
        return opened["control"], opened["object"], _when(opened.get("effective_at"))
    try:
        record = actions.record(services.twin, str(incident.get("subject")))
    except actions.ActionError:
        log.warn(
            "a reconcile incident is on no action record; it is left as it is",
            incident=incident.get("uid"),
            subject=incident.get("subject"),
        )
        return None, None, None
    action = catalog.find(services.twin, record["action"]) if record["action"] else None
    return (action or {}).get("control"), record["object"], record.get("effective_at")


def _earlier_incidents(services: Services, found: actions.Reconciled) -> list[dict[str, Any]]:
    """The open reconcile incidents of earlier records on the same control and object."""
    matching = []
    for incident in fidelity.incidents(services.twin, kind=RECONCILE, status="unresolved"):
        if incident.get("subject") == found.record:
            continue
        control, object_uid, effective = _about(services, incident)
        if control != found.control or object_uid != found.object:
            continue
        if effective is not None and effective > found.effective_at:
            continue
        matching.append(incident)
    return matching


def reconcile(services: Services) -> tuple[dict[str, Any], VersionRef]:
    """Compare what is due with what is observed now; open an incident for each mismatch
    and close those an action that had its effect puts right; tell both. The answer and
    the head once it is written."""
    started = time.perf_counter()
    head = services.store.head()
    now = clock_now(services, head)
    settled = actions.reconcile(services.twin, now=now)
    outcomes: list[dict[str, Any]] = []
    opened: list[str] = []
    closed: list[str] = []
    told: list[dict[str, Any]] = []
    # In the order the actions took effect, so a later one closes what an earlier opened.
    for found in sorted(settled, key=lambda item: (item.effective_at, item.record)):
        document = {**found.as_dict(), "incidents": []}
        events: list[dict[str, Any]] = []
        try:
            if found.outcome == MISMATCH_OUTCOME:
                answered = fidelity.open_incident(
                    services.twin, RECONCILE, found.record, _incident_detail(found), now=now
                )
                document["incidents"].append(answered["uid"])
                if answered.get("version") is not None:
                    opened.append(answered["uid"])
                events.extend(answered.get("events") or [])
                events.append(
                    {
                        "type": MISMATCH,
                        "subject": found.record,
                        "data": {**_incident_detail(found), "incident": answered["uid"]},
                    }
                )
            else:
                for incident in _earlier_incidents(services, found):
                    answered = fidelity.close_incident(
                        services.twin,
                        RECONCILE,
                        str(incident["subject"]),
                        by=RECONCILER,
                        note=f"{found.record} on the same control and object had its effect",
                        now=now,
                        detail={"record": found.record, "observed": found.observed},
                    )
                    if answered is None:
                        continue
                    document["incidents"].append(answered["uid"])
                    closed.append(answered["uid"])
                    events.extend(answered.get("events") or [])
        except DtaError as exc:
            # The outcome is recorded on the action; only its incident is missing.
            log.error(
                "the incident of a reconciled action could not be kept",
                exc=exc,
                code=exc.code,
                record=found.record,
                outcome=found.outcome,
            )
            if found.outcome == MISMATCH_OUTCOME:
                events.append(
                    {
                        "type": MISMATCH,
                        "subject": found.record,
                        "data": {**_incident_detail(found), "incident": None},
                    }
                )
        emit(services, events, now)
        told.extend(events)
        outcomes.append(document)
    version = head_after(services)
    log.info(
        "reconciliation answered",
        reconciled=len(outcomes),
        mismatch=sum(1 for found in settled if found.outcome == MISMATCH_OUTCOME),
        opened=len(opened),
        closed=len(closed),
        now=iso(now),
        elapsed_ms=_ms(started),
    )
    document = {
        "data": outcomes,
        "opened": opened,
        "closed": closed,
        "events": told,
        "now": now,
        "version": version_json(version),
    }
    return document, version


class KeyedAnswers:
    """The answers given to keyed requests that are answered at once, so that asking
    again with the same key gives the same answer and does nothing again."""

    def __init__(self, kept: int = ANSWERS_KEPT) -> None:
        self._kept = kept
        self._lock = threading.Lock()
        self._answers: OrderedDict[str, Any] = OrderedDict()
        # Held while a request is answered, so two requests with one key do not both act.
        self.working = threading.Lock()

    def get(self, key: str | None) -> Any:
        if key is None:
            return None
        with self._lock:
            found = self._answers.get(key)
            if found is not None:
                self._answers.move_to_end(key)
            return found

    def keep(self, key: str | None, answer: Any) -> None:
        if key is None:
            return
        with self._lock:
            self._answers[key] = answer
            while len(self._answers) > self._kept:
                self._answers.popitem(last=False)


# -- control loops -----------------------------------------------------------------------


def loop_runnable(services: Services, loop_id: str) -> dict[str, Any]:
    """The loop, refused before any job is made when it is not enabled."""
    found = actions.control_loop(services.twin, loop_id)
    if found["status"] != ACTIVE or not found["enabled"]:
        log.info(
            "loop run refused: the loop is not enabled",
            loop=found["uid"],
            status=found["status"],
        )
        raise refused(LOOP_DISABLED, f"{found['uid']} is not enabled", loop=found["uid"])
    return found


def policy_for(services: Services) -> actions.Policy:
    """Runs a loop's policy through the worker as a function run does: the function the
    registry holds now, reading the head, writing nothing."""
    from . import registry, running

    def policy(function: str, *, deadline_s: float) -> Mapping[str, Any]:
        head = services.store.head()
        record = registry.function_or_404(services, function, head)
        log.debug("loop policy asked", function=function, deadline_s=deadline_s)
        ran = running.run(services, record, head, {}, known=head, write=False)
        return ran.answer.value  # type: ignore[return-value]

    return policy


def _tell_run(services: Services, run: Mapping[str, Any], at: datetime) -> list[str]:
    """Tell what the proposals of a loop's run came to that others should know of."""
    told: list[str] = []
    wanted = [
        found
        for found in run.get("records") or []
        if found.get("uid") and found.get("status") in (BLOCKED_STATUS, PROPOSED)
    ]
    if not wanted:
        return told
    kinds = {found["id"]: found for found in actions.types(services.twin)}
    for found in wanted:
        try:
            record = actions.record(services.twin, found["uid"])
        except DtaError as exc:
            log.error("a record of a loop's run cannot be read to tell", exc=exc, record=found)
            continue
        kind = tell(services, record, kinds.get(record["action"]), at)
        if kind is not None:
            told.append(kind)
    return told


def loop_work(services: Services, loop_id: str, key: str, now: datetime) -> Work:
    """One run of a loop: its policy asked, each action it names proposed with the state
    and the unresolved incidents the head holds when the run starts."""

    def work() -> Outcome:
        started = time.perf_counter()
        head = services.store.head()
        state, incidents = standing_at(services, head)
        try:
            run = actions.run_loop(
                services.twin,
                loop_id,
                policy=policy_for(services),
                state=state,
                incidents=incidents,
                now=now,
                executor=services.executor,
                key=key,
            )
        except actions.ActionError as exc:
            if exc.code in (POLICY_FAILED, BAD_POLICY_ANSWER):
                # The failed run is on the loop's record; the job carries it too.
                return Outcome(
                    result=dict(exc.detail.get("run") or {}, loop=bare(exc.detail.get("loop"))),
                    error={"code": exc.code, "message": exc.message},
                )
            raise
        if not run.get("repeated"):
            run = {**run, "told": _tell_run(services, run, now)}
        log.info(
            "loop run job finished",
            loop=loop_id,
            key=key,
            status=run.get("status"),
            repeated=bool(run.get("repeated")),
            elapsed_ms=_ms(started),
        )
        return Outcome(result=run)

    return work


def run_key(loop_id: str, key: str | None) -> str:
    """What names a loop's run: the caller's key, or one of its own for a run asked
    for without one."""
    return key if key is not None else f"loop:{loop_id}:{uuid.uuid4().hex}"
