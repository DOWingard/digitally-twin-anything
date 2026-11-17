"""A backtest walks forward through the frame: the origins, the scores, the baseline on
the same folds, how often it fits again, and the residuals it leaves."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from dta_core.errors import DtaError
from dta_runtime.models import ops
from dta_runtime.models.series import from_arrow, prepare
from dta_runtime.models.shipped import SHIPPED
from dta_runtime.protocol import BacktestResult
from models_testkit import STEP, Frames, Kept, at, backtest_request, frame

LENGTH = 120
HORIZON, BETWEEN, WINDOWS = 8, 4, 5
# The last origin is a horizon before the end, the others four steps apart before it.
ORIGINS = [LENGTH - 1 - HORIZON - BETWEEN * back for back in range(WINDOWS - 1, -1, -1)]
SHA = "ab" * 32

_rng = np.random.default_rng(11)
# Whole numbers, never zero, so that every measure is defined.
SERIES = {
    "node:T1": np.round(20 + 5 * np.sin(np.arange(LENGTH) / 5.0) + _rng.normal(0, 1.5, LENGTH)),
    "node:T2": np.round(40 + 0.1 * np.arange(LENGTH) + _rng.normal(0, 2.5, LENGTH)),
}
TABLE = frame(SERIES)
PLAIN = """
from dta_runtime.models.shipped import Persistence, RidgeLags


class Plain:
    '''A shipped family without its own walk-forward evaluation.'''

    interventional = False

    def __init__(self, inner):
        self._inner = inner
        self.fits = 0

    def fit(self, frame, params, *, horizon):
        self.fits += 1
        FITS.append(len(frame))
        return self._inner.fit(frame, params, horizon=horizon)

    def predict(self, state, history, horizon, **more):
        return self._inner.predict(state, history, horizon, **more)

    def validity(self, frame):
        return self._inner.validity(frame)


FITS = []


def persistence(*, step, twin):
    return Plain(Persistence(step=step))


def ridge(*, step, twin):
    return Plain(RidgeLags(step=step))
