"""Each shipped family: fitted, stored, loaded in a fresh interpreter and asked to
predict a series whose continuation is known."""

from __future__ import annotations

import io
import json
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from dta_core.families import ORDER
from dta_runtime.models import ops
from dta_runtime.models.shipped import SHIPPED
from dta_runtime.models.state import FORMAT, MEDIA_TYPE, read_header, versions
from dta_runtime.protocol import FitResult, PredictRequest, PredictResult, prediction_columns
from models_testkit import (
    RUN,
    STEP,
    TIME,
    Frames,
    Kept,
    at,
    fit_request,
    frame,
    predict_request,
    ref,
)

LENGTH = 240
HORIZON = 8
PERIOD = [1.0, 5.0, 2.0, 8.0]
LINE = 3.0 + 0.5 * np.arange(LENGTH)
SEASON = np.array(PERIOD * (LENGTH // 4))
CONSTANT = np.full(LENGTH, 7.0)


def _after(values: np.ndarray, kind: str) -> np.ndarray:
    """What comes after the series, for the kinds whose continuation is beyond doubt."""
    ahead = np.arange(LENGTH, LENGTH + HORIZON)
    return {
        "last": np.full(HORIZON, values[-1]),
        "line": 3.0 + 0.5 * ahead,
        "season": np.array([PERIOD[index % 4] for index in ahead]),
        "season_on_line": np.array([PERIOD[index % 4] for index in ahead]) + 0.05 * ahead,
        "constant": np.full(HORIZON, 7.0),
    }[kind]


# family -> (parameters, the series, what must follow it, tolerance)
KNOWN: dict[str, tuple[dict[str, Any], np.ndarray, str, float]] = {
    # The last value seen, whatever came before it.
    "persistence": ({}, LINE, "last", 1e-9),
    # The value one season earlier: a pure season repeats.
    "seasonal_naive": ({"season_length": 4}, SEASON, "season", 1e-9),
    # With the season taken out a pure season is a constant; put back, it repeats.
    "theta": ({"season_length": 4}, SEASON, "season", 1e-3),
    # Exponential smoothing with a trend follows a line.
    "ets": ({"season_length": 1}, LINE, "line", 1e-3),
    # Differenced once a line is a constant: ARIMA with drift continues it.
    "arima": ({"season_length": 1}, LINE, "line", 1e-3),
    # The season separated from the line, the line forecast, the season put back.
    "mstl": ({"season_length": [4]}, SEASON + 0.05 * np.arange(LENGTH), "season_on_line", 0.05),
    # A line is its last value plus the last difference: linear in two lags.
    "ridge_lags": ({"lags": [1, 2], "date_features": [], "alpha": 1e-9}, LINE, "line", 1e-4),
    # The four values before a point of a pure season say which one comes next.
    "gbm_lags": ({"lags": [1, 2, 3, 4], "date_features": []}, SEASON, "season", 0.02),
}

# Runs a prediction as the worker would, in an interpreter that has loaded nothing yet.
FRESH = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
folder = Path(sys.argv[2])
import pyarrow as pa
from dta_runtime.models import ops
from dta_runtime.protocol import PredictRequest
from models_testkit import Frames, Kept

kept = Kept()
for path in folder.glob("*.artifact"):
    kept.files[path.stem] = path.read_bytes()
frames = {
    path.stem: pa.ipc.open_file(pa.memory_map(str(path))).read_all()
    for path in folder.glob("*.arrow")
}
request = PredictRequest.model_validate_json((folder / "request.json").read_text())
table, result = ops.predict(request, Frames(**frames), kept)
with pa.ipc.new_file(str(folder / "prediction.out"), table.schema) as writer:
    writer.write_table(table)
(folder / "result.json").write_text(result.model_dump_json())
"""


def _write(path: Path, table: pa.Table) -> None:
    with pa.ipc.new_file(str(path), table.schema) as writer:
        writer.write_table(table)


def predict_fresh(
    tmp_path: Path, request: PredictRequest, kept: Kept, **frames: pa.Table
) -> tuple[pa.Table, PredictResult]:
    """The prediction of a request, made by a new Python process from the stored bytes."""
    folder = tmp_path / "fresh"
    folder.mkdir()
    for digest, content in kept.files.items():
        (folder / f"{digest}.artifact").write_bytes(content)
    for name, table in frames.items():
        _write(folder / f"{name}.arrow", table)
    (folder / "request.json").write_text(request.model_dump_json())
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", FRESH, str(Path(__file__).parent), str(folder)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    table = pa.ipc.open_file(pa.memory_map(str(folder / "prediction.out"))).read_all()
    return table, PredictResult.model_validate_json((folder / "result.json").read_text())


def test_every_shipped_family_has_an_adapter_and_a_known_answer():
    assert tuple(SHIPPED) == ORDER == tuple(KNOWN)
    assert all(SHIPPED[name].name == name for name in ORDER)
    assert not any(SHIPPED[name](step=STEP).interventional for name in ORDER)


@pytest.mark.parametrize("family", ORDER)
def test_a_family_fits_is_stored_and_predicts_what_the_method_must_give(family, kept, tmp_path):
    params, values, kind, tolerance = KNOWN[family]
    series = {"node:T1": values, "node:T2": values + 100.0}
    table = frame(series)
    twin = Frames(fit=table, history=table)

    fitted = ops.fit(fit_request(family, HORIZON, params=params), twin, kept)
    assert isinstance(fitted, FitResult)
    assert (fitted.series, fitted.rows) == (2, 2 * LENGTH)
    assert (fitted.fitted_from, fitted.fitted_to) == (at(0), at(LENGTH - 1))
    assert fitted.validity == {"y": (float(values.min()), float(values.max()) + 100.0)}
    assert fitted.notes == []
    # The handler leaves these to the worker.
    assert fitted.duration_ms == 0.0 and fitted.logs == []

    content = kept.files[fitted.state]
    assert kept.media[fitted.state] == MEDIA_TYPE
    header, body = read_header(content)
    assert content.index(b"\n") == len(content) - len(body) - 1
    assert header == {
        "format": FORMAT,
        "family": {"name": family},
        "params": params,
        "step": 900.0,
        "horizon": HORIZON,
        "levels": "any",
        "columns": ["unique_id", "ds", "y"],
        "fitted_from": at(0).isoformat(),
        "fitted_to": at(LENGTH - 1).isoformat(),
        "series": 2,
        "validity": {"y": [float(values.min()), float(values.max()) + 100.0]},
        "versions": versions(),
        "body": header["body"],
    }
    assert set(header["versions"]) == {"python", "statsforecast", "mlforecast", "scikit-learn",
                                       "numpy"}  # fmt: skip
    assert header["body"]["bytes"] == len(body)

    request = predict_request(family, fitted.state, HORIZON, levels=[80, 95])
    here, result = ops.predict(request, twin, kept)
    fresh, fresh_result = predict_fresh(tmp_path, request, kept, history=table)
    # The stored bytes give the same prediction in a process that never fitted.
    assert fresh.equals(here) and fresh_result == result
    assert (result.rows, result.series, result.notes) == (2 * HORIZON, 2, [])
    assert result.duration_ms == 0.0

    assert here.schema.names == prediction_columns([80, 95])
    assert here.schema.field("unique_id").type == pa.string()
    assert here.schema.field("ds").type == TIME
    rows = here.to_pylist()
    assert [(row["unique_id"], row["ds"]) for row in rows] == [
        (uid, at(LENGTH + ahead)) for uid in ("node:T1", "node:T2") for ahead in range(HORIZON)
    ]
    expected = _after(values, kind)
    for index, uid in enumerate(("node:T1", "node:T2")):
        found = np.array([row["mean"] for row in rows if row["unique_id"] == uid])
        assert found == pytest.approx(expected + 100.0 * index, abs=tolerance), family
    for row in rows:
        assert row["lo_95"] <= row["lo_80"] <= row["mean"] <= row["hi_80"] <= row["hi_95"]

    # Without levels: the mean alone.
    plain, _ = ops.predict(predict_request(family, fitted.state, 3), twin, kept)
    assert plain.schema.names == ["unique_id", "ds", "mean"] and plain.num_rows == 6
    assert plain.column("mean").to_pylist() == pytest.approx(
        [row["mean"] for row in rows if row["ds"] < at(LENGTH + 3)], abs=1e-9
    )


def _no_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """From here on, anything that fits a model fails the test."""
    import mlforecast
    import statsforecast
    from statsforecast import models

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a prediction fitted a model")

    for cls in (models.Naive, models.SeasonalNaive, models.AutoTheta, models.AutoETS,
                models.AutoARIMA, models.MSTL):  # fmt: skip
        monkeypatch.setattr(cls, "fit", refused)
    monkeypatch.setattr(statsforecast.StatsForecast, "fit", refused)
    monkeypatch.setattr(mlforecast.MLForecast, "fit", refused)
    monkeypatch.setattr(mlforecast.MLForecast, "fit_models", refused)


# What a prediction is marked with when the history has gone beyond what the fit saw of
# the series: a line has, a season has not.
def _beyond(kind: str) -> list[str]:
    return ["outside_validity"] if kind in ("line", "last", "season_on_line") else []


@pytest.mark.parametrize("family", ORDER)
def test_a_state_is_applied_to_the_series_grown_longer_without_fitting_again(
    family, kept, monkeypatch
):
    params, values, kind, tolerance = KNOWN[family]
    fitted = ops.fit(
        fit_request(family, HORIZON, params=params),
        Frames(fit=frame({"node:T1": values[:200]})),
        kept,
    )
    _no_fit(monkeypatch)
    table, result = ops.predict(
        predict_request(family, fitted.state, HORIZON),
        Frames(history=frame({"node:T1": values})),
        kept,
    )
    # After the history's last time, from the history's values.
    assert [row["ds"] for row in table.to_pylist()] == [at(LENGTH + k) for k in range(HORIZON)]
    assert table.column("mean").to_pylist() == pytest.approx(_after(values, kind), abs=tolerance)
    assert result.notes == _beyond(kind)


@pytest.mark.parametrize("family", ORDER)
def test_a_history_that_begins_elsewhere_is_predicted_from_all_the_same(family, kept, monkeypatch):
    """Most fitted models carry nothing of where their series began. Exponential
    smoothing does (the level, the trend and the place in the season of its first row),
    and so does what forecasts a trend with it: those are fitted again on the history."""
    params, values, kind, tolerance = KNOWN[family]
    anchored = family in ("ets", "mstl")
    assert SHIPPED[family].anchored is anchored if family in ORDER[:6] else True
    fitted = ops.fit(
        fit_request(family, HORIZON, params=params),
        Frames(fit=frame({"node:T1": values[:200]})),
        kept,
    )
    if not anchored:
        _no_fit(monkeypatch)
    # 42 rows later: not a whole number of seasons.
    later = frame({"node:T1": values[42:]}, first=42)
    table, result = ops.predict(
        predict_request(family, fitted.state, HORIZON), Frames(history=later), kept
    )
    assert [row["ds"] for row in table.to_pylist()] == [at(LENGTH + k) for k in range(HORIZON)]
    assert table.column("mean").to_pylist() == pytest.approx(_after(values, kind), abs=tolerance)
    assert result.notes == _beyond(kind) + (["refitted:1"] if anchored else [])


def test_the_lag_families_take_features_and_their_future_values_as_drivers(kept, tmp_path):
    rng = np.random.default_rng(7)
    length, horizon = 300, 4
    inflow = {uid: rng.uniform(0.0, 1.0, length + horizon) for uid in ("node:T1", "node:T2")}
    # The level is ten and twice the inflow of the same time: one rule for every tank.
    level = {uid: 10.0 + 2.0 * values for uid, values in inflow.items()}
    known = {uid: values[:length] for uid, values in level.items()}
    table = frame(known, features={"Tank_inflow": {u: v[:length] for u, v in inflow.items()}})
    # The drivers come as a frame over the horizon: no target there, the feature known.
    whole = frame(
        {uid: [*values[:length], *[None] * horizon] for uid, values in level.items()},
        features={"Tank_inflow": inflow},
    )
    twin = Frames(fit=table, history=table, drivers=whole)
    params = {"lags": [1], "date_features": [], "alpha": 1e-9}

    fitted = ops.fit(fit_request("ridge_lags", horizon, params=params), twin, kept)
    assert fitted.notes == []
    assert set(fitted.validity) == {"y", "Tank_inflow"}
    low, high = fitted.validity["Tank_inflow"]
    assert 0.0 <= low < 0.05 and 0.95 < high <= 1.0
    header, _ = read_header(kept.files[fitted.state])
    assert header["columns"] == ["unique_id", "ds", "y", "Tank_inflow"]

    request = predict_request("ridge_lags", fitted.state, horizon, drivers=True, levels=[90])
    found, result = ops.predict(request, twin, kept)
    assert result.notes == []
    expected = [level[uid][length + ahead] for uid in sorted(level) for ahead in range(horizon)]
    assert found.column("mean").to_pylist() == pytest.approx(expected, abs=1e-3)
    assert found.schema.names == prediction_columns([90])
    fresh, _ = predict_fresh(tmp_path, request, kept, history=table, drivers=whole)
    assert fresh.equals(found)

    # A driver beyond what the fit saw is used, and the prediction marked.
    far = {
        uid: np.concatenate([values[:length], np.full(horizon, 3.0)])
        for uid, values in inflow.items()
    }
    beyond = frame(
        {uid: [*values[:length], *[None] * horizon] for uid, values in level.items()},
        features={"Tank_inflow": far},
    )
    found, result = ops.predict(request, Frames(history=table, drivers=beyond), kept)
    assert result.notes == ["outside_validity"]
    assert found.column("mean").to_pylist()[:horizon] == pytest.approx([16.0] * horizon, abs=1e-2)

    # The statsforecast families read the target alone, and say so.
    fitted = ops.fit(fit_request("persistence", horizon), twin, kept)
    assert fitted.notes == ["features_unused"] and set(fitted.validity) == {"y"}
    found, result = ops.predict(predict_request("persistence", fitted.state, horizon), twin, kept)
    assert result.notes == ["features_unused"] and found.num_rows == 2 * horizon


def test_a_lag_family_without_room_to_calibrate_has_intervals_from_the_backtest_or_none(kept):
    values = 5.0 + np.sin(np.arange(30) / 3.0)
    table = frame({"node:T1": values})
    twin = Frames(fit=table, history=table)
    params = {"lags": [1, 2], "date_features": []}
    # 30 rows: 2 for the lags, a horizon of 12 to fit on, and one window of 12 at most.
    fitted = ops.fit(fit_request("ridge_lags", 12, params=params), twin, kept)
    assert fitted.notes == ["no_intervals"]
    assert read_header(kept.files[fitted.state])[0]["levels"] == []

    found, result = ops.predict(
        predict_request("ridge_lags", fitted.state, 12, levels=[80]), twin, kept
    )
    assert result.notes == ["no_intervals"]
    assert found.schema.names == ["unique_id", "ds", "mean"]

    # The quantiles of a backtest's absolute error stand in, per step ahead.
    from dta_runtime.models.scores import residuals_file

    steps = list(range(1, 13))
    stored = kept.put(
        residuals_file(
            pa.table(
                {
                    "step": pa.array(steps * 2, pa.int64()),
                    "quantile": pa.array([0.8] * 12 + [0.95] * 12, pa.float64()),
                    "abs_error": pa.array(
                        [0.1 * step for step in steps] + [0.2 * step for step in steps],
                        pa.float64(),
                    ),
                }
            )
        ),
        "application/vnd.apache.arrow.file",
    )
    request = predict_request("ridge_lags", fitted.state, 12, levels=[80, 95], residuals=stored)
    found, result = ops.predict(request, twin, kept)
    assert result.notes == []
    assert found.schema.names == prediction_columns([80, 95])
    for ahead, row in enumerate(found.to_pylist(), start=1):
        assert row["hi_80"] - row["mean"] == pytest.approx(0.1 * ahead)
        assert row["mean"] - row["lo_80"] == pytest.approx(0.1 * ahead)
        assert row["hi_95"] - row["mean"] == pytest.approx(0.2 * ahead)

    # A level or a step the residuals do not hold is refused, saying what they hold.
    for more in ({"levels": [90]}, {"levels": [80], "horizon": 13}):
        body = {"levels": [80], "horizon": 12, **more}
        with pytest.raises(Exception) as caught:
            ops.predict(
                predict_request(
                    "ridge_lags", fitted.state, body["horizon"], levels=body["levels"],
                    residuals=stored,
                ),
                twin,
                kept,
            )  # fmt: skip
        assert caught.value.code == "model.bad_params"
        assert "80" in caught.value.message and "95" in caught.value.message


def test_a_stored_state_is_a_header_line_and_a_pickle(kept):
    table = frame({"node:T1": LINE})
    fitted = ops.fit(fit_request("persistence"), Frames(fit=table), kept)
    content = kept.files[fitted.state]
    line, _, body = content.partition(b"\n")
    header = json.loads(line)
    assert header["format"] == 1 and header["family"] == {"name": "persistence"}
    state = pickle.load(io.BytesIO(body))
    assert set(state) == {"settings", "models", "starts"}
    assert list(state["models"]) == ["node:T1"] and state["starts"]["node:T1"] == at(0)


def test_series_that_cannot_be_learned_from_are_left_out_and_counted(kept):
    values = list(SEASON[:60])
    table = frame(
        {
            "node:A": values,
            # Nothing stored before its sixth row: those rows are dropped, the rest kept.
            "node:B": [None] * 5 + values[5:],
            # A missing value after its first.
            "node:C": values[:30] + [None] + values[31:],
            # Stops before the frame's last time.
            "node:D": values[:50],
            # Its last values are missing: it has ended, as far as a model can know.
            "node:E": values[:57] + [None] * 3,
            # Never had a value.
            "node:F": [None] * 60,
        }
    )
    # Removed for a while: the times are missing, not the values.
    holed = table.filter(
        pa.compute.invert(
            pa.compute.and_(
                pa.compute.equal(table.column("unique_id"), "node:A"),
                pa.compute.equal(table.column("ds"), pa.scalar(at(20), TIME)),
            )
        )
    )
    twin = Frames(fit=table, history=table)
    fitted = ops.fit(fit_request("seasonal_naive", params={"season_length": 4}), twin, kept)
    assert fitted.notes == ["series_with_gaps:1", "series_ended:3"]
    assert (fitted.series, fitted.rows) == (2, 60 + 55)
    assert (fitted.fitted_from, fitted.fitted_to) == (at(0), at(59))
    found, result = ops.predict(predict_request("seasonal_naive", fitted.state, 4), twin, kept)
    assert result.notes == ["series_with_gaps:1", "series_ended:3"]
    assert sorted(set(found.column("unique_id").to_pylist())) == ["node:A", "node:B"]

    fitted = ops.fit(
        fit_request("seasonal_naive", params={"season_length": 4}), Frames(fit=holed), kept
    )
    assert fitted.notes == ["series_with_gaps:2", "series_ended:3"] and fitted.series == 1


def test_a_series_the_state_never_saw_is_predicted_by_a_lag_family_only(kept):
    table = frame({"node:T1": LINE})
    both = frame({"node:T1": LINE, "node:T9": LINE + 50.0})
    params = KNOWN["ridge_lags"][0]
    fitted = ops.fit(fit_request("ridge_lags", params=params), Frames(fit=table), kept)
    found, result = ops.predict(
        predict_request("ridge_lags", fitted.state), Frames(history=both), kept
    )
    # One model for every series: it continues the new line as well.
    assert sorted(set(found.column("unique_id").to_pylist())) == ["node:T1", "node:T9"]
    assert found.column("mean").to_pylist()[HORIZON:] == pytest.approx(
        _after(LINE, "line") + 50.0, abs=1e-3
    )
    # Only rows after the last time the fit saw, and drivers, are held against its range.
    assert result.notes == []

    fitted = ops.fit(fit_request("ets", params={"season_length": 1}), Frames(fit=table), kept)
    found, result = ops.predict(predict_request("ets", fitted.state), Frames(history=both), kept)
    # A model per series: the series without one is left out and counted.
    assert set(found.column("unique_id").to_pylist()) == {"node:T1"}
    assert "series_unfitted:1" in result.notes


def test_the_defaults_follow_the_grid(kept):
    quarter = SHIPPED["ets"](step=STEP)
    assert quarter.settings({}) == {"season_length": 96}
    assert SHIPPED["ets"](step=STEP * 4).settings({})["season_length"] == 24
    # A step longer than a day has no daily season.
    from datetime import timedelta

    assert SHIPPED["theta"](step=timedelta(days=2)).settings({})["season_length"] == 1
    assert SHIPPED["mstl"](step=STEP).settings({}) == {"season_length": [96]}
    lags = SHIPPED["ridge_lags"](step=STEP)
    # A day back when a series holds two days and the horizon, a week back likewise.
    assert lags.settings({}, 100, 8)["lags"] == [1, 2, 3, 4]
    assert lags.settings({}, 2 * 96 + 8, 8)["lags"] == [1, 2, 3, 4, 96]
    assert lags.settings({}, 2 * 672 + 8, 8)["lags"] == [1, 2, 3, 4, 96, 672]
    assert lags.settings({}, 100, 8)["date_features"] == ["hour", "dayofweek"]
    assert lags.settings({"lags": [3, 1]}, 10, 1)["lags"] == [1, 3]
    assert ref("fit")["step"] == STEP and RUN["budget_s"] == 240.0
