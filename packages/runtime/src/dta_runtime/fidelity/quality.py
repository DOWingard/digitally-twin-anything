"""The two tests of an observed signal, as plain functions of plain data.

Stuck: over the lookback, split into sync intervals, each interval in which the source
delivered is C (the value changed) or N (it did not). The signal is called stuck when the
run of N since its last change is improbable under the signal's own history:

1. A smooth signal is not tested by the length of its run. A level, say, is flat for a
   while around each of its turning points, longer at the turning point of a slow swing
   than at a daily one, and a lookback seldom holds the slowest: no length of run tells
   such a flat from a stuck sensor without calling good readings stuck. Smooth is read
   off the signs of its changes: of the m pairs of consecutive changes in the lookback,
   s keep their sign; with m >= 10 it is smooth when a one-sided binomial test of s
   against m/2 gives p < 0.01.
   What tells them apart is the twin's own forecast. Once a smooth signal has gone
   n >= 3 delivering intervals without a change, the question that would infer it (the
   predictive question on its property with an active model and the shortest horizon)
   forecasts it once for the run, from the end O of the interval of its last change and
   never from an inferred value; at each check of the run the forecast is read at the
   lead L (intervals since O). It is stuck when |f(O, L) - r| > m, r the unchanged
   reading and m half the question's tolerance: eps/2 for a metric in the property's
   units (mae, rmse, max_abs), eps/2 * |r| for a relative one (mape). Half, so that a
   frozen reading is flagged before it is wrong by the tolerance. Without a question, a
   metric of either kind, a forecast, a lead within the horizon, or a finite value at
   that lead, it is not tested (`forecast.refused` says why): never flagged without
   that evidence.
2. Otherwise the history up to and including the last change is taken as a two-state
   chain and its transitions are counted (between intervals that both delivered):
   n_CC, n_CN, n_NC, n_NN. With p0 = (u + 1)/(k + 2), u of the k history intervals
   without a change, the probability of the run of n intervals without a change that
   follows the last change is P = p0^n when the no-change intervals come as
   independent draws, and P = b a^(n-1) when they cluster:

       b = P(N | C) = (n_CN + w p0) / (n_CC + n_CN + w)
       a = P(N | N) = (n_NN + w p0) / (n_NC + n_NN + w),    w = 2,

   the rows estimated with a prior of weight w centred on independence. They cluster
   when a G-test of independence on the 2x2 table of transitions gives p < 0.01 and
   a > b.
3. It is stuck when n >= 3 and P < 1e-7, the false-alarm rate per series and check.
   (Steps 2 and 3 are for a signal that is not smooth.)

It is tested only when it changed in at least 20 intervals, so a signal that seldom
changes is never called stuck. The lookback is the fidelity window, and never fewer
than 96 sync intervals.

Biased: the mean signed residual of a series at its newest origins against its own
earlier ones. Baseline: the median and 1.4826 * MAD of the earlier residuals (the
newest three left out), the scale no smaller than a tenth of the tolerance. The
newest three, standardised, run through a two-sided CUSUM (k = 0.5, H = 5) from
zero; the series alarms when either side ends above H and the newest three depart
from the baseline's median by at least the tolerance on average: a shift smaller
than what the question tolerates is not material, however steady the series was
before. Series alarm together when
the model is wrong, so they are called biased only when fewer than half of the
tested series of the question alarm at once.

A series is tested by every predictive question on its property, and is called by
the question that would infer it (the one with the shortest horizon). It is biased
only when every other question on the property tested it at the same check and
alarms too, on the same side: a sensor that reads wrong disagrees with every model
of its property; a model that is wrong for one series disagrees alone (a pump the
model does not see, say, moving one tank's level). A property with one predictive
question needs no agreement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import numpy as np

STUCK_MIN_CHANGES = 20
STUCK_MIN_INTERVALS = 3
# The false-alarm rate per series and check: P(the run in progress | the signal's history).
# Every interval can start a run, so a month of 15-minute syncs (2880 intervals) holds at
# most about 2880 * alpha false alarms per series: below 1e-3 needs alpha < 3.5e-7.
STUCK_ALPHA = 1e-7
# The stuck test reads at least this many sync intervals, whatever the fidelity window: a
# signal changing in every interval is called stuck after 6 only when k >= 20 changes
# precede them ((1/22)^6 < 1e-7), and the rate above was measured over 96 intervals.
STUCK_LOOKBACK = 96
# Weight, in transitions, of the prior that centres each row of the chain on independence.
STUCK_PRIOR = 2.0
# The level of the tests that call a signal smooth and its no-change intervals clustered.
SMOOTH_LEVEL = 0.01
SMOOTH_MIN_PAIRS = 10
CLUSTER_LEVEL = 0.01

# Why a signal was not tested for being stuck.
NEVER_CHANGED = "never_changed"
FEW_CHANGES = "few_changes"
SMOOTH = "smooth"
INDEPENDENT, MARKOV, FORECAST = "independent", "markov", "forecast"
# The share of the inferring question's tolerance a smooth signal's frozen reading may lie
# from the forecast before it is called stuck; and the metrics whose tolerance is in the
# property's units, or relative to the true value.
FORECAST_MARGIN = 0.5
ABSOLUTE_METRICS = frozenset({"mae", "rmse", "max_abs"})
RELATIVE_METRICS = frozenset({"mape"})

BIAS_MIN_ORIGINS = 8
BIAS_HELD_OUT = 3
BIAS_K = 0.5
BIAS_H = 5.0
MAD_SCALE = 1.4826
SIGMA_FLOOR = 0.1

# Why another question on the property does not bear out a series called biased.
NOT_TESTED = "not_tested"
NO_ALARM = "no_alarm"
OTHER_SIDE = "other_side"


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
    return [found is not None for found in steps_per_interval(start, step, count, before, events)]


def steps_per_interval(
    start: datetime,
    step: timedelta,
    count: int,
    before: Any,
    events: Sequence[tuple[datetime, Any]],
) -> list[float | None]:
    """For each interval as `changes_per_interval` splits them: None when the value did
    not change in it, else how far it moved over it (NaN when the values are not
    numbers, 0.0 when it came back to where it was)."""
    first: list[Any] = [None] * count
    final: list[Any] = [None] * count
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
            if not changed[position]:
                first[position] = previous
            changed[position] = True
            final[position] = value
        previous = value
    found: list[float | None] = []
    for position in range(count):
        if not changed[position]:
            found.append(None)
            continue
        old, new = first[position], final[position]
        numbers = all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in (old, new)
        )
        found.append(float(new) - float(old) if numbers else math.nan)
    return found


@dataclass(frozen=True)
class StuckTest:
    tested: bool
    stuck: bool
    # Delivering intervals since the interval of the last change.
    n: int
    # Observed intervals up to and including that of the last change, and those of them
    # without a change.
    k: int
    u: int
    p0: float | None
    # Index of the interval of the last change; None when it never changed.
    last: int | None
    # Why it was not tested; None when it was.
    skipped: str | None = None
    # Pairs of consecutive changes in the lookback, those that kept their sign, and the
    # one-sided binomial p-value of that many.
    pairs: int = 0
    kept: int = 0
    smooth_p: float | None = None
    # Transitions of the history between intervals that both delivered.
    cc: int = 0
    cn: int = 0
    nc: int = 0
    nn: int = 0
    # The G-test of independence of those transitions, and the model used.
    cluster_p: float | None = None
    model: str | None = None
    # b = P(N | C), a = P(N | N) as the model has them (both p0 when independent).
    b: float | None = None
    a: float | None = None
    # P of the run in progress, and the shortest run that would be called stuck.
    p: float | None = None
    needed: int | None = None
    # A smooth signal's test against the inferring question's forecast: {question,
    # origin, lead, value, reading, margin, refused}; None when there was no run to test.
    forecast: Mapping[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        return {
            "tested": self.tested,
            "stuck": self.stuck,
            "skipped": self.skipped,
            "n": self.n,
            "k": self.k,
            "u": self.u,
            "p0": self.p0,
            "signs": {"pairs": self.pairs, "kept": self.kept, "p": self.smooth_p},
            "transitions": {"cc": self.cc, "cn": self.cn, "nc": self.nc, "nn": self.nn},
            "cluster_p": self.cluster_p,
            "model": self.model,
            "b": self.b,
            "a": self.a,
            "p": self.p,
            "alpha": STUCK_ALPHA,
            "needed": self.needed,
            "forecast": None if self.forecast is None else dict(self.forecast),
        }


def run_probability(b: float, a: float, n: int) -> float:
    """P(a run of n intervals without a change right after a change) = b a^(n-1)."""
    return b * a ** max(0, n - 1)


def needed_run(b: float, a: float) -> int:
    """The shortest run, at least STUCK_MIN_INTERVALS, whose probability is below the
    false-alarm rate."""
    if b < STUCK_ALPHA or a <= 0.0:
        return STUCK_MIN_INTERVALS
    # b a^(n-1) < alpha  <=>  n - 1 > log(alpha / b) / log(a), with 0 < a < 1.
    bound = math.log(STUCK_ALPHA / b) / math.log(a)
    return max(STUCK_MIN_INTERVALS, math.floor(bound) + 2)


@dataclass(frozen=True)
class Smoothness:
    pairs: int
    kept: int
    # One-sided binomial p-value of keeping the sign that often; None below the minimum.
    p: float | None

    @property
    def smooth(self) -> bool:
        return self.p is not None and self.p < SMOOTH_LEVEL


def smoothness(steps: Sequence[float | None]) -> Smoothness:
    """How often a signal's consecutive changes keep their sign, as the stuck test's
    first step reads it."""
    moves = [found for found in steps if found is not None and math.isfinite(found) and found]
    pairs = max(0, len(moves) - 1)
    kept = sum(
        1 for left, right in zip(moves, moves[1:], strict=False) if (left > 0) == (right > 0)
    )
    if pairs < SMOOTH_MIN_PAIRS:
        return Smoothness(pairs, kept, None)
    # One-sided binomial tail: P(X >= kept) for X ~ Bin(pairs, 1/2).
    tail = sum(math.comb(pairs, found) for found in range(kept, pairs + 1))
    return Smoothness(pairs, kept, min(1.0, tail / 2.0**pairs))


def clustering_p(cc: int, cn: int, nc: int, nn: int) -> float | None:
    """The G-test of independence of the next interval from the one before; None when a
    row or a column of the table is empty, which says nothing either way."""
    table = ((cc, cn), (nc, nn))
    rows = [sum(row) for row in table]
    columns = [cc + nc, cn + nn]
    total = sum(rows)
    if 0 in rows or 0 in columns:
        return None
    # G = 2 sum O ln(O / E), with one degree of freedom: P(chi2_1 > G) = erfc(sqrt(G / 2)).
    g = 2.0 * sum(
        observed * math.log(observed * total / (rows[i] * columns[j]))
        for i, row in enumerate(table)
        for j, observed in enumerate(row)
        if observed
    )
    return math.erfc(math.sqrt(max(g, 0.0) / 2.0))


def stuck_test(
    changed: Sequence[bool],
    delivering: Sequence[bool],
    steps: Sequence[float | None] | None = None,
) -> StuckTest:
    """The stuck test over intervals given oldest first. An interval is observed when its
    source delivered or the value changed in it; one that is not breaks the chain.
    `steps` (as `steps_per_interval` gives them) are what tells a smooth signal; without
    them none is taken for smooth."""
    if len(changed) != len(delivering) or (steps is not None and len(steps) != len(changed)):
        raise ValueError("changed, delivering and steps differ in length")
    last = max((index for index, flag in enumerate(changed) if flag), default=None)
    if last is None:
        return StuckTest(False, False, 0, 0, 0, None, None, skipped=NEVER_CHANGED)
    seen = [bool(flag) or bool(sent) for flag, sent in zip(changed, delivering, strict=True)]
    k = sum(seen[: last + 1])
    u = sum(1 for index in range(last + 1) if seen[index] and not changed[index])
    n = sum(1 for flag in delivering[last + 1 :] if flag)
    counts = {"cc": 0, "cn": 0, "nc": 0, "nn": 0}
    for index in range(1, last + 1):
        if seen[index - 1] and seen[index]:
            key = ("c" if changed[index - 1] else "n") + ("c" if changed[index] else "n")
            counts[key] += 1
    shape = smoothness(steps) if steps is not None else Smoothness(0, 0, None)
    common = {"pairs": shape.pairs, "kept": shape.kept, "smooth_p": shape.p, **counts}
    if k - u < STUCK_MIN_CHANGES:
        return StuckTest(False, False, n, k, u, None, last, skipped=FEW_CHANGES, **common)
    p0 = (u + 1) / (k + 2)
    if shape.smooth:
        return StuckTest(False, False, n, k, u, p0, last, skipped=SMOOTH, **common)
    weight = STUCK_PRIOR
    b = (counts["cn"] + weight * p0) / (counts["cc"] + counts["cn"] + weight)
    a = (counts["nn"] + weight * p0) / (counts["nc"] + counts["nn"] + weight)
    cluster_p = clustering_p(**counts)
    if cluster_p is not None and cluster_p < CLUSTER_LEVEL and a > b:
        model = MARKOV
    else:
        model, b, a = INDEPENDENT, p0, p0
    p = run_probability(b, a, n)
    return StuckTest(
        True,
        n >= STUCK_MIN_INTERVALS and p < STUCK_ALPHA,
        n,
        k,
        u,
        p0,
        last,
        **common,
        cluster_p=cluster_p,
        model=model,
        b=b,
        a=a,
        p=p,
        needed=needed_run(b, a),
    )


def wants_forecast(test: StuckTest) -> bool:
    """Whether a signal is to be tested against the forecast: skipped as smooth, with a
    run of at least `STUCK_MIN_INTERVALS` delivering intervals since its last change."""
    return test.skipped == SMOOTH and test.last is not None and test.n >= STUCK_MIN_INTERVALS


def forecast_margin(metric: str | None, tolerance: float | None, reading: float) -> float | None:
    """How far a frozen reading may lie from the forecast before it is called stuck: half
    the tolerance, in the property's units or relative to the reading. None when the
    metric is of neither kind, there is no tolerance, or a relative margin would be
    nothing (a reading of zero, where a relative error is not defined)."""
    if tolerance is None or not math.isfinite(tolerance) or tolerance <= 0:
        return None
    if metric in ABSOLUTE_METRICS:
        return FORECAST_MARGIN * tolerance
    if metric in RELATIVE_METRICS and reading != 0:
        return FORECAST_MARGIN * tolerance * abs(reading)
    return None


def against_forecast(
    test: StuckTest, *, value: float, reading: float, margin: float, document: Mapping[str, Any]
) -> StuckTest:
    """A smooth signal's test (`wants_forecast`) decided by the forecast: stuck when the
    unchanged reading lies more than `margin` from the forecast value."""
    return replace(
        test,
        tested=True,
        stuck=test.n >= STUCK_MIN_INTERVALS and abs(value - reading) > margin,
        skipped=None,
        model=FORECAST,
        forecast=dict(document),
    )


def without_forecast(test: StuckTest, document: Mapping[str, Any]) -> StuckTest:
    """A smooth signal's test left untested, with why there was no forecast to test it."""
    return replace(test, tested=False, stuck=False, forecast=dict(document))


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
    # Mean of the newest residuals less the baseline's median, in the series' unit.
    shift: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def s(self) -> float:
        """The side that ended highest, signed: positive when the residuals rose."""
        return self.upper if self.upper >= self.lower else -self.lower

    @property
    def side(self) -> int:
        """+1 when the upper side ended above H, -1 when the lower one did, 0 without
        an alarm."""
        if not self.alarm:
            return 0
        return 1 if self.s > 0 else -1

    def document(self) -> dict[str, Any]:
        return {
            "tested": self.tested,
            "alarm": self.alarm,
            "origins": self.origins,
            "median": self.median,
            "sigma": self.sigma,
            "z": list(self.z),
            "s": self.s,
            "shift": self.shift,
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
    shift = float(np.mean(newest) - median)
    return BiasTest(
        True,
        (upper > BIAS_H or lower > BIAS_H) and abs(shift) >= tolerance,
        count,
        median,
        sigma,
        tuple(float(value) for value in z),
        upper,
        lower,
        shift,
    )


def biased(results: Mapping[str, BiasTest]) -> set[str]:
    """The series called biased: those that alarm, when fewer than half of the tested
    ones do; none otherwise (then it is the model)."""
    tested = [name for name, found in results.items() if found.tested]
    alarming = {name for name in tested if results[name].alarm}
    if 2 * len(alarming) < len(tested):
        return alarming
    return set()


def uncorroborated(called: BiasTest, others: Mapping[str, BiasTest | None]) -> dict[str, str]:
    """Of the other questions on a series' property, keyed by id, with their test of
    the series at this check (None when they did not test it), those that do not bear
    out the alarm of `called`, and why. Empty when every one does: then it is biased."""
    found: dict[str, str] = {}
    for qid, test in sorted(others.items()):
        if test is None or not test.tested:
            found[qid] = NOT_TESTED
        elif not test.alarm:
            found[qid] = NO_ALARM
        elif test.side != called.side:
            found[qid] = OTHER_SIDE
    return found
