"""What a check measures of a question, as plain functions of plain data.

The error of one origin is the question's metric over every series and step the
origin predicted, computed the way the backtests compute it: the metric per series
over its points, then one number across series (the mean, or the largest for
`max_abs`). The window error is the mean of the origins' errors. A breach and a
warning are read off those.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

METRICS = ("mae", "rmse", "mape", "max_abs", "error_rate")
# How the per-series values of a metric become one.
_ACROSS = {"mae": "mean", "rmse": "mean", "mape": "mean", "max_abs": "max", "error_rate": "mean"}

# A breach needs at least this many origins in the window.
MIN_ORIGINS = 3
# The warning sum: S <- max(0, S + e/epsilon - WARN_SLACK); a warning when S > WARN_LIMIT.
WARN_SLACK = 0.8
WARN_LIMIT = 1.0


class Undefined(ValueError):
    """The metric has no value on these points."""


@dataclass(frozen=True)
class OriginScore:
    """The error of one origin, its number of points, and per series the mean signed
    residual (observed less predicted)."""

    error: float
    n: int
    residuals: dict[str, float]


def score_origin(
    series: Sequence[str], observed: Sequence[float], predicted: Sequence[float], metric: str
) -> OriginScore:
    """The metric over every point given; raises `Undefined` when it has no value there."""
    if metric not in _ACROSS:
        raise Undefined(f"unknown metric {metric!r}; the metrics are {', '.join(METRICS)}")
    y = np.asarray(observed, dtype=np.float64)
    yhat = np.asarray(predicted, dtype=np.float64)
    if not (len(series) == y.size == yhat.size):
        raise ValueError("series, observed and predicted differ in length")
    if y.size == 0:
        raise Undefined("no point has both a prediction and an observation")
    names, index = np.unique(np.asarray(series, dtype=object), return_inverse=True)
    counts = np.bincount(index, minlength=names.size).astype(np.float64)
    residual = y - yhat

    def per_series_mean(values: np.ndarray) -> np.ndarray:
        return np.bincount(index, weights=values, minlength=names.size) / counts

    if metric == "mae":
        per_series = per_series_mean(np.abs(residual))
    elif metric == "rmse":
        per_series = np.sqrt(per_series_mean(residual**2))
    elif metric == "mape":
        if np.any(y == 0):
            raise Undefined(f"mape is not defined where the truth is zero ({int(np.sum(y == 0))})")
        per_series = per_series_mean(np.abs(residual) / np.abs(y))
    elif metric == "max_abs":
        per_series = np.full(names.size, -np.inf)
        np.maximum.at(per_series, index, np.abs(residual))
    else:
        if not np.all(y == np.round(y)):
            raise Undefined("error_rate needs a truth of whole numbers")
        per_series = per_series_mean((np.round(yhat) != y).astype(np.float64))
    error = float(per_series.max() if _ACROSS[metric] == "max" else per_series.mean())
    signed = per_series_mean(residual)
    return OriginScore(
        error=error,
        n=int(y.size),
        residuals={str(name): float(value) for name, value in zip(names, signed, strict=True)},
    )


def in_window(origin: datetime, *, now: datetime, horizon: timedelta, window: timedelta) -> bool:
    """Whether an origin's forecast ends inside (now - window, now]."""
    return now - window < origin + horizon <= now


def window_error(
    scored: Iterable[tuple[datetime, float]],
    *,
    now: datetime,
    horizon: timedelta,
    window: timedelta,
    fitted_to: datetime | None,
) -> tuple[float | None, int]:
    """The mean error of the scored origins whose forecast ends inside the window and
    that the active model had not seen, and how many there are."""
    kept = [
        error
        for origin, error in scored
        if in_window(origin, now=now, horizon=horizon, window=window)
        and (fitted_to is None or origin >= fitted_to)
    ]
    if not kept:
        return None, 0
    return float(np.mean(kept)), len(kept)


def breach(error: float | None, n: int, tolerance: float) -> bool:
    return error is not None and n >= MIN_ORIGINS and error > tolerance


def warning_sum(previous: float, error: float, tolerance: float) -> float:
    """The one-sided sum the warning is raised on, after one more origin."""
    return max(0.0, previous + error / tolerance - WARN_SLACK)


def warning_due(
    total: float, last_warned: datetime | None, *, now: datetime, window: timedelta
) -> bool:
    """Whether a warning is raised: the sum is over its limit and no warning was raised
    within the window."""
    return total > WARN_LIMIT and (last_warned is None or now - last_warned >= window)


@dataclass(frozen=True)
class Evidence:
    """Origins at or after a cut, and their mean error; none when there is no cut."""

    n: int
    error: float | None
    cut: datetime | None

    def holds(self, tolerance: float) -> bool | None:
        """True or false once there are enough origins; None before."""
        if self.n < MIN_ORIGINS or self.error is None:
            return None
        return self.error <= tolerance


def evidence(scored: Iterable[tuple[datetime, float]], cut: datetime | None) -> Evidence:
    if cut is None:
        return Evidence(0, None, None)
    kept = [error for origin, error in scored if origin >= cut]
    return Evidence(len(kept), float(np.mean(kept)) if kept else None, cut)
