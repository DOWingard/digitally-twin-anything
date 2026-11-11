"""The model families shipped with the runtime.

A candidate of a model spec names one of these, or code of the twin's own as
`module:name`. The order is from the simplest on: among candidates that are
equally good, the earlier one is selected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FamilySpec:
    name: str
    description: str
    # Whether it may serve as the naive model a candidate has to beat.
    baseline: bool = False


_FAMILIES = (
    FamilySpec("persistence", "The last value seen, carried forward", baseline=True),
    FamilySpec("seasonal_naive", "The value one season earlier", baseline=True),
    FamilySpec("theta", "Theta method with the season removed and put back"),
    FamilySpec("ets", "Exponential smoothing, its form chosen by information criterion"),
    FamilySpec("arima", "ARIMA, its orders chosen by information criterion"),
    FamilySpec("mstl", "Trend and several seasons separated, the trend forecast"),
    FamilySpec("ridge_lags", "Linear regression on lags of the target and the calendar"),
    FamilySpec("gbm_lags", "Gradient-boosted trees on lags of the target and the calendar"),
)

FAMILIES: Mapping[str, FamilySpec] = {found.name: found for found in _FAMILIES}
# Simplest first.
ORDER: tuple[str, ...] = tuple(FAMILIES)
# What `models.<id>.acceptance.baseline` names, and the family that stands for it.
BASELINES: Mapping[str, str] = {"persistence": "persistence", "seasonal": "seasonal_naive"}

__all__ = ["BASELINES", "FAMILIES", "ORDER", "FamilySpec"]
