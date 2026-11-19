"""What becomes of an incident at a check, and the twin's state, as plain functions.

A breach incident climbs a ladder, one step per check:

    rung 1  recalibrate the active candidate, while attempt < recalibrate_attempts
    rung 2  reselect among every candidate of the specification, if reselect
    rung 3  ask the managing agent to re-model, if agent_remodel; a model of the
            specification registered or changed afterwards brings rung 2 back, and
            nothing within remodel_timeout fails the incident

A rung that is not allowed is passed over. A step that is refused, or that put a
model in place whose origins then miss the tolerance, moves the incident on: the
next attempt at rung 1, rung 3 after rung 2. Rungs 1 and 2 make a cycle. When a cycle
has spent them it ends, asking for the re-model the first time (rung 3); cycle c >= 2
starts on newer data g * 2^(c-2) after the previous one ended, g being retry_backoff
(the fidelity interval when it is not set), while c <= retry_budget. The re-model
request stays open while those cycles run: a cycle whose model holds resolves the
incident, a model the agent registered or changed brings rung 2 back, and only
remodel_timeout fails an incident whose request is open, however many cycles were
spent. Without a request open, a cycle beyond retry_budget fails the incident. While a
correction is pending, or the model it put in place has fewer than three origins, the
incident waits. `attempt` counts the tries at the current rung: rung 2 starts at 0, and
a re-model brings it back at 1. `due` is the instant the next cycle starts, once the
current one has ended.

Resolution: three or more origins at or after the cut whose mean error is within the
tolerance. The cut is the end of the data of the model a correction put in place; with
none in place, the opening; for a failed incident, the end of the data of the active
model when that model was fitted after the failure (otherwise nothing resolves it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from dta_core.manifest.models import Correction

from .measures import Evidence
from .values import RECALIBRATE, RESELECT

OPEN = "open"
REMODEL_REQUESTED = "remodel_requested"
FAILED = "failed"
RESOLVED = "resolved"
UNRESOLVED = (OPEN, REMODEL_REQUESTED, FAILED)

UNKNOWN = "unknown"
HEALTHY = "healthy"
CORRECTING = "correcting"

RUNG_NAMES = {1: RECALIBRATE, 2: RESELECT}


@dataclass(frozen=True)
class Pending:
    """A correction asked for and not answered yet."""

    key: str
    rung: int
    attempt: int
    cycle: int
    at: datetime


@dataclass(frozen=True)
class Position:
    """Where a breach incident stands on its ladder."""

    status: str
    # The rung of the next step when nothing is pending or awaited.
    rung: int
    attempt: int
    cycle: int
    # End of the data of the model a correction put in place.
    awaiting: datetime | None = None
    pending: Pending | None = None
    # Rung 2 was brought back by a re-model in this cycle.
    after_remodel: bool = False
    requested_at: datetime | None = None
    failed_at: datetime | None = None
    # Cycles begun before the latest retry. A retry starts again at cycle 1, but the
    # keys of the corrections it asks for count on from here, so none is used twice.
    offset: int = 0
    # When cycle + 1 starts: set once the current cycle has spent its rungs.
    due: datetime | None = None
    # No further cycle is left, and the incident waits on its re-model request.
    spent: bool = False

    @property
    def key_cycle(self) -> int:
        return self.offset + self.cycle


def opened() -> Position:
    """A breach incident as it is opened, before its first step."""
    return Position(status=OPEN, rung=1, attempt=0, cycle=1)


@dataclass(frozen=True)
class Step:
    """What a check did to a breach incident: where it stands now, the correction to
    run (rung, attempt, cycle of its key), and the steps to log, in order."""

    position: Position
    request: tuple[int, int, int] | None = None
    logged: list[dict[str, Any]] = field(default_factory=list)
    remodel_requested: bool = False


def cut_of(position: Position, opened_at: datetime, fitted_to: datetime | None) -> datetime | None:
    """From which origin on the evidence of a breach incident is counted."""
    if position.status == FAILED:
        if fitted_to is not None and position.failed_at is not None:
            return fitted_to if fitted_to >= position.failed_at else None
        return None
    if position.awaiting is not None:
        return position.awaiting
    return opened_at


def failed_rung(position: Position) -> Position:
    """The step at the current rung came to nothing: move on from it."""
    if position.rung == 1:
        return replace(position, attempt=position.attempt + 1, awaiting=None, pending=None)
    return replace(position, rung=3, awaiting=None, pending=None)


def spacing_of(settings: Correction, interval: timedelta) -> timedelta:
    """g: the wait after cycle 1 before cycle 2, doubled for each cycle after it."""
    return settings.retry_backoff if settings.retry_backoff is not None else interval


def cycle_start(ended: datetime, cycle: int, spacing: timedelta) -> datetime:
    """When cycle `cycle` (>= 2) starts, its previous cycle having ended at `ended`."""
    return ended + spacing * (2 ** (cycle - 2))


def _has_rungs(settings: Correction) -> bool:
    """Whether a cycle holds any step: without rung 1 or 2 there is nothing to run again."""
    return settings.recalibrate_attempts > 0 or settings.reselect


def _last_cycle(position: Position, settings: Correction) -> bool:
    return position.cycle + 1 > settings.retry_budget or not _has_rungs(settings)


def _take(
    position: Position, settings: Correction, now: datetime, logged: list, spacing: timedelta
) -> Step:
    """The next step from a position with nothing pending or awaited."""
    current = position
    asked = False
    if current.spent:
        return Step(current, None, logged)
    while True:
        if current.due is not None:
            if now < current.due:
                return Step(current, None, logged, remodel_requested=asked)
            cycle = current.cycle + 1
            logged.append({"action": "cycle", "cycle": cycle})
            current = replace(
                current, rung=1, attempt=0, cycle=cycle, after_remodel=False, due=None
            )
        if current.rung == 1:
            if current.attempt < settings.recalibrate_attempts:
                request = (1, current.attempt, current.key_cycle)
                return Step(current, request, logged, remodel_requested=asked)
            current = replace(current, rung=2, attempt=0)
        if current.rung == 2:
            if settings.reselect:
                request = (2, current.attempt, current.key_cycle)
                return Step(current, request, logged, remodel_requested=asked)
            current = replace(current, rung=3)
        # The cycle has spent rungs 1 and 2.
        if (
            settings.agent_remodel
            and current.status != REMODEL_REQUESTED
            and not current.after_remodel
        ):
            current = replace(current, status=REMODEL_REQUESTED, requested_at=now)
            logged.append(
                {
                    "action": "remodel_requested",
                    "timeout_s": settings.remodel_timeout.total_seconds(),
                }
            )
            asked = True
        cycle = current.cycle + 1
        if _last_cycle(current, settings):
            if current.status == REMODEL_REQUESTED:
                # Only the re-model's timeout fails an incident whose request is open.
                if _has_rungs(settings):
                    logged.append({"action": "cycles_spent", "cycles": current.cycle})
                return Step(replace(current, spent=True), None, logged, remodel_requested=asked)
            logged.append({"action": "failed", "reason": "retry_budget", "cycles": current.cycle})
            return Step(replace(current, status=FAILED, failed_at=now), None, logged)
        due = cycle_start(now, cycle, spacing)
        logged.append({"action": "cycle_ended", "cycle": current.cycle, "next_cycle": cycle})
        current = replace(current, due=due)


def next_step(
    position: Position,
    settings: Correction,
    found: Evidence,
    *,
    now: datetime,
    tolerance: float,
    remodelled: bool,
    interval: timedelta,
) -> Step:
    """One check's step for a breach incident, given the evidence at its cut, whether a
    model of its specification was registered or changed since a re-model request, and
    the fidelity interval (the spacing of cycles when retry_backoff is not set)."""
    logged: list[dict[str, Any]] = []
    if position.status == RESOLVED:
        return Step(position)
    holds = found.holds(tolerance)
    if position.status == FAILED:
        if holds:
            logged.append({"action": "resolved", "n": found.n, "error": found.error})
            return Step(replace(position, status=RESOLVED), None, logged)
        return Step(position)
    current = position
    if holds:
        action = "held" if current.awaiting is not None else "recovered"
        logged.append({"action": action, "n": found.n, "error": found.error})
        return Step(replace(current, status=RESOLVED, pending=None, due=None), None, logged)
    if current.status == REMODEL_REQUESTED and not remodelled:
        assert current.requested_at is not None
        if now - current.requested_at >= settings.remodel_timeout:
            logged.append(
                {
                    "action": "failed",
                    "reason": "remodel_timeout",
                    "timeout_s": settings.remodel_timeout.total_seconds(),
                }
            )
            # A correction of a further cycle still running is no longer waited for.
            failed = replace(current, status=FAILED, failed_at=now, pending=None, due=None)
            return Step(failed, None, logged)
    if current.awaiting is not None:
        if holds is None:
            return Step(current)
        logged.append(
            {"action": "not_held", "n": found.n, "error": found.error, "rung": current.rung}
        )
        current = failed_rung(current)
    if current.pending is not None:
        return Step(current, None, logged)
    if current.status == REMODEL_REQUESTED and remodelled:
        logged.append({"action": "remodelled"})
        # The second attempt at rung 2: its key must differ from the one the cycle may
        # have taken before the re-model, or the job would not run again. A cycle has at
        # most one re-model, as only a cycle without one makes a request.
        current = replace(
            current,
            status=OPEN,
            rung=2,
            attempt=1,
            after_remodel=True,
            requested_at=None,
            due=None,
            spent=False,
        )
    return _take(current, settings, now, logged, spacing_of(settings, interval))


def answered(position: Position, *, accepted: bool, fitted_to: datetime | None) -> Position:
    """The incident once the pending correction has been reported back; an open re-model
    request stays open."""
    if accepted:
        status = REMODEL_REQUESTED if position.status == REMODEL_REQUESTED else OPEN
        return replace(position, status=status, awaiting=fitted_to, pending=None)
    return failed_rung(position)


def retried(position: Position) -> Position:
    """Back to cycle 1, rung 1, with nothing pending or awaited."""
    return Position(status=OPEN, rung=1, attempt=0, cycle=1, offset=position.key_cycle)


# -- stale ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleStep:
    status: str | None
    # Consecutive stale checks.
    count: int
    logged: list[dict[str, Any]] = field(default_factory=list)


def stale_step(status: str | None, count: int, fresh: bool | None, *, budget: int) -> StaleStep:
    """One check's step for a question's stale incident (status None: there is none).

    Opens at a stale check, resolves at a fresh one, fails once it has been stale at
    `budget` consecutive checks. A check that cannot tell changes nothing."""
    if fresh is None or (status is None and fresh) or status == RESOLVED and fresh:
        return StaleStep(status, count)
    if fresh:
        return StaleStep(RESOLVED, count, [{"action": "fresh", "count": count}])
    total = count + 1 if status in UNRESOLVED else 1
    logged: list[dict[str, Any]] = [{"action": "stale", "count": total}]
    if status in (None, RESOLVED):
        logged.insert(0, {"action": "opened"})
        status = OPEN
    if status == OPEN and total >= budget:
        logged.append({"action": "failed", "reason": "retry_budget", "count": total})
        status = FAILED
    return StaleStep(status, total, logged)


# -- the twin's state ------------------------------------------------------------------


def state_of(statuses: Iterable[str], checked: bool) -> str:
    """FAILED if any incident failed, else CORRECTING if any is open, else HEALTHY once a
    check has completed, else unknown."""
    found = set(statuses)
    if FAILED in found:
        return FAILED
    if found & {OPEN, REMODEL_REQUESTED}:
        return CORRECTING
    return HEALTHY if checked else UNKNOWN


def remodelled(before: Mapping[str, str], now: Mapping[str, str]) -> bool:
    """Whether a model of a specification was registered or changed: a candidate that
    was not there, or one whose identity differs."""
    return any(before.get(uid) != identity for uid, identity in now.items())
