"""How a backtest is laid out, when a candidate is accepted, and which one is selected.

Plain arithmetic on what the manifest declares and what a backtest measured:
the API's jobs call these, so nothing here imports a model library.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from dta_core.families import ORDER

from .errors import bad_params

if TYPE_CHECKING:
    from dta_core.manifest.models import Acceptance

ABOVE_THRESHOLD = "above_threshold"
NOT_BETTER_THAN_BASELINE = "not_better_than_baseline"
# Errors closer than this, relative to the larger, are the same error.
_TIE = 1e-12


@dataclass(frozen=True)
class Plan:
    """A walk-forward evaluation in steps of the grid."""

    # Steps predicted from each origin, and steps between two origins.
    horizon: int
    step: int
    # Origins the history has room for, and whether they are as many as the spec asks.
    windows: int
    enough: bool


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    # Why not, as codes; empty when accepted.
    reasons: tuple[str, ...]
    # The error in the acceptance's measure.
    error: float
    # 1 - error / the baseline's error; None without a baseline or when its error is zero.
    skill: float | None


def _positive(value: timedelta, what: str) -> None:
    if value <= timedelta(0):
        raise bad_params(f"{what} must be positive, got {value}", param=what)


def horizon_steps(horizon: timedelta, step: timedelta) -> int:
    """Steps of the grid that cover the horizon: the smallest whole number of them."""
    _positive(step, "step")
    _positive(horizon, "horizon")
    return -(-horizon // step)


def plan(
    rows: int,
    *,
    initial_window: timedelta,
    origin_step: timedelta,
    horizon: timedelta,
    step: timedelta,
    min_folds: int,
) -> Plan:
    """The folds a history of `rows` grid times (the longest series) has room for: the
    first origin after the initial window, the last a horizon before the end."""
    ahead = horizon_steps(horizon, step)
    _positive(initial_window, "initial_window")
    _positive(origin_step, "origin_step")
    if origin_step % step:
        raise bad_params(
            f"the time between origins ({origin_step}) is not a whole multiple of the "
            f"step of the grid ({step})",
            param="origin_step",
        )
    window = -(-initial_window // step)
    between = origin_step // step
    windows = (rows - window - ahead) // between + 1 if rows >= window + ahead else 0
    return Plan(horizon=ahead, step=between, windows=windows, enough=windows >= min_folds)


def choose_refit(first_fit_s: float, windows: int, budget_s: float) -> int | bool:
    """How often a backtest can afford to fit again: the smallest k, fitting at every
    k-th origin, for which the fits take at most half the budget, judged by how long
    the first one took. False (fit once) when even one fit in `windows` is too many."""
    allowed = budget_s / 2.0
    for every in range(1, windows + 1):
        if first_fit_s * math.ceil(windows / every) <= allowed:
            return every
    return False


def judge(
    scores: Mapping[str, float],
    baseline_scores: Mapping[str, float] | None,
    acceptance: Acceptance,
) -> Verdict:
    """Accepted when the error is within the threshold and, where a baseline is
    declared, strictly below the baseline's on the same folds."""
    if acceptance.metric is None or acceptance.threshold is None:
        raise bad_params("the acceptance names no metric or no threshold", param="acceptance")
    metric = acceptance.metric.value
    if metric not in scores:
        raise bad_params(f"the backtest did not score {metric}", param="metrics")
    error = float(scores[metric])
    reasons = []
    # Written so that an error that is not a number is never accepted.
    if not error <= acceptance.threshold:
        reasons.append(ABOVE_THRESHOLD)
    skill = None
    if acceptance.baseline is not None:
        if baseline_scores is None or metric not in baseline_scores:
            raise bad_params(
                f"the baseline {acceptance.baseline} was not scored in {metric}", param="baseline"
            )
        baseline = float(baseline_scores[metric])
        if not error < baseline:
            reasons.append(NOT_BETTER_THAN_BASELINE)
        if baseline != 0 and not math.isnan(baseline):
            skill = 1.0 - error / baseline
    return Verdict(accepted=not reasons, reasons=tuple(reasons), error=error, skill=skill)


def select(verdicts: Mapping[str, Verdict], families: Mapping[str, str]) -> str | None:
    """The accepted candidate with the lowest error. Equal errors go to the simpler
    family: the one earlier among the shipped families, a family of the twin's own after
    all of them, then the candidate whose id sorts first. None when none is accepted."""
    accepted = {name: verdict for name, verdict in verdicts.items() if verdict.accepted}
    if not accepted:
        return None
    best = min(verdict.error for verdict in accepted.values())
    tied = [
        name
        for name, verdict in accepted.items()
        if math.isclose(verdict.error, best, rel_tol=_TIE, abs_tol=0.0)
    ]

    def simplicity(name: str) -> tuple[int, str]:
        family = families[name]
        return (ORDER.index(family) if family in ORDER else len(ORDER), name)

    return min(tied, key=simplicity)
