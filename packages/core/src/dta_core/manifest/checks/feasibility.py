"""Checks that the contract can be met with the declared sources and their measurements.

A question depends on a source when one of its `Type.property` targets is mapped
by that source. Comparisons are made only between values that are present; a
missing measurement is a gap, not a finding.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from ..models import ErrorMetric
from .findings import Issue, Severity, check
from .freshness import freshness_bounds
from .view import FORECAST_KINDS, ManifestView, fmt_duration

# Metrics in the unit of the signal, so they compare directly with its noise floor.
_ABSOLUTE_METRICS = frozenset({ErrorMetric.mae, ErrorMetric.rmse, ErrorMetric.max_abs})
_ZERO = timedelta(0)


@check("coverage.unmapped_target", Severity.warning)
def unmapped_target(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        for ref in view.well_formed_targets(question):
            if not view.mapped_columns(ref):
                yield Issue(f"contract.questions.{qid}.targets", f"no source maps '{ref}'")


@check("feasible.staleness", live_only=True)
def staleness(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        if question.staleness is None:
            continue
        for sid in view.dependencies(question):
            period = view.sources[sid].refresh_period
            if period is not None and question.staleness < period:
                yield Issue(
                    f"contract.questions.{qid}.staleness",
                    f"staleness {fmt_duration(question.staleness)} is shorter than the "
                    f"refresh period {fmt_duration(period)} of source '{sid}'",
                )


@check("feasible.interval", live_only=True)
def interval(view: ManifestView) -> Iterator[Issue]:
    sync = view.manifest.sync
    if sync is None or sync.interval is None:
        return
    value = sync.interval
    shown = fmt_duration(value)
    # Sources change at different rates. Syncing faster than the fastest of them
    # brings nothing; syncing faster than a slower one only reads it more often
    # than it changes, which is allowed. A rate limit binds every source it is on.
    refreshes = [
        (source.refresh_period, sid)
        for sid, source in sorted(view.sources.items())
        if source.refresh_period is not None
    ]
    if refreshes:
        fastest, sid = min(refreshes)
        if value < fastest:
            yield Issue(
                "sync.interval",
                f"interval {shown} is shorter than the refresh period "
                f"{fmt_duration(fastest)} of source '{sid}', the fastest one",
            )
    for sid, source in sorted(view.sources.items()):
        period = source.rate_limit_period
        if period is not None and value < period:
            yield Issue(
                "sync.interval",
                f"interval {shown} is shorter than the rate limit period "
                f"{fmt_duration(period)} of source '{sid}'",
            )
    # Tightest first, and only the tightest one that fails is reported: one
    # interval that is too long is one finding, however many questions it fails.
    for bound in freshness_bounds(view):
        if value <= bound.value:
            break
        if bound.value < _ZERO:
            continue  # the staleness is shorter than the source's period: said there
        if bound.source is None:
            yield Issue(
                "sync.interval",
                f"interval {shown} is longer than the staleness "
                f"{fmt_duration(bound.staleness)} of question '{bound.question}'",
            )
        else:
            assert bound.refresh_period is not None
            yield Issue(
                "sync.interval",
                f"interval {shown} plus the refresh period "
                f"{fmt_duration(bound.refresh_period)} of source '{bound.source}' is "
                f"{fmt_duration(value + bound.refresh_period)}, longer than the staleness "
                f"{fmt_duration(bound.staleness)} of question '{bound.question}': just "
                "before a run its data is that old",
            )
        break
    if sync.minimum_interval is not None and value < sync.minimum_interval:
        yield Issue(
            "sync.interval",
            f"interval {shown} is below minimum_interval {fmt_duration(sync.minimum_interval)}",
        )
    if sync.maximum_interval is not None and value > sync.maximum_interval:
        yield Issue(
            "sync.interval",
            f"interval {shown} is above maximum_interval {fmt_duration(sync.maximum_interval)}",
        )


@check("feasible.noise")
def noise(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        tolerance = question.tolerance
        if tolerance is None or tolerance.value is None:
            continue
        if tolerance.metric not in _ABSOLUTE_METRICS:
            continue
        for ref in view.well_formed_targets(question):
            for mapped in view.mapped_columns(ref):
                profile = mapped.column_profile
                if profile is None or profile.noise_sigma is None:
                    continue
                if tolerance.value < profile.noise_sigma:
                    yield Issue(
                        f"contract.questions.{qid}.tolerance.value",
                        f"tolerance {tolerance.value:g} is below the noise floor "
                        f"{profile.noise_sigma:g} of column '{mapped.column}' in source "
                        f"'{mapped.source_id}', which '{ref}' is read from",
                    )


def _longest_history(view: ManifestView, targets: list[str]) -> timedelta | None:
    """Longest time span among the profiled datasets the targets are read from.

    None when no such dataset has been profiled yet. A profiled dataset without a
    time span counts as having no history.
    """
    spans = []
    for ref in targets:
        for mapped in view.mapped_columns(ref):
            dataset = mapped.dataset
            if dataset is None:
                continue
            if dataset.history_start is not None and dataset.history_end is not None:
                spans.append(dataset.history_end - dataset.history_start)
            else:
                spans.append(timedelta(0))
    return max(spans) if spans else None


@check("feasible.history")
def history(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        if question.kind not in FORECAST_KINDS or question.model is None:
            continue
        model = view.models.get(question.model)
        backtest = model.backtest if model else None
        if backtest is None or backtest.initial_window is None or backtest.step is None:
            continue
        longest = _longest_history(view, view.well_formed_targets(question))
        if longest is None:
            continue
        needed = backtest.initial_window + backtest.min_folds * backtest.step
        if longest >= needed:
            continue
        severity = Severity.warning if question.provisional else Severity.error
        note = " (question is provisional)" if question.provisional else ""
        yield Issue(
            f"models.{question.model}.backtest",
            f"question '{qid}' has at most {fmt_duration(longest)} of history; the backtest "
            f"needs {fmt_duration(needed)} (initial window + {backtest.min_folds} x step){note}",
            severity,
        )


@check("feasible.acceptance", Severity.warning)
def acceptance(view: ManifestView) -> Iterator[Issue]:
    for mid, model in sorted(view.models.items()):
        accept = model.acceptance
        question = view.questions.get(model.question) if model.question else None
        if accept is None or question is None or question.tolerance is None:
            continue
        tolerance = question.tolerance
        if accept.metric is None or accept.threshold is None or tolerance.value is None:
            continue
        if accept.metric == tolerance.metric and accept.threshold > tolerance.value:
            yield Issue(
                f"models.{mid}.acceptance.threshold",
                f"acceptance threshold {accept.threshold:g} exceeds the tolerance "
                f"{tolerance.value:g} of question '{model.question}' on the same metric",
            )
