"""The sync interval a twin's contract and its sources call for: the step of its time grid.

The questions bound the interval from above: each allows at most its staleness
less the refresh period of a source it is answered from, and a property that
changes fast wants at least two looks per characteristic time. The sources
bound it from below: syncing faster than the fastest source changes brings
nothing, and a rate limit forbids it.

Forecasts are made in steps of the interval, in a static twin too, so every
forecast horizon has to be a whole number of them. The interval the rule gives,
Δ, is fitted to g, the greatest common divisor of the horizons: g / ⌈g / Δ⌉,
or the next shorter interval that divides g in whole milliseconds, which the
twin's versions are kept in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

from .checks.freshness import freshness_bounds
from .checks.view import FORECAST_KINDS, ManifestView, fmt_duration
from .models import Manifest, SyncDerivation

RULE = (
    "fit(max(source_period, rate_period, min(freshness_bound, change_time / 2)), horizon), "
    "fit(d, g) = g / ceil(g / d), shortened until it divides g in whole milliseconds"
)
HORIZON = "horizon"
_ZERO = timedelta(0)
_MS = timedelta(milliseconds=1)


def _horizons(view: ManifestView) -> dict[str, timedelta]:
    """Each forecast question's horizon, where it is a positive whole number of milliseconds."""
    return {
        qid: question.horizon
        for qid, question in sorted(view.questions.items())
        if question.kind in FORECAST_KINDS
        and question.horizon is not None
        and question.horizon > _ZERO
        and question.horizon % _MS == _ZERO
    }


