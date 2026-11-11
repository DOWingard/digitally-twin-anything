"""What a model family is: how it is fitted and how it predicts.

A family works on frames in the long form the forecasting libraries use: one
row per series and time, `unique_id` (the object's uid), `ds` (the time of the
version, UTC) and `y` (the target), followed by any further columns the model
spec names as features. Frames are pandas DataFrames.

Shipped families are thin adapters of library models; a twin's own family
(a physical or hybrid model, for one) implements the same four members and
runs in the worker like the shipped ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

UNIQUE_ID = "unique_id"
DS = "ds"
Y = "y"
# Column of a prediction's central value.
MEAN = "mean"


def low(level: float) -> str:
    """Column of the lower end of the interval with this coverage in percent."""
    return f"lo_{level:g}"


def high(level: float) -> str:
    return f"hi_{level:g}"


def prediction_columns(levels: Sequence[float]) -> list[str]:
    """Columns of a prediction frame, in order."""
    columns = [UNIQUE_ID, DS, MEAN]
    for level in levels:
        columns += [low(level), high(level)]
    return columns


@dataclass(frozen=True)
class Intervention:
    """A property of an object held at a value over a time, as a what-if assumes it."""

    # `Type.property`.
    target: str
    uid: str
    value: Any
    start: datetime
    # None: until the end of the horizon.
    end: datetime | None = None


@runtime_checkable
class Family(Protocol):
    """One way of predicting a target. Made once per run by its factory, which is called
    with the step of the frame's grid and, for code of the twin's own, a reader of the twin
    (`factory(step=..., twin=...)`)."""

    # Whether its predictions hold under changes to controls it never saw varied. Only a
    # family that models the mechanism, or was fitted on data in which the controls were
    # varied, may say so.
    interventional: bool

    def fit(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> Any:
        """The fitted state: whatever `predict` needs, picklable."""
        ...

    def predict(
        self,
        state: Any,
        history: pd.DataFrame,
        horizon: int,
        *,
        levels: Sequence[float] = (),
        drivers: pd.DataFrame | None = None,
        interventions: Sequence[Intervention] = (),
    ) -> pd.DataFrame:
        """`horizon` steps after the last time of `history`, per series: the columns of
        `prediction_columns(levels)`. `drivers` holds known future values of features;
        a family that is not interventional is never given interventions."""
        ...

    def validity(self, frame: pd.DataFrame) -> Mapping[str, tuple[float, float]]:
        """Per input column, the range of values the fit saw; a prediction asked for
        beyond it is still made, and marked."""
        ...


class Backtests(Protocol):
    """An optional member of a family: its own walk-forward evaluation, when the library
    has one that is cheaper than fitting and predicting fold by fold."""

    def cross_validate(
        self,
        frame: pd.DataFrame,
        params: Mapping[str, Any],
        *,
        horizon: int,
        step: int,
        windows: int,
        refit: int | bool,
        levels: Sequence[float] = (),
    ) -> pd.DataFrame:
        """One row per series, origin (`cutoff`) and predicted time: `unique_id`, `ds`,
        `cutoff`, `y` and the prediction columns."""
        ...
