"""Proposing an action, deciding on it, sending it, and reading what became of it.

A proposal is judged at the head of the live line: its object, its parameters and the
bounds of its control, then the interlock, with the twin's fidelity state and the
incidents it has not resolved as the caller read them. Whatever the judgment, it is
recorded, with the state and, when blocked, why. A
record is always written before the external call is made, and the outcome of the call
is a second version, so nothing is sent without a trace; a record left `approved`
without an outcome (the process stopped in between) is sent again, with the same
idempotency key, by the next proposal under the same key or the next approval.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dta_core.log import Log
from dta_core.manifest.checks.filters import precondition_clauses
from dta_core.store import InvalidInput, StoredObject, UnknownProperty, VersionRef

from ..filters import parse_where
from ..twin import Twin
from . import catalog, gate
from .checks import judge, same
from .errors import (
    BAD_STATE,
    KEY_REUSED,
    NOT_EXECUTABLE,
    NOT_PENDING,
    RECORD_NOT_FOUND,
    reason,
    refused,
    rejected,
)
from .executor import Execution, Executor
from .records import (
    ACTIVE,
    APPROVED,
    BLOCKED,
    EXECUTED,
    FAILED,
    INCIDENT_KINDS,
    LOCKS,
    PENDING,
    PROPOSED,
    RECORD_TYPE,
    REJECTED,
    STATES,
    action_uid,
    bare,
    canonical,
    get,
    keyed_id,
    new_id,
    record_document,
    record_uid,
    write,
)

log = Log("runtime.actions")


def _unmet_precondition(
    twin: Twin, document: Mapping[str, Any], stored: StoredObject, head: VersionRef
) -> list[dict[str, str]]:
    """The reason a proposal on `stored` fails the action's precondition at `head`; none
    when the action has no precondition or the object meets it. A precondition that cannot
    be read refuses the proposal: a guard that cannot be checked does not hold."""
    text = document.get("precondition")
    if not text:
        return []
    clauses = precondition_clauses(str(text))
    try:
        fields = {field.name: field for field in twin.ontology.type(stored.type).fields}
        triples = parse_where(clauses, fields, stored.type)
        met = twin.store.table(
            stored.type, columns=["uid"], filter=[("uid", "=", stored.uid), *triples], at=head
        ).num_rows
    except (InvalidInput, UnknownProperty, KeyError) as exc:
        log.error(
            "an action's precondition cannot be evaluated: the proposal is refused",
            action=document.get("id"),
            object=stored.uid,
            precondition=str(text),
            exc=exc,
        )
        return [reason("precondition", f"{text!r} cannot be evaluated: {exc}")]
    if met:
        return []
    log.info(
        "proposal refused: the object does not meet the action's precondition",
        action=document.get("id"),
        object=stored.uid,
        precondition=str(text),
        version=head.id,
    )
    return [reason("precondition", f"{stored.uid} does not meet {text!r} at this version")]


Rows = Mapping[str, list[dict[str, Any]]]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def check_state(state: str) -> None:
    if state not in STATES:
        log.error("a fidelity state that does not exist was handed in", state=str(state))
        raise refused(
            BAD_STATE,
            f"{state!r} is not a fidelity state ({', '.join(STATES)})",
            state=str(state),
        )


def check_incidents(incidents: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The incidents not resolved as the gate reads them, `{"kind", "uid"}` each; refused
    when one is not an incident of a kind the twin has."""
    if isinstance(incidents, str | bytes | Mapping):
        log.error("the incidents handed in are not a list", given=type(incidents).__name__)
        raise refused(BAD_STATE, "the incidents are a list of {kind, uid}")
    found: list[dict[str, str]] = []
    for index, incident in enumerate(incidents):
        kind = incident.get("kind") if isinstance(incident, Mapping) else None
        uid = incident.get("uid") if isinstance(incident, Mapping) else None
        if kind not in INCIDENT_KINDS or not isinstance(uid, str) or not uid:
            log.error(
                "an incident handed in is not one the twin could have",
                index=index,
                kind=str(kind),
                uid=str(uid),
            )
            raise refused(
                BAD_STATE,
                f"incidents[{index}] is not {{kind, uid}} with a kind of "
                f"{', '.join(INCIDENT_KINDS)}",
                index=index,
            )
        found.append({"kind": kind, "uid": uid})
    return found