"""


def twin() -> Frames:
    return Frames(fit=TABLE)


def persistence_errors(series: dict[str, np.ndarray] = SERIES) -> dict[str, np.ndarray]:
    """Per series, what persistence is off by: a row per origin, a column per step ahead."""
    return {
        uid: np.array(
            [values[origin + 1 : origin + 1 + HORIZON] - values[origin] for origin in ORIGINS]
        )
        for uid, values in series.items()
    }


def own(entry: str) -> dict[str, str]:
    return {"artifact": SHA, "entry": entry}


def folds_of(family: Any, **more: Any) -> pd.DataFrame:
    history = prepare(from_arrow(TABLE), [], STEP).frame
    arguments = {"horizon": HORIZON, "step": BETWEEN, "windows": WINDOWS, "refit": True, **more}
    return family.cross_validate(history, arguments.pop("params", {}), **arguments)


@pytest.mark.parametrize(
    ("family", "params"),
    [
        ("persistence", {}),
        ("seasonal_naive", {"season_length": 4}),
        ("ridge_lags", {"lags": [1, 2], "date_features": []}),
    ],
)
def test_the_origins_are_the_last_a_horizon_before_the_end_and_a_step_apart(family, params):
    folds = folds_of(SHIPPED[family](step=STEP), params=params)
    assert list(folds.columns) == ["unique_id", "ds", "cutoff", "y", "mean"]
    for uid, values in SERIES.items():
        found = folds[folds["unique_id"] == uid].sort_values(["cutoff", "ds"])
        assert [moment.to_pydatetime() for moment in found["cutoff"].unique()] == [
            at(origin) for origin in ORIGINS
        ]
        assert [moment.to_pydatetime() for moment in found["ds"]] == [
            at(origin + ahead) for origin in ORIGINS for ahead in range(1, HORIZON + 1)
        ]
        assert found["y"].to_list() == [
            values[origin + ahead] for origin in ORIGINS for ahead in range(1, HORIZON + 1)
        ]
    # Other settings, other origins: one a horizon before the end, the rest behind it.
    wide = folds_of(SHIPPED[family](step=STEP), params=params, horizon=3, step=10, windows=2)
    assert [moment.to_pydatetime() for moment in sorted(wide["cutoff"].unique())] == [
        at(LENGTH - 1 - 3 - 10),
        at(LENGTH - 1 - 3),
    ]


def test_the_scores_are_the_errors_per_series_over_every_fold_then_across_series(kept):
    metrics = ["mae", "rmse", "mape", "max_abs", "error_rate"]
    result = ops.backtest(
        backtest_request("persistence", metrics=metrics, refit=True), twin(), kept
    )
    assert isinstance(result, BacktestResult)
    errors = persistence_errors()
    truth = {
        uid: np.array([values[origin + 1 : origin + 1 + HORIZON] for origin in ORIGINS])
        for uid, values in SERIES.items()
    }
    expected = {
        "mae": np.mean([np.abs(found).mean() for found in errors.values()]),
        "rmse": np.mean([np.sqrt((found**2).mean()) for found in errors.values()]),
        "mape": np.mean([(np.abs(errors[uid]) / np.abs(truth[uid])).mean() for uid in errors]),
        "max_abs": max(np.abs(found).max() for found in errors.values()),
        # Persistence of whole numbers predicts whole numbers: wrong wherever it is off.
        "error_rate": np.mean([(found != 0).mean() for found in errors.values()]),
    }
    assert list(result.scores) == metrics
    assert result.scores == pytest.approx(expected, rel=1e-12)
    assert 0 < result.scores["error_rate"] < 1 and result.scores["max_abs"] >= 5
    assert (result.folds, result.series, result.refit) == (WINDOWS, 2, True)
    assert result.baseline_scores is None and result.notes == []
    assert result.first_fit_ms > 0
    assert result.duration_ms == 0.0 and result.logs == []
    # Only what was asked for, in the order asked.
    some = ops.backtest(backtest_request("persistence", metrics=["rmse", "mae"]), twin(), kept)
    assert list(some.scores) == ["rmse", "mae"]
    assert some.scores == pytest.approx({name: expected[name] for name in ("rmse", "mae")})


def test_the_residuals_are_the_quantiles_of_the_absolute_error_per_step_ahead(kept):
    result = ops.backtest(backtest_request("persistence"), twin(), kept)
    assert kept.media[result.residuals] == "application/vnd.apache.arrow.file"
    table = pa.ipc.open_file(pa.BufferReader(kept.files[result.residuals])).read_all()
    assert table.schema.names == ["step", "quantile", "abs_error"]
    errors = np.concatenate(list(persistence_errors().values()))
    rows = table.to_pylist()
    assert [(row["step"], row["quantile"]) for row in rows] == [
        (ahead, quantile)
        for ahead in range(1, HORIZON + 1)
        for quantile in (0.5, 0.8, 0.9, 0.95, 0.99)
    ]
    for row in rows:
        # Ten errors per step: five origins of two series.
        expected = np.quantile(np.abs(errors[:, row["step"] - 1]), row["quantile"])
        assert row["abs_error"] == pytest.approx(expected, rel=1e-12)


def test_the_baseline_is_scored_on_the_same_folds(kept):
    params = {"lags": [1, 2, 3], "date_features": []}
    alone = ops.backtest(backtest_request("persistence", metrics=["mae", "rmse"]), twin(), kept)
    result = ops.backtest(
        backtest_request("ridge_lags", params=params, metrics=["mae", "rmse"],
                         baseline="persistence"),
        twin(),
        kept,
    )  # fmt: skip
    assert result.baseline_scores == pytest.approx(alone.scores, rel=1e-12)
    assert list(result.baseline_scores) == ["mae", "rmse"]
    assert result.scores != result.baseline_scores
    # A family measured against itself has its own error for a baseline.
    same = ops.backtest(backtest_request("persistence", baseline="persistence"), twin(), kept)
    assert same.scores == same.baseline_scores
    # The naive season is the candidate's, when the candidate names one.
    seasonal = ops.backtest(
        backtest_request("theta", params={"season_length": 4}, baseline="seasonal_naive"),
        twin(),
        kept,
    )
    naive = ops.backtest(
        backtest_request("seasonal_naive", params={"season_length": 4}), twin(), kept
    )
    assert seasonal.baseline_scores == pytest.approx(naive.scores, rel=1e-12)
    for baseline in ("theta", "ridge_lags", "nothing"):
        with pytest.raises(DtaError) as caught:
            ops.backtest(backtest_request("persistence", baseline=baseline), twin(), kept)
        assert caught.value.code == "model.bad_params"
        assert "persistence" in caught.value.message and "seasonal_naive" in caught.value.message


def test_a_difference_between_the_folds_of_the_baseline_and_the_candidate_is_an_error(
    kept, monkeypatch
):
    from dta_runtime.models.shipped import Persistence

    original = Persistence.cross_validate
    calls = []

    def shifted(self, frame, params, **more):
        calls.append(more["windows"])
        # The second run (the baseline's) walks other origins.
        if len(calls) == 2:
            more = {**more, "step": more["step"] + 1}
        return original(self, frame, params, **more)

    monkeypatch.setattr(Persistence, "cross_validate", shifted)
    with pytest.raises(DtaError) as caught:
        ops.backtest(backtest_request("persistence", baseline="persistence"), twin(), kept)
    assert caught.value.code == "model.failed" and "same folds" in caught.value.message
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("took_s", "budget_s", "expected"),
    [(1.0, 240.0, 1), (30.0, 240.0, 5), (40.0, 240.0, 7), (120.0, 240.0, 20), (121.0, 240.0, False),
     (1.0, 30.0, 2)],
)  # fmt: skip
def test_how_often_to_fit_again_is_chosen_by_how_long_the_first_fit_took(
    kept, monkeypatch, took_s, budget_s, expected
):
    import statsforecast

    # The first fit takes as long as the test says.
    clock = iter([100.0, 100.0 + took_s])
    monkeypatch.setattr(ops, "_clock", lambda: next(clock))
    given: list[Any] = []
    original = statsforecast.StatsForecast.cross_validation

    def recorded(self, **arguments):
        given.append(arguments["refit"])
        return original(self, **arguments)

    monkeypatch.setattr(statsforecast.StatsForecast, "cross_validation", recorded)
    request = backtest_request("persistence", windows=20, step=2, horizon=4, budget_s=budget_s)
    assert request.refit is None
    result = ops.backtest(request, twin(), kept)
    assert result.refit == expected and type(result.refit) is type(expected)
    assert result.first_fit_ms == pytest.approx(took_s * 1000.0)
    assert result.folds == 20
    # What the library was told: every origin is `True` to it, however it was chosen.
    assert given == [True if expected == 1 else expected]


def test_a_refit_that_is_asked_for_is_the_one_used(kept, own_code):
    own_code("plain_families", PLAIN)
    import plain_families

    for refit, fits in ((True, 5), (False, 1), (2, 3), (3, 2), (5, 1)):
        plain_families.FITS.clear()
        result = ops.backtest(
            backtest_request(own("plain_families:persistence"), refit=refit), twin(), kept
        )
        assert result.refit == refit and type(result.refit) is type(refit)
        # One fit to time, on what the first origin knows, then one per origin fitted at.
        first = 2 * (LENGTH - HORIZON - BETWEEN * (WINDOWS - 1))
        assert plain_families.FITS[:2] == [first, first]
        assert len(plain_families.FITS) == 1 + fits
        assert plain_families.FITS[1:] == sorted(plain_families.FITS[1:])


@pytest.mark.parametrize("refit", [True, False, 2])
def test_fold_by_fold_gives_what_the_librarys_own_evaluation_gives(kept, own_code, refit):
    """The one loop this package writes, against the library's on the same family."""
    own_code("plain_families", PLAIN)
    from dta_runtime.models.families import make
    from dta_runtime.protocol import FamilyRef

    shipped = SHIPPED["persistence"](step=STEP)
    plain = make(FamilyRef(**own("plain_families:persistence")), step=STEP, twin=twin())
    assert not hasattr(plain, "cross_validate")
    history = prepare(from_arrow(TABLE), [], STEP).frame
    by_library = folds_of(shipped, refit=refit, levels=[80])
    by_fold = ops._fold_by_fold(
        plain, "plain", history, {}, horizon=HORIZON, step=BETWEEN, windows=WINDOWS,
        refit=refit, levels=[80], inputs=[], grid=STEP,
    )  # fmt: skip
    keys = ["unique_id", "cutoff", "ds"]
    by_library = by_library.sort_values(keys).reset_index(drop=True)
    by_fold = by_fold.sort_values(keys).reset_index(drop=True)
    assert list(by_fold.columns) == list(by_library.columns)
    assert sorted(by_fold["cutoff"].unique()) == sorted(by_library["cutoff"].unique())
    pd.testing.assert_frame_equal(by_fold, by_library, check_exact=False, rtol=1e-12)

    # And through the handler: the same scores, the same residuals to the byte.
    metrics = ["mae", "rmse", "max_abs"]
    library = ops.backtest(
        backtest_request("persistence", metrics=metrics, refit=refit, baseline="persistence"),
        twin(),
        kept,
    )
    folded = ops.backtest(
        backtest_request(own("plain_families:persistence"), metrics=metrics, refit=refit,
                         baseline="persistence"),
        twin(),
        kept,
    )  # fmt: skip
    assert folded.scores == pytest.approx(library.scores, rel=1e-12)
    assert folded.baseline_scores == library.baseline_scores
    assert (folded.folds, folded.series) == (library.folds, library.series) == (WINDOWS, 2)
    assert folded.residuals == library.residuals


