"""Control loops: a policy function asked, on an interval, what to do about the
controls a loop drives.

A loop is registered from the manifest disabled, and runs only once it is enabled. A
run asks the policy through the runner it is handed, checks the answer whole, and puts
each action it names through `propose` with the loop as the source: bounds, the
interlock, execution and records are this package's, never the policy's. The loop's
record keeps what its last run did, in the same version as the run's last proposal.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.store.model import iso_utc

from ..twin import Twin
from . import catalog
from .errors import (
    BAD_POLICY_ANSWER,
    LOOP_DISABLED,
    LOOP_NOT_FOUND,
    LOOP_ORPHANED,
    POLICY_FAILED,
    ActionError,
    refused,
)
from .errors import (
    REJECTED as REJECTED_CODE,
)
from .executor import Executor
from .proposals import Tail, check_state, propose_with
from .records import (
    APPROVED,
    BLOCKED,
    DISABLED,
    EXECUTED,
    FAILED,
    FUNCTION_TYPE,
    LOCKS,
    LOOP_TYPE,
    PROPOSED,
    REJECTED,
    bare,
    canonical,
    from_json,
    function_uid,
    get,
    loop_uid,
    utc,
    write,
)

log = Log("runtime.actions")

# The most actions one answer of a policy may name.
MAX_ACTIONS = 20
# How long a policy is given when its function states no latency budget.
DEFAULT_DEADLINE_S = 30.0
SUCCEEDED = "succeeded"
_ANSWER_KEYS = {"actions", "notes"}
_ACTION_KEYS = {"action", "object", "params", "reason"}


class Policy(Protocol):
    """Runs a loop's policy function; given by the caller."""

    def __call__(self, function: str, *, deadline_s: float) -> Mapping[str, Any]:
        """The function's answer, `{"actions": [{"action", "object", "params", "reason"}],
        "notes": [...]}`, within `deadline_s` seconds. `function` is the function's id."""
        ...


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def loop_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """A `TwinControlLoop` record with its last run parsed and its policy by id."""
    return {
        "id": bare(row["uid"]),
        "uid": row["uid"],
        "description": row.get("description"),
        "controls": list(row.get("controls") or []),
        "policy": bare(row.get("policy")),
        "interval": row.get("interval"),
        "origin": row.get("origin"),
        "status": row.get("status"),
        "enabled": bool(row.get("enabled")),
        "last_run_at": utc(row.get("last_run_at")),
        "last_run": from_json(row.get("last_run")),
    }


def loops(twin: Twin, at: Any = None) -> list[dict[str, Any]]:
    """Every control loop at a version of the live line (the head by default), by id."""
    if not twin.ontology.has_type(LOOP_TYPE):
        return []
    table = twin.store.table(LOOP_TYPE, at=at or "head", ontology=twin.ontology)
    return sorted((loop_document(row) for row in table.to_pylist()), key=lambda d: d["id"])


def control_loop(twin: Twin, loop_id: str, at: Any = None) -> dict[str, Any]:
    """One control loop by id or uid; refused when there is none."""
    uid = loop_uid(loop_id)
    found = get(twin, uid, LOOP_TYPE, at or "head")
    if found is None:
        log.warn("no such control loop", loop=uid)
        raise refused(LOOP_NOT_FOUND, f"there is no control loop {loop_id!r}", loop=uid)
    return loop_document(found)


def _switch(twin: Twin, loop_id: str, enabled: bool, by: str, now: datetime) -> dict[str, Any]:
    uid = loop_uid(loop_id)
    said = "enabled" if enabled else "disabled"
    with LOCKS.hold(uid):
        found = control_loop(twin, uid)
        if enabled and found["status"] == DISABLED:
            log.warn("a control loop the manifest no longer declares cannot be enabled", loop=uid)
            raise refused(
                LOOP_ORPHANED,
                f"{uid} is no longer declared in the manifest and cannot be enabled",
                loop=uid,
            )
        if found["enabled"] == enabled:
            log.info(f"the control loop is {said} already; nothing written", loop=uid, by=by)
            return found
        write(
            twin,
            {LOOP_TYPE: [{"uid": uid, "enabled": enabled}]},
            now=now,
            cause="registration",
            note=f"control loop {found['id']} {said} by {by}",
            extra={"loop": uid, "enabled": enabled, "by": by},
        )
    log.info(f"control loop {said}", loop=uid, by=by)
    return {**found, "enabled": enabled}


def enable(twin: Twin, loop: str, *, by: str, now: datetime) -> dict[str, Any]:
    """Let a loop run. Who enabled it is kept with the version that records it."""
    return _switch(twin, loop, True, by, now)


