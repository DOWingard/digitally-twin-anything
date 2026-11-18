"""Checks of the twin's time grid: the sync interval, in either tier.

A live twin syncs on `sync.interval`; the models and scenarios of any twin step
on it. The twin refuses to place a model on no grid, and refuses a backtest
whose origins are not on it, so a forecast question needs an interval that its
horizon and its model's backtest step are whole multiples of.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from .findings import Issue, check
from .view import FORECAST_KINDS, ManifestView, fmt_duration

DERIVE = "dta sync derive --write"


def interval_of(view: ManifestView) -> timedelta | None:
    sync = view.manifest.sync
    return sync.interval if sync is not None else None


def on_grid(value: timedelta, interval: timedelta) -> bool:
    return interval > timedelta(0) and value % interval == timedelta(0)


def next_multiple(value: timedelta, interval: timedelta) -> timedelta:
    """The smallest whole multiple of `interval` that is not shorter than `value`."""
    return -(-value // interval) * interval


@check("grid.no_interval")
def no_interval(view: ManifestView) -> Iterator[Issue]:
    if interval_of(view) is not None:
        return
    needs = [
        f"question '{qid}' is {question.kind.value}"
        for qid, question in sorted(view.questions.items())
        if question.kind in FORECAST_KINDS
    ]
    needs += [f"models.{mid} is declared" for mid in sorted(view.models)]
    if needs:
        yield Issue(
            "sync.interval",
            f"{needs[0]}, and a twin's models step on sync.interval, which is not set (in a "
            f"static twin too): run {DERIVE}",
        )


@check("grid.horizon")
def horizon(view: ManifestView) -> Iterator[Issue]:
    interval = interval_of(view)
    if interval is None or interval <= timedelta(0):
        return
    for qid, question in sorted(view.questions.items()):
        if question.kind not in FORECAST_KINDS or question.horizon is None:
            continue
        if on_grid(question.horizon, interval):
            continue
        yield Issue(
            f"contract.questions.{qid}.horizon",
            f"horizon {fmt_duration(question.horizon)} is not a whole multiple of "
            f"sync.interval {fmt_duration(interval)}, the step forecasts are made in: run "
            f"{DERIVE}, which fits the interval to the horizons",
        )


@check("grid.backtest_step")
def backtest_step(view: ManifestView) -> Iterator[Issue]:
    interval = interval_of(view)
    if interval is None or interval <= timedelta(0):
        return
    for mid, model in sorted(view.models.items()):
        backtest = model.backtest
        if backtest is None or backtest.step is None or backtest.step <= timedelta(0):
            continue
        if on_grid(backtest.step, interval):
            continue
        yield Issue(
            f"models.{mid}.backtest.step",
            f"the time between forecast origins, {fmt_duration(backtest.step)}, is not a whole "
            f"multiple of sync.interval {fmt_duration(interval)}: make it one, such as "
            f"{fmt_duration(next_multiple(backtest.step, interval))}",
        )