def test_fold_by_fold_hands_a_lag_family_the_features_of_each_fold_as_drivers(kept, own_code):
    own_code("plain_families", PLAIN)
    rng = np.random.default_rng(5)
    inflow = {uid: rng.uniform(0, 1, LENGTH) for uid in SERIES}
    level = {uid: 10.0 + 2.0 * values for uid, values in inflow.items()}
    table = frame(level, features={"Tank_inflow": inflow})
    params = {"lags": [1], "date_features": [], "alpha": 1e-9}
    by_library = ops.backtest(
        backtest_request("ridge_lags", params=params, metrics=["mae", "max_abs"]),
        Frames(fit=table),
        kept,
    )
    by_fold = ops.backtest(
        backtest_request(own("plain_families:ridge"), params=params, metrics=["mae", "max_abs"]),
        Frames(fit=table),
        kept,
    )
    # The level is a rule of the inflow of the same time: known ahead, it leaves no error.
    assert by_library.scores["max_abs"] < 1e-3
    assert by_fold.scores == pytest.approx(by_library.scores, abs=1e-6)


def test_intervals_in_a_backtest_come_where_the_library_can_give_them(kept):
    season = {"season_length": 4}
    with_levels = ops.backtest(
        backtest_request("theta", levels=[80, 95], params=season), twin(), kept
    )
    assert with_levels.notes == []
    params = {"lags": [1, 2], "date_features": []}
    lags = ops.backtest(backtest_request("ridge_lags", params=params, levels=[80]), twin(), kept)
    assert lags.notes == []
    # With features, and not fitted at every origin, the library cannot calibrate.
    rng = np.random.default_rng(5)
    table = frame(SERIES, features={"Tank_inflow": {u: rng.uniform(0, 1, LENGTH) for u in SERIES}})
    for refit, notes in ((True, []), (False, ["no_intervals"]), (2, ["no_intervals"])):
        found = ops.backtest(
            backtest_request("ridge_lags", params=params, levels=[80], refit=refit),
            Frames(fit=table),
            kept,
        )
        assert found.notes == notes
    # The scores are of the mean: the same with levels and without.
    plain = ops.backtest(backtest_request("ridge_lags", params=params), twin(), kept)
    assert lags.scores == pytest.approx(plain.scores, rel=1e-12)


def test_series_left_out_of_a_backtest_are_counted(kept):
    values = {uid: list(found) for uid, found in SERIES.items()}
    table = frame(
        {
            **values,
            "node:T3": values["node:T1"][:60] + [None] + values["node:T1"][61:],
            "node:T4": values["node:T2"][:100],
        }
    )
    result = ops.backtest(backtest_request("persistence"), Frames(fit=table), kept)
    assert result.notes == ["series_with_gaps:1", "series_ended:1"]
    assert result.series == 2
    alone = ops.backtest(backtest_request("persistence"), twin(), Kept())
    assert result.scores == alone.scores
