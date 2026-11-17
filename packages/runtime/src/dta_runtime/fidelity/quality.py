"""The two tests of an observed signal, as plain functions of plain data.

Stuck: over the lookback, split into sync intervals, a signal that used to change
in most intervals has not changed for n intervals in which its source kept
delivering. With k intervals before its last change, u of them without a change,
the chance of an interval without a change is taken as p0 = (u + 1)/(k + 2); the
signal is stuck when n >= 3 and p0^n < 1e-4. It is tested only when it changed in
at least 20 intervals, so a signal that seldom changes is never called stuck.

Biased: the mean signed residual of a series at its newest origins against its own
earlier ones. Baseline: the median and 1.4826 * MAD of the earlier residuals (the
newest three left out), the scale no smaller than a tenth of the tolerance. The
newest three, standardised, run through a two-sided CUSUM (k = 0.5, H = 5) from
zero; the series alarms when either side ends above H. Series alarm together when
the model is wrong, so they are called biased only when fewer than half of the
tested series of the question alarm at once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

STUCK_MIN_CHANGES = 20
STUCK_MIN_INTERVALS = 3
STUCK_P = 1e-4

BIAS_MIN_ORIGINS = 8
BIAS_HELD_OUT = 3
BIAS_K = 0.5
BIAS_H = 5.0
MAD_SCALE = 1.4826
SIGMA_FLOOR = 0.1


# -- stuck -------------------------------------------------------------------------------


def changes_per_interval(
    start: datetime,
    step: timedelta,
    count: int,
    before: Any,
    events: Sequence[tuple[datetime, Any]],
) -> list[bool]:
    """For each of `count` intervals (start + i*step, start + (i+1)*step], whether the
    value changed in it. `before` is the value in force at `start`; `events` are the
    values stored after it, oldest first. Storing the same value again is no change."""
    changed = [False] * count
    previous = before
    for moment, value in events:
        if moment <= start:
            previous = value
            continue
        position = -((start - moment) // step) - 1
        if position >= count:
            break
        if value != previous:
            changed[position] = True
        previous = value
    return changed


@dataclass(frozen=True)
class StuckTest:
    tested: bool
    stuck: bool
    # Delivering intervals since the interval of the last change.
    n: int
    # Intervals up to and including that of the last change, and those without a change.
    k: int
    u: int
    p0: float | None
    # Index of the interval of the last change; None when it never changed.
    last: int | None

    def document(self) -> dict[str, Any]:
        return {
            "tested": self.tested,
            "stuck": self.stuck,
            "n": self.n,
            "k": self.k,
            "u": self.u,
            "p0": self.p0,
            "p0_n": None if self.p0 is None else self.p0**self.n,
        }


def stuck_test(changed: Sequence[bool], delivering: Sequence[bool]) -> StuckTest:
    """The stuck test over intervals given oldest first."""
    if len(changed) != len(delivering):
        raise ValueError("changed and delivering differ in length")
    last = max((index for index, flag in enumerate(changed) if flag), default=None)
    if last is None:
        return StuckTest(False, False, 0, 0, 0, None, None)
    k = last + 1
    u = sum(1 for flag in changed[:k] if not flag)
    n = sum(1 for flag in delivering[k:] if flag)
    if k - u < STUCK_MIN_CHANGES:
        return StuckTest(False, False, n, k, u, None, last)
    p0 = (u + 1) / (k + 2)
    return StuckTest(True, n >= STUCK_MIN_INTERVALS and p0**n < STUCK_P, n, k, u, p0, last)


# -- biased ------------------------------------------------------------------------------


@dataclass(frozen=True)
class BiasTest:
    tested: bool
    alarm: bool
    origins: int
    median: float | None = None
    sigma: float | None = None
    z: tuple[float, ...] = ()
    # The CUSUM's two sides after the newest origin.
    upper: float = 0.0
    lower: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def s(self) -> float:
        """The side that ended highest, signed: positive when the residuals rose."""
        return self.upper if self.upper >= self.lower else -self.lower

    def document(self) -> dict[str, Any]:
        return {
            "tested": self.tested,
            "alarm": self.alarm,
            "origins": self.origins,
            "median": self.median,
            "sigma": self.sigma,
            "z": list(self.z),
            "s": self.s,
        }


def bias_test(residuals: Sequence[float], tolerance: float) -> BiasTest:
    """The bias test of one series over its mean signed residuals, oldest origin first."""
    count = len(residuals)
    if count < BIAS_MIN_ORIGINS:
        return BiasTest(False, False, count)
    values = np.asarray(residuals, dtype=np.float64)
    baseline, newest = values[:-BIAS_HELD_OUT], values[-BIAS_HELD_OUT:]
    median = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - median)))
    sigma = max(MAD_SCALE * mad, SIGMA_FLOOR * tolerance)
    z = (newest - median) / sigma
    upper = lower = 0.0
    for value in z:
        upper = max(0.0, upper + float(value) - BIAS_K)
        lower = max(0.0, lower - float(value) - BIAS_K)
    return BiasTest(
        True,
        upper > BIAS_H or lower > BIAS_H,
        count,
        median,
        sigma,
        tuple(float(value) for value in z),
        upper,
        lower,
    )


def biased(results: Mapping[str, BiasTest]) -> set[str]:
    """The series called biased: those that alarm, when fewer than half of the tested
    ones do; none otherwise (then it is the model)."""
    tested = [name for name, found in results.items() if found.tested]
    alarming = {name for name in tested if results[name].alarm}
    if 2 * len(alarming) < len(tested):
        return alarming
    return set()