def _plain(value: Any) -> Any:
    """A value JSON can hold: a number that is not finite as null, anything unknown as text."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(name): _plain(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return str(value)


def _json_text(value: Any) -> str:
    try:
        return canonical(value)
    except (TypeError, ValueError):
        return canonical(_plain(value))


@dataclass
class Tail:
    """Rows that go into the version holding a proposal's last write: `make` is given the
    record as it then is. `applied` tells whether they were written."""

    make: Callable[[dict[str, Any]], Rows]
    applied: bool = False


def _commit(
    twin: Twin,
    row: dict[str, Any],
    *,
    now: datetime,
    document: dict[str, Any],
    tail: Tail | None,
    note: str,
    extra: Mapping[str, Any],
) -> VersionRef:
    upserts: dict[str, list[dict[str, Any]]] = {RECORD_TYPE: [row]}
    if tail is not None:
        for type_name, found in tail.make(document).items():
            upserts.setdefault(type_name, []).extend(found)
    version = write(twin, upserts, now=now, note=note, extra=extra)
    if tail is not None:
        tail.applied = True
    return version


def _execution_document(execution: Execution, now: datetime, key: str) -> dict[str, Any]:
    effective = execution.effective_at if execution.effective_at is not None else now
    return {
        "at": now,
        "effective_at": effective if execution.status == EXECUTED else None,
        "http_status": execution.http_status,
        "excerpt": execution.excerpt,
        "duration_ms": execution.duration_ms,
        "attempts": execution.attempts,
        "error": execution.error,
        "idempotency_key": key,
    }


def _execute(
    twin: Twin,
    action: dict[str, Any],
    record: dict[str, Any],
    *,
    executor: Executor,
    now: datetime,
    tail: Tail | None,
) -> dict[str, Any]:
    """Make the call of an `approved` record and record its outcome as one version."""
    uid = record["uid"]
    try:
        execution = executor(action, record)
    except Exception as exc:
        log.error(
            "the executor raised; the action is recorded as failed",
            exc=exc,
            record=uid,
            action=action["id"],
        )
        execution = Execution(FAILED, error=f"the executor raised {type(exc).__name__}")
    status = EXECUTED if execution.status == EXECUTED else FAILED
    detail = _execution_document(execution, now, record["id"])
    outcome = {
        "uid": uid,
        "status": status,
        "execution": canonical(detail),
        "effective_at": detail["effective_at"],
        # Only the change of a control can be looked for afterwards.
        "reconciliation": PENDING if status == EXECUTED and action.get("control") else None,
    }
    merged = {**record, **record_document(uid, {**_row_of(record), **outcome})}
    _commit(
        twin,
        outcome,
        now=now,
        document=merged,
        tail=tail,
        note=f"action {action['id']} on {record['object']}: {status}",
        extra={"record": uid, "status": status},
    )
    if status == EXECUTED:
        log.info(
            "action executed",
            record=uid,
            action=action["id"],
            object=record["object"],
            http_status=execution.http_status,
            attempts=execution.attempts,
            effective_at=detail["effective_at"],
            duration_ms=execution.duration_ms,
        )
    else:
        log.error(
            "action failed",
            record=uid,
            action=action["id"],
            object=record["object"],
            http_status=execution.http_status,
            attempts=execution.attempts,
            error=execution.error,
            duration_ms=execution.duration_ms,
        )
    return merged


def _row_of(document: Mapping[str, Any]) -> dict[str, Any]:
    """The stored row of a record document, as `record_document` reads it back."""

    def text(name: str) -> Any:
        value = document.get(name)
        return None if value is None else canonical(value)

    return {
        "action": action_uid(document["action"]) if document.get("action") else None,
        "object": document.get("object"),
        "params": canonical(document.get("params") or {}),
        "reason": document.get("reason"),
        "source": document.get("source"),
        "proposed_at": document.get("proposed_at"),
        "at_version": document.get("at_version"),
        "fidelity": document.get("fidelity"),
        "status": document.get("status"),
        "reasons": text("reasons") if document.get("reasons") else None,
        "approval": text("approval"),
        "execution": text("execution"),
        "effective_at": document.get("effective_at"),
        "reconciliation": document.get("reconciliation"),
        "observed": text("observed"),
        "reconciled_at": document.get("reconciled_at"),
    }


def _interrupted(record: Mapping[str, Any]) -> bool:
    return record.get("status") == APPROVED and record.get("execution") is None


def _resume(
    twin: Twin,
    record: dict[str, Any],
    *,
    executor: Executor | None,
    now: datetime,
    tail: Tail | None,
) -> dict[str, Any]:
    """Send a record that was cleared to go and has no outcome: the process that cleared
    it stopped before recording one, and may or may not have sent it."""
    action = catalog.find(twin, record["action"])
    if executor is None or action is None:
        log.warn(
            "an approved action has no outcome and nothing can send it now; it stays approved",
            record=record["uid"],
            action=record["action"],
            has_executor=executor is not None,
        )
        return record
    log.warn(
        "an approved action has no outcome: the process stopped before recording one; "
        "sending it again with the same idempotency key",
        record=record["uid"],
        action=record["action"],
    )
    return _execute(twin, action, record, executor=executor, now=now, tail=tail)


def _same_proposal(
    record: Mapping[str, Any], action: str, object_uid: Any, params: Any, source: str
) -> bool:
    if record["action"] != bare(action_uid(action)) or record["object"] != object_uid:
        return False
    if record["source"] != source or not isinstance(params, Mapping):
        return False
    stored = record["params"] or {}
    return set(stored) == set(params) and all(same(stored[name], params[name]) for name in params)


def _again(
    twin: Twin,
    record: dict[str, Any],
    *,
    action: str,
    object_uid: Any,
    params: Any,
    source: str,
    key: str,
    executor: Executor | None,
    now: datetime,
    tail: Tail | None,
) -> dict[str, Any]:
    """A proposal under a key already used: the record made for it, completed when its
    call never got an outcome. Anything else is refused."""
    if not _same_proposal(record, action, object_uid, params, source):
        log.warn(
            "a proposal key was used again for another proposal",
            record=record["uid"],
            action=action,
            source=source,
        )
        raise refused(
            KEY_REUSED,
            "this key was used before for another proposal",
            record=record["uid"],
            key=key,
        )
    log.info(
        "a proposal was made again under its key; its record is given back",
        record=record["uid"],
        status=record["status"],
    )
    if record["status"] == REJECTED:
        raise rejected(record["reasons"] or [reason("action", "rejected")], record=record["uid"])
    if _interrupted(record):
        record = _resume(twin, record, executor=executor, now=now, tail=tail)
    return {**record, "created": False}


def propose(
    twin: Twin,
    action: str,
    *,
    object: str,
    params: Mapping[str, Any],
    reason: str | None,
    source: str,
    state: str,
    incidents: Iterable[Mapping[str, Any]],
    now: datetime,
    executor: Executor | None,
    key: str | None = None,
) -> dict[str, Any]:
    """Judge a proposal of `action` on `object` at the head, record it and, when the
    interlock lets it through, send it. The record, with `created` (false when `key`
    gave back the record made for it earlier).

    A proposal that cannot be carried out as asked is recorded as `rejected` and raised
    as `action.rejected`. `source` is who proposes: a name, or a control loop's uid.
    `state` is the twin's fidelity state and `incidents` the `kind` and `uid` of every
    incident it has not resolved, read at one version; `executor` what makes external
    calls, None when the executors add-on is off. The same `key` gives the same record
    back and does nothing again.
    """
    return propose_with(
        twin,
        action,
        object_uid=object,
        params=params,
        reason_text=reason,
        source=source,
        state=state,
        incidents=incidents,
        now=now,
        executor=executor,
        key=key,
        tail=None,
    )


def propose_with(
    twin: Twin,
    action: str,
    *,
    object_uid: Any,
    params: Any,
    reason_text: str | None,
    source: str,
    state: str,
    incidents: Iterable[Mapping[str, Any]],
    now: datetime,
    executor: Executor | None,
    key: str | None,
    tail: Tail | None,
    interlock: bool = True,
) -> dict[str, Any]:
    """`propose`, with rows of the caller's own put into the version of its last write.

    With `interlock` false the proposal is executed whatever the autonomy of its control,
    the state and the incidents, and `executor` must be given: it is what a control sends
    when the twin can no longer vouch for itself, and a fail-safe that waited for the twin
    to be trusted again would come too late. The object, the parameters, the bounds and
    the precondition are still judged; a refusal is still recorded and raised."""
    started = time.perf_counter()
    if not interlock and executor is None:
        raise ValueError("an action sent past the interlock needs something to make its call")
    check_state(state)
    unresolved = check_incidents(incidents)
    uid = record_uid(keyed_id(key) if key is not None else new_id())
    with LOCKS.hold(uid):
        store = twin.store
        head = store.head()
        if key is not None:
            found = get(twin, uid, RECORD_TYPE, head)
            if found is not None:
                return _again(
                    twin,
                    record_document(uid, found),
                    action=action,
                    object_uid=object_uid,
                    params=params,
                    source=source,
                    key=key,
                    executor=executor,
                    now=now,
                    tail=tail,
                )
        document = catalog.find(twin, action, head) if isinstance(action, str) and action else None
        if document is None:
            log.warn(
                "proposal refused: there is no such action type, so no record can name it",
                action=str(action),
                object=str(object_uid),
                source=source,
            )
            raise rejected(
                [reason("action", f"there is no action type {action!r}")],
                record=None,
                action=str(action),
                object=str(object_uid),
            )
        stored = (
            store.get(object_uid, at=head) if isinstance(object_uid, str) and object_uid else None
        )
        judged = judge(
            twin.ontology, document, object_uid=str(object_uid), stored=stored, params=params
        )
        if stored is not None and not judged.reasons:
            judged.reasons.extend(_unmet_precondition(twin, document, stored, head))
        if document["status"] != ACTIVE:
            judged.reasons.insert(
                0,
                reason(
                    "action",
                    f"{document['id']} is disabled: the manifest no longer declares it",
                ),
            )
        row: dict[str, Any] = {
            "uid": uid,
            "action": document["uid"],
            "object": str(object_uid),
            "params": _json_text(judged.params if judged.params else params or {}),
            "reason": reason_text,
            "source": source,
            "proposed_at": now,
            "at_version": head.id,
            "fidelity": state,
            "reasons": None,
            "approval": None,
            "execution": None,
            "effective_at": None,
            "reconciliation": None,
            "observed": None,
            "reconciled_at": None,
        }
        extra = {"record": uid, "key": key}
        context = {
            "record": uid,
            "action": document["id"],
            "object": str(object_uid),
            "source": source,
            "state": state,
        }
        if judged.reasons:
            row.update(status=REJECTED, reasons=canonical(judged.reasons))
            made = record_document(uid, row)
            _commit(
                twin,
                row,
                now=now,
                document=made,
                tail=tail,
                note=f"action {document['id']} on {object_uid}: {REJECTED}",
                extra={**extra, "status": REJECTED},
            )
            log.warn(
                "proposal rejected",
                **context,
                reasons=judged.reasons,
                elapsed_ms=_ms(started),
            )
            raise rejected(judged.reasons, record=uid, action=document["id"], object=object_uid)

        decision = (
            gate.on_proposal(
                document, has_executor=executor is not None, state=state, incidents=unresolved
            )
            if interlock
            else gate.EXECUTE
        )
        blocking = [found["uid"] for found in gate.blocking(unresolved)]
        if decision == gate.BLOCK:
            row.update(
                status=BLOCKED,
                reasons=canonical(
                    gate.blocked_reasons(state, approval=False, incidents=unresolved)
                ),
            )
        else:
            row["status"] = PROPOSED if decision == gate.PROPOSE else APPROVED
        made = record_document(uid, row)
        final = decision != gate.EXECUTE
        _commit(
            twin,
            row,
            now=now,
            document=made,
            tail=tail if final else None,
            note=f"action {document['id']} on {object_uid}: {row['status']}",
            extra={**extra, "status": row["status"]},
        )
        if decision == gate.BLOCK:
            log.warn(
                "proposal blocked by the interlock",
                **context,
                blocking=blocking,
                unresolved=len(unresolved),
                elapsed_ms=_ms(started),
            )
        elif decision == gate.PROPOSE:
            log.info(
                "proposal recorded; it is not executed by the twin",
                **context,
                autonomy=document["autonomy"],
                binding=document["binding"],
                has_executor=executor is not None,
                elapsed_ms=_ms(started),
            )
        else:
            assert executor is not None
            log.info(
                "proposal cleared to execute; sending it"
                if interlock
                else "action sent past the interlock and the autonomy of its control",
                **context,
                unresolved=len(unresolved),
            )
            made = _execute(twin, document, made, executor=executor, now=now, tail=tail)
            log.info(
                "proposal decided",
                **context,
                status=made["status"],
                elapsed_ms=_ms(started),
            )
        return {**made, "created": True}


def _pending(uid: str, record: Mapping[str, Any], wanted: str) -> None:
    if record["status"] != PROPOSED:
        log.info(
            f"the record is not awaiting a decision; it is not {wanted}",
            record=uid,
            status=record["status"],
        )
        raise refused(
            NOT_PENDING,
            f"{uid} is {record['status']}, not awaiting a decision",
            record=uid,
            status=record["status"],
        )


def _require(twin: Twin, uid: str, at: Any = "head") -> dict[str, Any]:
    found = get(twin, uid, RECORD_TYPE, at)
    if found is None:
        log.warn("no such action record", record=uid)
        raise refused(RECORD_NOT_FOUND, f"there is no action record {uid!r}", record=uid)
    return record_document(uid, found)


def approve(
    twin: Twin,
    record: str,
    *,
    by: str,
    note: str | None,
    state: str,
    now: datetime,
    executor: Executor | None,
) -> dict[str, Any]:
    """Approve a proposed record: it executes, or is blocked while the twin is failed.

    The approval is recorded before the call is made. A record that was approved and
    never got an outcome is sent again. Refused for a record that is not awaiting a
    decision (`action.not_pending`) and for one the twin cannot execute: advisory, or
    with nothing to make the call (`action.not_executable`).
    """
    started = time.perf_counter()
    check_state(state)
    uid = record_uid(record)
    with LOCKS.hold(uid):
        found = _require(twin, uid)
        if _interrupted(found):
            return _resume(twin, found, executor=executor, now=now, tail=None)
        _pending(uid, found, "approved")
        action = catalog.find(twin, found["action"])
        if (
            action is None
            or action["status"] != ACTIVE
            or not gate.executable(action, executor is not None)
        ):
            why = (
                "its action type is disabled"
                if action is not None and action["status"] != ACTIVE
                else "the executors add-on is off"
                if executor is None
                else "its action is advisory: the twin never executes it"
            )
            log.info("approval refused: the twin cannot execute this record", record=uid, why=why)
            raise refused(
                NOT_EXECUTABLE, f"{uid} cannot be executed by the twin: {why}", record=uid
            )
        approval = {"decision": "approved", "by": by, "note": note, "at": now, "fidelity": state}
        context = {"record": uid, "action": action["id"], "by": by, "state": state}
        if gate.on_approval(state) == gate.BLOCK:
            row = {
                "uid": uid,
                "status": BLOCKED,
                "approval": canonical(approval),
                "reasons": canonical(gate.blocked_reasons(state, approval=True)),
            }
            write(
                twin,
                {RECORD_TYPE: [row]},
                now=now,
                note=f"action {action['id']} on {found['object']}: {BLOCKED}",
                extra={"record": uid, "status": BLOCKED},
            )
            log.warn("approved action blocked by the interlock", **context, elapsed_ms=_ms(started))
            return record_document(uid, {**_row_of(found), **row})
        row = {"uid": uid, "status": APPROVED, "approval": canonical(approval)}
        write(
            twin,
            {RECORD_TYPE: [row]},
            now=now,
            note=f"action {action['id']} on {found['object']}: {APPROVED}",
            extra={"record": uid, "status": APPROVED},
        )
        log.info("action approved; sending it", **context)
        assert executor is not None
        made = _execute(
            twin,
            action,
            record_document(uid, {**_row_of(found), **row}),
            executor=executor,
            now=now,
            tail=None,
        )
        log.info("approval decided", **context, status=made["status"], elapsed_ms=_ms(started))
        return made


def reject(twin: Twin, record: str, *, by: str, note: str | None, now: datetime) -> dict[str, Any]:
    """Turn down a proposed record. Refused for a record not awaiting a decision."""
    uid = record_uid(record)
    with LOCKS.hold(uid):
        found = _require(twin, uid)
        _pending(uid, found, "rejected")
        row = {
            "uid": uid,
            "status": REJECTED,
            "approval": canonical({"decision": "rejected", "by": by, "note": note, "at": now}),
            "reasons": canonical([reason("approval", f"turned down by {by}")]),
        }
        write(
            twin,
            {RECORD_TYPE: [row]},
            now=now,
            note=f"action {found['action']} on {found['object']}: {REJECTED}",
            extra={"record": uid, "status": REJECTED},
        )
    log.info("proposal turned down", record=uid, action=found["action"], by=by)
    return record_document(uid, {**_row_of(found), **row})


# -- reading -----------------------------------------------------------------------------


def record(twin: Twin, uid: str, at: Any = None) -> dict[str, Any]:
    """One record, by uid or id, at a version of the live line (the head by default)."""
    return _require(twin, record_uid(uid), at or "head")


def records(
    twin: Twin,
    *,
    action: str | None = None,
    object: str | None = None,
    status: str | None = None,
    source: str | None = None,
    reconciliation: str | None = None,
    at: Any = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """The records at a version of the live line (the head by default) that match every
    filter given, newest proposal first."""
    started = time.perf_counter()
    if not twin.ontology.has_type(RECORD_TYPE):
        return []
    conditions: list[tuple[str, str, Any]] = []
    for name, value in (
        ("action", action_uid(action) if action else None),
        ("object", object),
        ("status", status),
        ("source", source),
        ("reconciliation", reconciliation),
    ):
        if value is not None:
            conditions.append((name, "=", value))
    table = twin.store.table(
        RECORD_TYPE,
        filter=conditions or None,
        at=at or "head",
        ontology=twin.ontology,
        order_by=[("proposed_at", "desc")],
        limit=limit,
    )
    found = [record_document(row["uid"], row) for row in table.to_pylist()]
    log.debug(
        "action records read",
        count=len(found),
        filters=[name for name, _, _ in conditions],
        elapsed_ms=_ms(started),
    )
    return found
