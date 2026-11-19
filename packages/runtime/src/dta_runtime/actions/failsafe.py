"""What a control sends to the real system when the twin can no longer vouch for itself.

A control the twin may act on says in the manifest what happens then (`on_failure`):
`hold` sends nothing, and the real system keeps the twin's last command; `release` sends
an action that hands the object back; `safe_value` sends a fixed value. It is due while
the twin's fidelity state is `failed` (trigger `failed`) and, when the control sets
`after_stale`, once an incident of data too old for a question has been open that long
(trigger `stale`).

It is sent to each object the twin commanded under the control: one an action of the
control executed on, proposed by anyone but a fail-safe, since the last fail-safe of the
control executed on it. It goes past the interlock and the autonomy of the control, which
would hold it back exactly while it is needed; the object, the parameters, the bounds
and the precondition are still judged, a refusal is recorded, and so is whatever the call
answers. With nothing to make the call, the call is recorded as failed and says why.

It is sent once per episode: the stretch the twin is not healthy, named by the time it
began. The record's key is `failsafe:<episode>:<control>:<object>`, so being due again
in the same episode gives the record back and sends nothing; once the twin has been
healthy, the next stretch is a new episode. The record's `source` is
`failsafe:<control>`; a record's origin (`failsafe`, `loop` or `proposal`) comes from its
source.

This is not a safety function: a twin that is not running sends nothing. Nothing here
reads the wall clock; `now` is the data time of the check it is sent at.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import FailureMode
from dta_core.store.model import iso_utc

from ..twin import Twin
from . import catalog
from .errors import ActionError
from .executor import Execution, Executor
from .proposals import propose_with
from .records import (
    APPROVED,
    EXECUTED,
    EXECUTOR,
    FAILED,
    FAILED_STATE,
    FAILSAFE_ORIGIN,
    FAILSAFE_PREFIX,
    LOOP_ORIGIN,
    ORIGINS,
    PROPOSAL_ORIGIN,
    RECORD_TYPE,
    get,
    keyed_id,
    origin_filter,
    origin_of,
    record_uid,
    utc,
)
from .records import REJECTED as REJECTED_STATUS

log = Log("runtime.actions")

SOURCE_PREFIX = FAILSAFE_PREFIX
# Why a fail-safe is due.
FAILED_TRIGGER = "failed"
STALE_TRIGGER = "stale"


def source_of(control: str) -> str:
    """The source of the records a control's fail-safe makes."""
    return f"{SOURCE_PREFIX}{control}"


def key_of(episode: datetime, control: str, object_uid: str) -> str:
    """What names the fail-safe of a control on an object in an episode."""
    return f"{SOURCE_PREFIX}{iso_utc(episode)}:{control}:{object_uid}"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


@dataclass(frozen=True)
class Due:
    """A control whose fail-safe is due: what it sends and why."""

    control: str
    mode: str
    action: str
    parameters: dict[str, Any]
    trigger: str
    # For the `stale` trigger: the incident that has been open long enough, and since when.
    incident: str | None = None
    opened_at: datetime | None = None

    def reason(self) -> str:
        if self.trigger == FAILED_TRIGGER:
            why = "the twin's fidelity is failed"
        else:
            opened = iso_utc(self.opened_at) if self.opened_at else "an unknown time"
            why = f"data has been too old for a question since {opened} ({self.incident})"
        return f"fail-safe of control {self.control} ({self.mode}): {why}"


@dataclass
class Sent:
    """What came of a fail-safe on one object. `status` is `executed`, `failed` (the call
    did not succeed) or `rejected` (it could not be sent as the control says); `created` is
    false when the episode's record was there already."""

    control: str
    object: str
    action: str
    mode: str
    trigger: str
    key: str
    episode: datetime
    status: str
    created: bool
    record: str | None = None
    reasons: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def executed(self) -> bool:
        return self.status == EXECUTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "control": self.control,
            "object": self.object,
            "action": self.action,
            "mode": self.mode,
            "trigger": self.trigger,
            "episode": iso_utc(self.episode),
            "record": self.record,
            "status": self.status,
            "created": self.created,
            "reasons": list(self.reasons),
            "error": self.error,
        }


