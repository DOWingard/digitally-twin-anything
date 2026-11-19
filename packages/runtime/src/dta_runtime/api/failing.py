"""The fail-safes of the twin's controls, sent at a fidelity check.

After every check, a replayed one too, each control whose `on_failure` is due at the
check's time is sent to the objects the twin commanded under it (see the actions
library's `failsafe`): its state and incidents are read at the head the check left, the
episode is the stretch the twin has not been healthy, and the time of every record is
the check's, never the wall clock. Each new outcome is told: `twin.action.failsafe`
(severity `warning`) when the call was executed, `twin.action.failsafe_failed`
(severity `critical`, the highest) when it could not be sent or its call did not
succeed. An outcome the episode had already is not told again.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dta_core.errors import DtaError
from dta_core.log import Log

from .. import fidelity
from ..actions import failsafe
from ..notify import CRITICAL, WARNING
from .tables import iso

if TYPE_CHECKING:
    from .services import Services

log = Log("api.failsafe")

FAILSAFE = "twin.action.failsafe"
FAILSAFE_FAILED = "twin.action.failsafe_failed"
STALE = "stale"
UNRESOLVED = "unresolved"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def tell(services: Services, sent: dict[str, Any], at: datetime) -> str | None:
    """Tell one new outcome of a fail-safe; the type told, None when it could not be."""
    executed = sent.get("status") == "executed"
    told = FAILSAFE if executed else FAILSAFE_FAILED
    try:
        services.notifier.emit(
            told,
            sent.get("record") or f"control:{sent.get('control')}",
            sent,
            time=at,
            severity=WARNING if executed else CRITICAL,
        )
    except Exception as exc:
        # The record is written; a notice that cannot be queued is not a reason to undo.
        log.error(
            "a fail-safe's notice could not be queued",
            exc=exc,
            control=sent.get("control"),
            record=sent.get("record"),
        )
        return None
    log.info(
        "fail-safe notice queued",
        type=told,
        control=sent.get("control"),
        object=sent.get("object"),
        record=sent.get("record"),
    )
    return told


def fail_safe(services: Services, at: datetime) -> list[dict[str, Any]]:
    """Send every control's fail-safe that is due at `at`; what came of it on each object,
    with `told` (the type of the notice, None when nothing new was told)."""
    started = time.perf_counter()
    head = services.store.head()
    manifest = services.twin.manifest(at=head)
    found = fidelity.standing(services.twin, head)
    state = str(found["state"])
    incidents = [{"kind": item["kind"], "uid": item["uid"]} for item in found["incidents"]]
    stale = (
        fidelity.incidents(services.twin, at=head, kind=STALE, status=UNRESOLVED)
        if any(item["kind"] == STALE for item in incidents)
        else []
    )
    wanted = failsafe.due(manifest, state=state, stale=stale, now=at)
    if not wanted:
        log.debug("no fail-safe is due", state=state, at=iso(at))
        return []
    episode = fidelity.unhealthy_since(services.twin, head)
    if episode is None:
        log.warn(
            "a fail-safe is due and the twin's states say it is healthy; nothing is sent",
            state=state,
            at=iso(at),
            controls=[item.control for item in wanted],
        )
        return []
    answered: list[dict[str, Any]] = []
    for item in wanted:
        try:
            outcomes = failsafe.send(
                services.twin,
                item,
                episode=episode,
                state=state,
                incidents=incidents,
                now=at,
                executor=services.executor,
            )
        except DtaError as exc:
            log.error(
                "a control's fail-safe could not be sent: the twin could not be read or written",
                exc=exc,
                code=exc.code,
                control=item.control,
                trigger=item.trigger,
            )
            document = {
                "control": item.control,
                "object": None,
                "action": item.action,
                "mode": item.mode,
                "trigger": item.trigger,
                "episode": iso(episode),
                "record": None,
                "status": "failed",
                "created": True,
                "reasons": [],
                "error": f"{exc.code}: {exc.message}",
            }
            answered.append({**document, "told": tell(services, document, at)})
            continue
        for outcome in outcomes:
            document = outcome.as_dict()
            told = tell(services, document, at) if outcome.created else None
            answered.append({**document, "told": told})
    log.info(
        "fail-safes sent at a check",
        at=iso(at),
        state=state,
        episode=iso(episode),
        controls=[item.control for item in wanted],
        outcomes=len(answered),
        executed=sum(1 for item in answered if item["status"] == "executed" and item["created"]),
        failed=sum(1 for item in answered if item["status"] != "executed" and item["created"]),
        elapsed_ms=_ms(started),
    )
    return answered
