"""How long a sync interval a question allows, given the sources it is answered from.

A run takes only intervals that have ended, and a source hands over something
new once per refresh period. Just before a run, the newest reading the twin
holds from a source can therefore be as old as one interval plus one refresh
period of that source. A question that tolerates data no older than its
staleness bounds the interval by `staleness - refresh period`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .view import ManifestView


@dataclass(frozen=True)
class FreshnessBound:
    """The longest interval one question allows, given one source it is answered from."""

    value: timedelta
    question: str
    staleness: timedelta
    # None when no source the question depends on states a refresh period.
    source: str | None = None
    refresh_period: timedelta | None = None


def freshness_bounds(view: ManifestView) -> list[FreshnessBound]:
    """One bound per question with a staleness and per source it depends on, tightest first."""
    bounds = []
    for qid, question in sorted(view.questions.items()):
        if question.staleness is None:
            continue
        periods = [
            (view.sources[sid].refresh_period, sid)
            for sid in view.dependencies(question)
            if view.sources[sid].refresh_period is not None
        ]
        if not periods:
            bounds.append(FreshnessBound(question.staleness, qid, question.staleness))
        for period, sid in periods:
            bounds.append(
                FreshnessBound(question.staleness - period, qid, question.staleness, sid, period)
            )
    return sorted(bounds, key=lambda bound: (bound.value, bound.question, bound.source or ""))