def disable(twin: Twin, loop: str, *, by: str, now: datetime) -> dict[str, Any]:
    return _switch(twin, loop, False, by, now)


def _error_text(exc: BaseException) -> str:
    """What the record keeps of a failure: its code and message, never a stack."""
    if isinstance(exc, DtaError):
        return f"{exc.code}: {exc.message}"
    # What code outside the twin's own errors said may hold anything; it stays in the log.
    return f"{type(exc).__name__} was raised; the cause is in the log"


def _problems(
    answer: Any, found: Mapping[str, Any], known: Mapping[str, dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Every problem of a policy's answer, the actions it names and its notes."""
    if not isinstance(answer, Mapping):
        return [f"the answer is {type(answer).__name__}, not an object"], [], []
    problems: list[str] = []
    unknown = sorted(str(name) for name in set(answer) - _ANSWER_KEYS)
    if unknown:
        problems.append(f"the answer has members it may not have: {', '.join(unknown)}")
    notes = answer.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        problems.append("notes is not a list of texts")
        notes = []
    actions = answer.get("actions")
    if not isinstance(actions, list):
        problems.append("actions is not a list")
        return problems, [], list(notes)
    if len(actions) > MAX_ACTIONS:
        problems.append(f"{len(actions)} actions; an answer names at most {MAX_ACTIONS}")
    controls = set(found["controls"])
    checked: list[dict[str, Any]] = []
    for index, item in enumerate(actions):
        where = f"actions[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where} is not an object")
            continue
        extra = sorted(str(name) for name in set(item) - _ACTION_KEYS)
        if extra:
            problems.append(f"{where} has members it may not have: {', '.join(extra)}")
        action, object_uid = item.get("action"), item.get("object")
        params, reason = item.get("params", {}), item.get("reason")
        if not isinstance(action, str) or not action:
            problems.append(f"{where}.action is not the id of an action type")
        elif bare(action) not in known:
            problems.append(f"{where}.action: there is no action type {action!r}")
        else:
            control = known[bare(action)].get("control")
            if control not in controls:
                problems.append(
                    f"{where}.action: {bare(action)} carries out "
                    f"{'no control' if not control else 'control ' + repr(control)}, not one "
                    f"of the loop's ({', '.join(sorted(controls))})"
                )
        if not isinstance(object_uid, str) or not object_uid:
            problems.append(f"{where}.object is not the uid of an object")
        if not isinstance(params, Mapping):
            problems.append(f"{where}.params is not an object")
        if reason is not None and not isinstance(reason, str):
            problems.append(f"{where}.reason is not a text")
        checked.append({"action": action, "object": object_uid, "params": params, "reason": reason})
    return problems, checked, list(notes)


def _counted(run: dict[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """The run with one more proposal's outcome."""
    found = copy.deepcopy(run)
    status = record.get("status")
    found["records"].append(
        {
            "uid": record.get("uid"),
            "action": record.get("action"),
            "object": record.get("object"),
            "status": status,
        }
    )
    if status == EXECUTED:
        found["executed"] += 1
    elif status == BLOCKED:
        found["blocked"] += 1
    elif status == REJECTED:
        found["rejected"] += 1
    elif status in (PROPOSED, APPROVED):
        found["awaiting"] += 1
    else:
        found["failed"] += 1
    return found


def _loop_row(uid: str, run: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {"uid": uid, "last_run_at": now, "last_run": canonical(run)}


def _record_run(twin: Twin, uid: str, run: Mapping[str, Any], now: datetime) -> None:
    write(
        twin,
        {LOOP_TYPE: [_loop_row(uid, run, now)]},
        now=now,
        note=f"control loop {bare(uid)} ran: {run['status']}",
        extra={"loop": uid, "run": run["key"]},
    )


def run_loop(
    twin: Twin,
    loop: str,
    *,
    policy: Policy,
    state: str,
    now: datetime,
    executor: Executor | None,
    key: str,
) -> dict[str, Any]:
    """Ask a loop's policy what to do and propose each action it names. What the run
    did: `key`, `at`, `status`, how many actions were `proposed`, and of them how many
    were `executed`, `blocked`, `rejected`, `failed` or are `awaiting` a decision, the
    `records`, the policy's `notes`, the `error` of a failed run; with `loop`, and
    `repeated` when `key` names the loop's last run, which is then given back as it is.

    A disabled loop refuses (`action.loop_disabled`). A policy that raises
    (`action.policy_failed`) or answers something that cannot be used whole
    (`action.bad_policy_answer`) fails the run before anything is proposed; the failure
    is recorded and raised. One proposal rejected or blocked does not stop the others.
    """
    started = time.perf_counter()
    check_state(state)
    uid = loop_uid(loop)
    with LOCKS.hold(uid):
        found = control_loop(twin, uid)
        if found["status"] == DISABLED or not found["enabled"]:
            log.warn(
                "a control loop that is not enabled was asked to run",
                loop=uid,
                status=found["status"],
                enabled=found["enabled"],
            )
            raise refused(LOOP_DISABLED, f"{uid} is not enabled", loop=uid)
        last = found["last_run"] if isinstance(found["last_run"], Mapping) else {}
        if last.get("key") == key:
            log.info("this run was made before; its outcome is given back", loop=uid, key=key)
            return {**last, "loop": found["id"], "repeated": True}

        function = get(twin, function_uid(found["policy"]), FUNCTION_TYPE)
        budget = (function or {}).get("latency_budget_ms")
        deadline_s = DEFAULT_DEADLINE_S if not budget else budget / 1000.0
        run: dict[str, Any] = {
            "key": key,
            "at": iso_utc(now),
            "policy": found["policy"],
            "state": state,
            "status": SUCCEEDED,
            "proposed": 0,
            "executed": 0,
            "blocked": 0,
            "rejected": 0,
            "failed": 0,
            "awaiting": 0,
            "records": [],
            "notes": [],
            "error": None,
        }
        context = {"loop": uid, "policy": found["policy"], "key": key}
        log.info("control loop run started", **context, state=state, deadline_s=deadline_s)
        asked = time.perf_counter()
        try:
            answer = policy(found["policy"], deadline_s=deadline_s)
        except Exception as exc:
            run.update(status=FAILED, error=_error_text(exc))
            log.error(
                "the loop's policy failed; nothing is proposed",
                exc=exc,
                **context,
                elapsed_ms=_ms(asked),
            )
            _record_run(twin, uid, run, now)
            raise refused(
                POLICY_FAILED,
                f"{uid}: its policy {found['policy']} failed: {run['error']}",
                loop=uid,
                run=run,
            ) from exc
        log.info("the loop's policy answered", **context, elapsed_ms=_ms(asked))
        known = {action["id"]: action for action in catalog.types(twin)}
        problems, actions, notes = _problems(answer, found, known)
        run["notes"] = notes
        if problems:
            run.update(status=FAILED, error="; ".join(problems))
            log.error(
                "the loop's policy answered something that cannot be used; nothing is proposed",
                **context,
                problems=problems,
            )
            _record_run(twin, uid, run, now)
            raise refused(
                BAD_POLICY_ANSWER,
                f"{uid}: the answer of its policy cannot be used: {problems[0]}"
                + ("" if len(problems) == 1 else f" (and {len(problems) - 1} more)"),
                loop=uid,
                problems=problems,
                run=run,
            )

        run["proposed"] = len(actions)
        tail: Tail | None = None
        for index, item in enumerate(actions):
            final: dict[str, Any] = {}
            if index == len(actions) - 1:
                before = run

                def make(record: dict[str, Any], before: dict = before, final: dict = final):
                    final["run"] = _counted(before, record)
                    return {LOOP_TYPE: [_loop_row(uid, final["run"], now)]}

                tail = Tail(make=make)
            outcome: dict[str, Any]
            try:
                made = propose_with(
                    twin,
                    item["action"],
                    object_uid=item["object"],
                    params=item["params"],
                    reason_text=item["reason"],
                    source=uid,
                    state=state,
                    now=now,
                    executor=executor,
                    key=f"{key}:{index}",
                    tail=tail if index == len(actions) - 1 else None,
                )
                outcome = made
            except ActionError as exc:
                status = REJECTED if exc.code == REJECTED_CODE else FAILED
                outcome = {
                    "uid": exc.detail.get("record"),
                    "action": bare(item["action"]),
                    "object": item["object"],
                    "status": status,
                }
                log.warn(
                    "a proposal of the loop was not accepted; the others go on",
                    **context,
                    index=index,
                    code=exc.code,
                )
            except Exception as exc:
                outcome = {
                    "uid": None,
                    "action": bare(item["action"]),
                    "object": item["object"],
                    "status": FAILED,
                }
                log.error(
                    "a proposal of the loop could not be made; the others go on",
                    exc=exc,
                    **context,
                    index=index,
                )
            if tail is not None and tail.applied:
                run = final["run"]
            else:
                run = _counted(run, outcome)
        if tail is None or not tail.applied:
            _record_run(twin, uid, run, now)
    log.info(
        "control loop run finished",
        **context,
        proposed=run["proposed"],
        executed=run["executed"],
        blocked=run["blocked"],
        rejected=run["rejected"],
        failed=run["failed"],
        awaiting=run["awaiting"],
        elapsed_ms=_ms(started),
    )
    return {**run, "loop": found["id"]}