def due(
    manifest: Manifest,
    *,
    state: str,
    stale: Iterable[Mapping[str, Any]],
    now: datetime,
) -> list[Due]:
    """The controls whose fail-safe is due at `now`, by id: the twin is `failed`, or one of
    the `stale` incidents not resolved (each with `uid` and `opened_at`) has been open for
    the control's `after_stale`. A control that holds is never due."""
    controls = dict(manifest.contract.controls) if manifest.contract else {}
    open_stale = sorted(
        (found for found in stale if found.get("opened_at") is not None),
        key=lambda found: (utc(found["opened_at"]), str(found.get("uid"))),
    )
    wanted: list[Due] = []
    for control_id, control in sorted(controls.items()):
        on_failure = control.on_failure
        if on_failure is None or on_failure.mode in (None, FailureMode.hold):
            continue
        incident: Mapping[str, Any] | None = None
        if state == FAILED_STATE:
            trigger = FAILED_TRIGGER
        elif on_failure.after_stale is not None:
            incident = next(
                (
                    found
                    for found in open_stale
                    if now - utc(found["opened_at"]) >= on_failure.after_stale
                ),
                None,
            )
            if incident is None:
                continue
            trigger = STALE_TRIGGER
        else:
            continue
        mode = on_failure.mode.value
        wanted.append(
            Due(
                control=control_id,
                mode=mode,
                action=on_failure.action_of(control.action) or "",
                parameters=dict(on_failure.parameters or {}),
                trigger=trigger,
                incident=None if incident is None else str(incident.get("uid")),
                opened_at=None if incident is None else utc(incident["opened_at"]),
            )
        )
        log.info(
            "a control's fail-safe is due",
            control=control_id,
            mode=mode,
            trigger=trigger,
            incident=None if incident is None else incident.get("uid"),
            now=iso_utc(now),
        )
    return wanted


def commanded(twin: Twin, control: str, at: Any = "head") -> list[str]:
    """The objects the twin commanded under a control and has not handed over since:
    each one an action of the control executed on, proposed by anything but a fail-safe,
    later than the last fail-safe of the control executed on it. Sorted."""
    started = time.perf_counter()
    if not twin.ontology.has_type(RECORD_TYPE):
        return []
    actions = [found["uid"] for found in catalog.types(twin, at=at) if found["control"] == control]
    if not actions:
        log.info("a control has no action type; it commanded nothing", control=control)
        return []
    columns = ["object", "source", "proposed_at"]
    executed = twin.store.table(
        RECORD_TYPE,
        columns=columns,
        filter=[("action", "in", actions), ("status", "=", EXECUTED)],
        at=at,
        ontology=twin.ontology,
    ).to_pylist()
    handed: dict[str, datetime] = {}
    for row in twin.store.table(
        RECORD_TYPE,
        columns=columns,
        filter=[("source", "=", source_of(control)), ("status", "=", EXECUTED)],
        at=at,
        ontology=twin.ontology,
    ).to_pylist():
        moment = utc(row["proposed_at"])
        if row["object"] not in handed or moment > handed[row["object"]]:
            handed[row["object"]] = moment
    found = sorted(
        {
            row["object"]
            for row in executed
            if origin_of(row.get("source")) != FAILSAFE_ORIGIN
            and (row["object"] not in handed or utc(row["proposed_at"]) > handed[row["object"]])
        }
    )
    log.debug(
        "objects commanded under a control read",
        control=control,
        actions=len(actions),
        executed=len(executed),
        handed_over=len(handed),
        commanded=len(found),
        elapsed_ms=_ms(started),
    )
    return found


