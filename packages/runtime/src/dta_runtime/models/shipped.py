"""The shipped families: library models behind `protocol.families.Family`.

Every method here hands a frame to statsforecast or mlforecast and renames
what comes back. Fitting, walking forward and intervals are the libraries';
the adapters decide parameters and defaults, say how many rows a series
needs, and apply a fitted state to new history without fitting again:
statsforecast models through their `forward`, mlforecast through `predict`
with new data.

Frames arrive prepared (`series.prepare`): every series complete and regular,
ending at the frame's last time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, ClassVar

import pandas as pd
from dta_core.families import ORDER
from mlforecast import MLForecast
from mlforecast.utils import PredictionIntervals
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from statsforecast import StatsForecast
from statsforecast.models import MSTL, AutoARIMA, AutoETS, AutoTheta, Naive, SeasonalNaive

from ..protocol.families import DS, MEAN, UNIQUE_ID, Intervention, Y, high, low
from .errors import bad_frame, bad_params
from .series import features_of, require_rows, value_ranges

DAY_S = 86_400
# Fewest rows the libraries' automatic models can be fitted on, whatever the season.
_AUTO_FLOOR = 10
# Windows a lag family's intervals are calibrated on at most, each a horizon long.
CALIBRATION_WINDOWS = 10
# A season from which the library's exact search over seasonal ARIMA models takes minutes
# per series; from here on the search compares candidates by conditional sums of squares
# and fits only the chosen one exactly, as the method's reference implementation does.
_ARIMA_APPROXIMATE_FROM = 13
_DATE_FEATURES = frozenset(
    {"year", "quarter", "month", "week", "day", "dayofweek", "dayofyear", "hour", "minute"}
)

Check = Callable[[Any], bool]


def _whole(minimum: int) -> Check:
    return lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _number(minimum: float, *, above: bool = False) -> Check:
    def check(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        return value > minimum if above else value >= minimum

    return check


def _list_of(item: Check) -> Check:
    return lambda value: (
        isinstance(value, list)
        and bool(value)
        and all(item(found) for found in value)
        and len(set(value)) == len(value)
    )


def _ets_form(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 3
        and value[0] in "ZAM"
        and value[1] in "ZNAM"
        and value[2] in "ZNAM"
    )


def level_values(levels: Sequence[float]) -> list[int | float]:
    """Levels as the libraries name their columns by: whole ones as integers."""
    found: list[int | float] = []
    for level in levels:
        if isinstance(level, bool) or not isinstance(level, int | float) or not 0 < level < 100:
            raise bad_params(f"a level is a coverage in percent between 0 and 100, got {level!r}",
                             param="levels")  # fmt: skip
        found.append(int(level) if float(level).is_integer() else float(level))
    return found


def _named(frame: pd.DataFrame, levels: Sequence[int | float]) -> pd.DataFrame:
    """A library's forecast columns under the protocol's names."""
    names = {f"{MEAN}-lo-{level}": low(level) for level in levels}
    names.update({f"{MEAN}-hi-{level}": high(level) for level in levels})
    return frame.rename(columns=names)


def _every(refit: int | bool) -> int | bool:
    # Fitting at every origin is one thing, however it is written.
    return True if refit is not False and refit == 1 else refit


class Shipped:
    """What the shipped families share: parameters checked against what each takes, and
    notes of what a caller has to know, kept for the handler to report."""

    name: ClassVar[str]
    takes: ClassVar[Mapping[str, Check]] = {}
    interventional = False

    def __init__(self, *, step: timedelta) -> None:
        self.step = step
        self.freq = pd.tseries.frequencies.to_offset(pd.Timedelta(step))
        # One day in steps of the grid: the season a twin's series most often has.
        self.day = max(1, round(DAY_S / step.total_seconds()))
        self.notes: list[str] = []

    def note(self, code: str) -> None:
        if code not in self.notes:
            self.notes.append(code)

    def checked(self, params: Mapping[str, Any]) -> dict[str, Any]:
        for name, value in params.items():
            if name not in self.takes:
                takes = ", ".join(self.takes) or "no parameter"
                raise bad_params(
                    f"{self.name} takes no parameter {name!r} (it takes {takes})",
                    param=name,
                    family=self.name,
                )
            if not self.takes[name](value):
                raise bad_params(
                    f"{self.name}: the parameter {name!r} cannot be {value!r}",
                    param=name,
                    family=self.name,
                )
        return dict(params)

    def inputs(self, features: Sequence[str]) -> list[str]:
        """The feature columns the family reads."""
        return list(features)

    def validity(self, frame: pd.DataFrame) -> dict[str, tuple[float, float]]:
        return value_ranges(frame, [Y, *self.inputs(features_of(frame))])

    def levels(self, state: Any) -> str | list[float]:
        """The coverages a fitted state gives intervals for: "any", or none."""
        return "any"


class _Stats(Shipped):
    """A statsforecast model, fitted per series on the target alone."""

    # Fewest rows of a series, before any season is counted.
    floor: ClassVar[int] = 1

    def model(self, settings: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    def settings(self, params: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.checked(params)
        if "season_length" in self.takes:
            settings.setdefault("season_length", self.day)
        return settings

    def season(self, settings: Mapping[str, Any]) -> int:
        return int(settings.get("season_length", 1))

    def need(self, settings: Mapping[str, Any]) -> tuple[int, str]:
        season = self.season(settings)
        if season > 1 and 2 * season >= self.floor:
            return 2 * season, f"two seasons of {season} steps; a shorter season_length needs fewer"
        return self.floor, f"the fewest {self.name} can be fitted on"

    def inputs(self, features: Sequence[str]) -> list[str]:
        if features:
            self.note("features_unused")
        return []

    def engine(self, settings: Mapping[str, Any]) -> StatsForecast:
        return StatsForecast(models=[self.model(settings)], freq=self.freq, n_jobs=1)

    def fit(self, frame: pd.DataFrame, params: Mapping[str, Any], *, horizon: int) -> Any:
        settings = self.settings(params)
        require_rows(frame, *self.need(settings))
        engine = self.engine(settings)
        engine.fit(frame[[UNIQUE_ID, DS, Y]])
        fitted = {str(uid): row[0] for uid, row in zip(engine.uids, engine.fitted_, strict=True)}
        return {"settings": settings, "models": fitted}

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
        models = state["models"]
        known = history[history[UNIQUE_ID].isin(list(models))]
        unfitted = history[UNIQUE_ID].nunique() - known[UNIQUE_ID].nunique()
        if unfitted:
            # A model of this kind is fitted per series: a series it never saw has none.
            self.note(f"series_unfitted:{unfitted}")
        if known.empty:
            raise bad_frame("no series of the history is among those the state was fitted on")
        require_rows(known, *self.need(state["settings"]))
        wanted = level_values(levels)
        parts = []
        for uid, rows in known.groupby(UNIQUE_ID, sort=True):
            # The fitted model applied to the series as it is now; nothing is fitted again.
            found = models[uid].forward(
                y=rows[Y].to_numpy(dtype=float), h=horizon, level=wanted or None
            )
            part = {
                UNIQUE_ID: uid,
                DS: pd.date_range(rows[DS].iloc[-1] + self.freq, periods=horizon, freq=self.freq),
                MEAN: found["mean"],
            }
            for level in wanted:
                part[low(level)] = found[f"lo-{level}"]
                part[high(level)] = found[f"hi-{level}"]
            parts.append(pd.DataFrame(part))
        return pd.concat(parts, ignore_index=True)

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
        settings = self.settings(params)
        need, why = self.need(settings)
        test = horizon + step * (windows - 1)
        require_rows(frame, need + test, f"{why}, before the first of {windows} origins")
        wanted = level_values(levels)
        folds = self.engine(settings).cross_validation(
            df=frame[[UNIQUE_ID, DS, Y]],
            h=horizon,
            step_size=step,
            n_windows=windows,
            refit=_every(refit),
            level=wanted or None,
        )
        return _named(folds, wanted)


class Persistence(_Stats):
    name = "persistence"

    def model(self, settings: Mapping[str, Any]) -> Any:
        return Naive(alias=MEAN)


class SeasonalNaiveFamily(_Stats):
    name = "seasonal_naive"
    takes = {"season_length": _whole(1)}

    def model(self, settings: Mapping[str, Any]) -> Any:
        return SeasonalNaive(season_length=settings["season_length"], alias=MEAN)


class Theta(_Stats):
    name = "theta"
    takes = {"season_length": _whole(1)}
    floor = _AUTO_FLOOR

    def model(self, settings: Mapping[str, Any]) -> Any:
        return AutoTheta(season_length=settings["season_length"], alias=MEAN)


class Ets(_Stats):
    name = "ets"
    takes = {"season_length": _whole(1), "model": _ets_form}
    floor = _AUTO_FLOOR

    def model(self, settings: Mapping[str, Any]) -> Any:
        return AutoETS(
            season_length=settings["season_length"], model=settings.get("model", "ZZZ"), alias=MEAN
        )


class Arima(_Stats):
    name = "arima"
    takes = {
        "season_length": _whole(1),
        "max_p": _whole(0),
        "max_q": _whole(0),
        "max_d": _whole(0),
    }
    floor = _AUTO_FLOOR

    def model(self, settings: Mapping[str, Any]) -> Any:
        orders = {name: settings[name] for name in ("max_p", "max_q", "max_d") if name in settings}
        season = settings["season_length"]
        return AutoARIMA(
            season_length=season,
            approximation=season >= _ARIMA_APPROXIMATE_FROM,
            alias=MEAN,
            **orders,
        )


class Mstl(_Stats):
    name = "mstl"
    takes = {"season_length": _list_of(_whole(2))}
    floor = _AUTO_FLOOR

    def settings(self, params: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.checked(params)
        settings.setdefault("season_length", [max(2, self.day)])
        return settings

    def season(self, settings: Mapping[str, Any]) -> int:
        return max(settings["season_length"])

    def model(self, settings: Mapping[str, Any]) -> Any:
        # The seasons are taken out, what is left is forecast by exponential smoothing
        # without a season, and the seasons are put back.
        return MSTL(
            season_length=sorted(settings["season_length"]),
            trend_forecaster=AutoETS(model="ZZN"),
            alias=MEAN,
        )


class _Lags(Shipped):
    """One scikit-learn regressor for every series, on lags of the target, the calendar
    and the frame's features, through mlforecast. The features' values over the horizon
    come as drivers."""

    takes: ClassVar[Mapping[str, Check]] = {
        "lags": _list_of(_whole(1)),
        "date_features": lambda value: (
            isinstance(value, list) and all(found in _DATE_FEATURES for found in value)
        ),
    }

    def regressor(self, settings: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    def levels(self, state: Any) -> str | list[float]:
        return "any" if state["interval_horizon"] else []

    def settings(self, params: Mapping[str, Any], rows: int, horizon: int) -> dict[str, Any]:
        """`rows`: what the shortest series has to be fitted on."""
        settings = self.checked(params)
        if "lags" not in settings:
            # A day and a week back where a series holds each twice over.
            seasons = [self.day, 7 * self.day]
            settings["lags"] = [1, 2, 3, 4] + [
                lag for lag in seasons if lag > 4 and rows >= 2 * lag + horizon
            ]
        settings["lags"] = sorted(settings["lags"])
        settings.setdefault("date_features", ["hour", "dayofweek"])
        return settings

    def engine(self, settings: Mapping[str, Any]) -> MLForecast:
        return MLForecast(
            models={MEAN: self.regressor(settings)},
            freq=self.freq,
            lags=settings["lags"],
            date_features=settings["date_features"],
        )

    @staticmethod
    def _calibration(rows: int, longest: int, horizon: int) -> int:
        """Windows of a horizon each the shortest series can spare for calibrating
        intervals, leaving a horizon of rows to fit on beyond its longest lag."""
        return min(CALIBRATION_WINDOWS, (rows - longest - horizon) // horizon)

    def fit(self, frame: pd.DataFrame, params: Mapping[str, Any], *, horizon: int) -> Any:
        rows = int(frame.groupby(UNIQUE_ID).size().min())
        settings = self.settings(params, rows, horizon)
        longest = settings["lags"][-1]
        require_rows(
            frame, longest + horizon, f"the longest lag ({longest}) plus the horizon ({horizon})"
        )
        windows = self._calibration(rows, longest, horizon)
        intervals = None
        if windows >= 2:
            # The scale of each series is kept beside the errors, so that they can be
            # carried to the history a prediction is made from.
            intervals = PredictionIntervals(n_windows=windows, h=horizon, scale_estimator="std")
        else:
            self.note("no_intervals")
        engine = self.engine(settings)
        engine.fit(frame, static_features=[], prediction_intervals=intervals)
        return {
            "settings": settings,
            "features": features_of(frame),
            "forecast": engine,
            # Steps ahead the intervals were calibrated for; 0: none.
            "interval_horizon": horizon if intervals is not None else 0,
        }

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
        features = state["features"]
        missing = [name for name in features if name not in history.columns]
        if missing:
            raise bad_frame(
                f"the history lacks {', '.join(missing)}, which the state was fitted with",
                missing=missing,
            )
        longest = state["settings"]["lags"][-1]
        require_rows(history, longest, f"the longest lag ({longest})")
        future = None
        if features:
            if drivers is None:
                raise bad_frame(
                    f"the state was fitted with the features {', '.join(features)}; a "
                    "prediction needs their values over the horizon, as drivers",
                    features=features,
                )
            future = drivers[[UNIQUE_ID, DS, *features]]
        wanted = level_values(levels)
        calibrated = bool(wanted) and horizon <= state["interval_horizon"]
        intervals: dict[str, Any] = {}
        if calibrated:
            # The errors calibrated at the fit, scaled to each series of the history;
            # the library's default would calibrate again on the history instead.
            intervals = {"level": wanted, "transfer_conformal": "scale_aligned"}
        found = state["forecast"].predict(
            h=horizon, new_df=history[[UNIQUE_ID, DS, Y, *features]], X_df=future, **intervals
        )
        return _named(found, wanted if calibrated else [])

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
        test = horizon + step * (windows - 1)
        rows = int(frame.groupby(UNIQUE_ID).size().min()) - test
        settings = self.settings(params, rows, horizon)
        longest = settings["lags"][-1]
        require_rows(
            frame,
            longest + horizon + test,
            f"the longest lag ({longest}) plus the horizon ({horizon}), before the first of "
            f"{windows} origins",
        )
        wanted = level_values(levels)
        every = _every(refit)
        intervals: dict[str, Any] = {}
        if wanted:
            calibration = self._calibration(rows, longest, horizon)
            # Between two fits the library calibrates on each origin's history by
            # predicting without the features' future values, which it then lacks.
            if calibration >= 2 and (every is True or not features_of(frame)):
                intervals = {
                    "level": wanted,
                    "prediction_intervals": PredictionIntervals(n_windows=calibration, h=horizon),
                }
            else:
                self.note("no_intervals")
        folds = self.engine(settings).cross_validation(
            frame,
            n_windows=windows,
            h=horizon,
            step_size=step,
            refit=every,
            static_features=[],
            **intervals,
        )
        return _named(folds, wanted if intervals else [])


class RidgeLags(_Lags):
    name = "ridge_lags"
    takes = {**_Lags.takes, "alpha": _number(0.0)}

    def regressor(self, settings: Mapping[str, Any]) -> Any:
        return Ridge(alpha=settings.get("alpha", 1.0))


class GbmLags(_Lags):
    name = "gbm_lags"
    takes = {**_Lags.takes, "max_iter": _whole(1), "learning_rate": _number(0.0, above=True)}

    def regressor(self, settings: Mapping[str, Any]) -> Any:
        # Seeded: the same frame gives the same state.
        return HistGradientBoostingRegressor(
            max_iter=settings.get("max_iter", 100),
            learning_rate=settings.get("learning_rate", 0.1),
            random_state=0,
        )


SHIPPED: dict[str, type[Shipped]] = {
    family.name: family
    for family in (Persistence, SeasonalNaiveFamily, Theta, Ets, Arima, Mstl, RidgeLags, GbmLags)
}
assert tuple(SHIPPED) == ORDER, "the shipped adapters are the shipped families, in their order"
