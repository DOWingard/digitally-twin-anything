"""What becomes of an incident at a check, and the twin's state, as plain functions.

A breach incident climbs a ladder, one step per check:

    rung 1  recalibrate the active candidate, while attempt < recalibrate_attempts
    rung 2  reselect among every candidate of the specification, if reselect
    rung 3  ask the managing agent to re-model, if agent_remodel; a model of the
            specification registered or changed afterwards brings rung 2 back, and
            nothing within remodel_timeout fails the incident

A rung that is not allowed is passed over. A step that is refused, or that put a
model in place whose origins then miss the tolerance, moves the incident on: the
next attempt at rung 1, rung 3 after rung 2. When the ladder runs out (rung 3 passed
over, or a rung 2 after a re-model came to nothing) the cycle ends: the next one
starts again at rung 1 on newer data, and a cycle beyond retry_budget fails the
incident. While a correction is pending, or the model it put in place has fewer
than three origins, the incident waits.

Resolution: three or more origins at or after the cut whose mean error is within the
tolerance. The cut is the end of the data of the model a correction put in place; with
none in place, the opening; for a failed incident, the end of the data of the active
model when that model was fitted after the failure (otherwise nothing resolves it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
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


def _take(position: Position, settings: Correction, now: datetime, logged: list) -> Step:
    """The next step from a position with nothing pending or awaited."""
    current = position
    while True:
        if current.rung == 1:
            if current.attempt < settings.recalibrate_attempts:
                return Step(current, (1, current.attempt, current.key_cycle), logged)
            current = replace(current, rung=2)
        if current.rung == 2:
            if settings.reselect:
                return Step(current, (2, current.attempt, current.key_cycle), logged)
            current = replace(current, rung=3)
        if settings.agent_remodel and not current.after_remodel:
            current = replace(current, status=REMODEL_REQUESTED, requested_at=now)
            logged.append(
                {
                    "action": "remodel_requested",
                    "timeout_s": settings.remodel_timeout.total_seconds(),
                }
            )
            return Step(current, None, logged, remodel_requested=True)
        cycle = current.cycle + 1
        if cycle > settings.retry_budget:
            logged.append({"action": "failed", "reason": "retry_budget", "cycles": current.cycle})
            return Step(replace(current, status=FAILED, failed_at=now), None, logged)
        logged.append({"action": "cycle", "cycle": cycle})
        current = replace(current, rung=1, attempt=0, cycle=cycle, after_remodel=False)


def next_step(
    position: Position,
    settings: Correction,
    found: Evidence,
    *,
    now: datetime,
    tolerance: float,
    remodelled: bool,
) -> Step:
    """One check's step for a breach incident, given the evidence at its cut and whether
    a model of its specification was registered or changed since a re-model request."""
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
        return Step(replace(current, status=RESOLVED, pending=None), None, logged)
    if current.awaiting is not None:
        if holds is None:
            return Step(current)
        logged.append(
            {"action": "not_held", "n": found.n, "error": found.error, "rung": current.rung}
        )
        current = failed_rung(current)
    if current.pending is not None:
        return Step(current, None, logged)
    if current.status == REMODEL_REQUESTED:
        assert current.requested_at is not None
        if remodelled:
            logged.append({"action": "remodelled"})
            # One more attempt: the key of this rung-2 step must differ from the one the
            # cycle took before the re-model, or the job would not run again.
            current = replace(
                current,
                status=OPEN,
                rung=2,
                attempt=current.attempt + 1,
                after_remodel=True,
                requested_at=None,
            )
        elif now - current.requested_at >= settings.remodel_timeout:
            logged.append(
                {
                    "action": "failed",
                    "reason": "remodel_timeout",
                    "timeout_s": settings.remodel_timeout.total_seconds(),
                }
            )
            return Step(replace(current, status=FAILED, failed_at=now), None, logged)
        else:
            return Step(current, None, logged)
    return _take(current, settings, now, logged)


def answered(position: Position, *, accepted: bool, fitted_to: datetime | None) -> Position:
    """The incident once the pending correction has been reported back."""
    if accepted:
        return replace(position, status=OPEN, awaiting=fitted_to, pending=None)
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