def _unable(why: str) -> Executor:
    """What stands in for the executor when nothing can make the call: it records why."""

    def call(action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        log.error(
            "a fail-safe cannot be sent: nothing can make its call",
            action=action.get("id"),
            record=record.get("uid"),
            why=why,
        )
        return Execution(FAILED, error=why)

    return call


def _caller(document: Mapping[str, Any] | None, executor: Executor | None) -> Executor:
    if document is not None and document.get("binding") != EXECUTOR:
        return _unable(f"{document.get('id')} is bound advisory: the twin cannot make its call")
    if executor is None:
        return _unable("the executors add-on is off: nothing can make the call")
    return executor


def send(
    twin: Twin,
    wanted: Due,
    *,
    episode: datetime,
    state: str,
    incidents: Iterable[Mapping[str, Any]],
    now: datetime,
    executor: Executor | None,
) -> list[Sent]:
    """Send a control's fail-safe to every object the twin commanded under it, once in
    the episode that began at `episode`. What came of it on each object; none when the
    twin commanded nothing under the control, which is not a failure."""
    started = time.perf_counter()
    head = twin.store.head()
    objects = commanded(twin, wanted.control, head)
    if not objects:
        log.info(
            "a control's fail-safe is due and the twin commanded no object under it; "
            "nothing is sent",
            control=wanted.control,
            trigger=wanted.trigger,
        )
        return []
    document = catalog.find(twin, wanted.action, head) if wanted.action else None
    call = _caller(document, executor)
    unresolved = list(incidents)
    sent: list[Sent] = []
    for object_uid in objects:
        key = key_of(episode, wanted.control, object_uid)
        context = {
            "control": wanted.control,
            "object": object_uid,
            "action": wanted.action,
            "mode": wanted.mode,
            "trigger": wanted.trigger,
            "episode": iso_utc(episode),
        }
        result = Sent(
            control=wanted.control,
            object=object_uid,
            action=wanted.action,
            mode=wanted.mode,
            trigger=wanted.trigger,
            key=key,
            episode=episode,
            status=REJECTED_STATUS,
            created=True,
        )
        found = get(twin, record_uid(keyed_id(key)), RECORD_TYPE, head)
        interrupted = (
            found is not None and found.get("status") == APPROVED and found.get("execution") is None
        )
        if found is not None and not interrupted:
            result.created = False
            result.record = found["uid"]
            result.status = str(found.get("status"))
            log.debug("the fail-safe of this episode was sent before; not again", **context)
            sent.append(result)
            continue
        try:
            made = propose_with(
                twin,
                wanted.action,
                object_uid=object_uid,
                params=wanted.parameters,
                reason_text=wanted.reason(),
                source=source_of(wanted.control),
                state=state,
                incidents=unresolved,
                now=now,
                executor=call,
                key=key,
                tail=None,
                interlock=False,
            )
        except ActionError as exc:
            reasons = list(exc.detail.get("reasons") or [])
            result.record = exc.detail.get("record")
            result.reasons = reasons or [{"member": "action", "message": exc.message}]
            result.error = exc.message
            log.error(
                "a fail-safe could not be sent as the control says",
                exc=exc,
                code=exc.code,
                record=result.record,
                reasons=result.reasons,
                **context,
            )
            sent.append(result)
            continue
        result.record = made["uid"]
        result.status = str(made["status"])
        # One the process stopped sending before its outcome gets its first outcome now.
        result.created = interrupted or bool(made.get("created", True))
        execution = made.get("execution") or {}
        result.error = execution.get("error") if isinstance(execution, Mapping) else None
        if result.executed:
            log.info("fail-safe sent", record=made["uid"], **context)
        else:
            log.error(
                "a fail-safe was sent and the call did not succeed",
                record=made["uid"],
                status=made["status"],
                error=result.error,
                **context,
            )
        sent.append(result)
    log.info(
        "a control's fail-safe done",
        control=wanted.control,
        trigger=wanted.trigger,
        objects=len(objects),
        executed=sum(1 for found in sent if found.executed and found.created),
        failed=sum(1 for found in sent if not found.executed and found.created),
        again=sum(1 for found in sent if not found.created),
        elapsed_ms=_ms(started),
    )
    return sent


__all__ = [
    "FAILED_TRIGGER",
    "FAILSAFE_ORIGIN",
    "LOOP_ORIGIN",
    "ORIGINS",
    "PROPOSAL_ORIGIN",
    "SOURCE_PREFIX",
    "STALE_TRIGGER",
    "Due",
    "Sent",
    "commanded",
    "due",
    "key_of",
    "origin_filter",
    "origin_of",
    "send",
    "source_of",
]