def _gcd(values: list[timedelta]) -> timedelta | None:
    if not values:
        return None
    return math.gcd(*(value // _MS for value in values)) * _MS


def fit_to_horizon(interval: timedelta, horizon: timedelta) -> timedelta:
    """The longest interval not longer than g / ⌈g / interval⌉ that divides `horizon` in
    whole milliseconds; the interval itself when it already divides it."""
    whole = horizon // _MS
    steps = max(1, -(-horizon // interval))
    while whole % steps:
        steps += 1
    return (whole // steps) * _MS


def _change_time(view: ManifestView) -> timedelta | None:
    """How fast the fastest-changing property a question rests on changes, where measured."""
    found = [
        mapped.column_profile.change_time
        for question in view.questions.values()
        for ref in view.well_formed_targets(question)
        for mapped in view.mapped_columns(ref)
        if mapped.column_profile is not None and mapped.column_profile.change_time is not None
    ]
    return min(found, default=None)


@dataclass(frozen=True)
class IntervalDerivation:
    """The interval the rule gives, what went into it and whether the contract can be met."""

    # None when nothing the rule needs is known.
    interval: timedelta | None
    inputs: SyncDerivation
    # The term that decided the interval: one of the rule's four names, or `horizon`
    # when fitting the interval to the forecast horizons changed it.
    binding: str | None = None
    # Why the interval does not keep every question inside its staleness; empty when it does.
    problems: tuple[str, ...] = field(default_factory=tuple)
    # The interval before it was fitted to the horizons.
    unfitted: timedelta | None = None
    # The forecast questions whose horizons the interval was fitted to.
    questions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def feasible(self) -> bool:
        return self.interval is not None and not self.problems


def derive_interval(manifest: Manifest) -> IntervalDerivation:
    """Apply the interval rule to the manifest's sources, profiles and contract.

    Terms that are not known are left out. With a source period known, an
    interval longer than it is shortened to a whole number of source periods,
    so that every interval holds the same number of readings.
    """
    view = ManifestView(manifest)
    refreshes = [s.refresh_period for s in view.sources.values() if s.refresh_period is not None]
    rates = [s.rate_limit_period for s in view.sources.values() if s.rate_limit_period is not None]
    source_period = min(refreshes, default=None)
    rate_period = max(rates, default=None)
    bounds = freshness_bounds(view)
    tightest = bounds[0] if bounds else None
    change_time = _change_time(view)
    stalenesses = [q.staleness for q in view.questions.values() if q.staleness is not None]
    horizons = _horizons(view)
    horizon = _gcd(list(horizons.values()))
    inputs = SyncDerivation(
        source_period=source_period,
        rate_period=rate_period,
        min_staleness=min(stalenesses, default=None),
        freshness_bound=tightest.value if tightest else None,
        change_time=change_time,
        horizon=horizon,
        rule=RULE,
    )

    lower = {"source_period": source_period, "rate_period": rate_period}
    upper = {
        "freshness_bound": tightest.value if tightest else None,
        "change_time": change_time / 2 if change_time is not None else None,
    }
    floors = {name: value for name, value in lower.items() if value is not None}
    ceilings = {name: value for name, value in upper.items() if value is not None}
    if not floors and not ceilings and horizon is None:
        return IntervalDerivation(None, inputs)

    binding, interval = None, None
    if ceilings:
        binding = min(ceilings, key=lambda name: ceilings[name])
        interval = ceilings[binding]
        if source_period is not None and interval > source_period:
            interval = (interval // source_period) * source_period
    if floors:
        name = max(floors, key=lambda name: floors[name])
        if interval is None or floors[name] >= interval:
            binding, interval = name, floors[name]
    unfitted = interval
    problems = []
    if horizon is not None and (interval is None or interval > _ZERO):
        fitted = horizon if interval is None else fit_to_horizon(interval, horizon)
        if fitted != interval:
            binding, interval = HORIZON, fitted
        if floors and interval < max(floors.values()):
            name = max(floors, key=lambda name: floors[name])
            problems.append(_below_floor(horizons, unfitted, interval, name, floors[name]))
    assert interval is not None

    if interval <= _ZERO:
        problems.append(f"the rule gives {fmt_duration(interval)}, which is not an interval")
    for bound in bounds:
        if interval <= bound.value:
            break
        if bound.source is None:
            problems.append(
                f"question '{bound.question}' needs data no older than "
                f"{fmt_duration(bound.staleness)}, but the interval cannot be shorter than "
                f"{fmt_duration(interval)} ({binding})"
            )
        else:
            assert bound.refresh_period is not None
            problems.append(
                f"question '{bound.question}' needs data no older than "
                f"{fmt_duration(bound.staleness)}; source '{bound.source}' changes every "
                f"{fmt_duration(bound.refresh_period)}, which leaves at most "
                f"{fmt_duration(max(bound.value, _ZERO))} for the interval, and it cannot be "
                f"shorter than {fmt_duration(interval)} ({binding})"
            )
    sync = manifest.sync
    if sync is not None and sync.minimum_interval is not None and interval < sync.minimum_interval:
        problems.append(
            f"the rule gives {fmt_duration(interval)}, below minimum_interval "
            f"{fmt_duration(sync.minimum_interval)}"
        )
    if sync is not None and sync.maximum_interval is not None and interval > sync.maximum_interval:
        problems.append(
            f"the rule gives {fmt_duration(interval)}, above maximum_interval "
            f"{fmt_duration(sync.maximum_interval)}"
        )
    return IntervalDerivation(
        interval, inputs, binding, tuple(problems), unfitted, tuple(sorted(horizons))
    )


def _below_floor(
    horizons: dict[str, timedelta],
    unfitted: timedelta | None,
    interval: timedelta,
    floor_name: str,
    floor: timedelta,
) -> str:
    """Why no interval both divides the horizons and respects the sources' lower bound."""
    # The questions that cannot be met alone; all of them when only their mix fails.
    alone = [
        qid
        for qid, value in horizons.items()
        if unfitted is None or fit_to_horizon(unfitted, value) < floor
    ]
    named = ", ".join(f"'{qid}' ({fmt_duration(horizons[qid])})" for qid in alone or horizons)
    noun = "question" if len(alone or horizons) == 1 else "questions"
    return (
        f"the interval has to divide the horizon of {noun} {named}; the longest interval that "
        f"does is {fmt_duration(interval)}, below the {floor_name} {fmt_duration(floor)} it "
        f"cannot be shorter than: make each horizon a whole multiple of {fmt_duration(floor)}"
    )
