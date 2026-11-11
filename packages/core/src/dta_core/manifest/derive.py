"""The sync interval a live twin's contract and its sources call for.

The questions bound the interval from above: each allows at most its staleness
less the refresh period of a source it is answered from, and a property that
changes fast wants at least two looks per characteristic time. The sources
bound it from below: syncing faster than the fastest source changes brings
nothing, and a rate limit forbids it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .checks.freshness import freshness_bounds
from .checks.view import ManifestView, fmt_duration
from .models import Manifest, SyncDerivation

RULE = "max(source_period, rate_period, min(freshness_bound, change_time / 2))"
_ZERO = timedelta(0)


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
    # The term that decided the interval: one of the rule's four names.
    binding: str | None = None
    # Why the interval does not keep every question inside its staleness; empty when it does.
    problems: tuple[str, ...] = field(default_factory=tuple)

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
    inputs = SyncDerivation(
        source_period=source_period,
        rate_period=rate_period,
        min_staleness=min(stalenesses, default=None),
        freshness_bound=tightest.value if tightest else None,
        change_time=change_time,
        rule=RULE,
    )

    lower = {"source_period": source_period, "rate_period": rate_period}
    upper = {
        "freshness_bound": tightest.value if tightest else None,
        "change_time": change_time / 2 if change_time is not None else None,
    }
    floors = {name: value for name, value in lower.items() if value is not None}
    ceilings = {name: value for name, value in upper.items() if value is not None}
    if not floors and not ceilings:
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
    assert interval is not None

    problems = []
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
    return IntervalDerivation(interval, inputs, binding, tuple(problems))
