"""What a fit, a backtest and a prediction are refused for, each with its code."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import numpy as np
import pytest
from dta_core.errors import DtaError
from dta_runtime.models import ops
from dta_runtime.models.state import read_header
from models_testkit import (
    STEP,
    Frames,
    Kept,
    at,
    backtest_request,
    fit_request,
    frame,
    predict_request,
)

LENGTH = 60
VALUES = 5.0 + np.sin(np.arange(LENGTH) / 3.0)
TABLE = frame({"node:T1": VALUES, "node:T2": VALUES + 1.0})
SMALL = {"season_length": 4}


def twin(**frames: Any) -> Frames:
    return Frames(**{"fit": TABLE, "history": TABLE, **frames})


def refused(call: Any, *args: Any) -> DtaError:
    with pytest.raises(DtaError) as caught:
        call(*args)
    return caught.value


@pytest.fixture(scope="module")
def stored() -> tuple[Kept, str]:
    kept = Kept()
    return kept, ops.fit(fit_request("persistence"), twin(), kept).state


def test_an_unknown_family_is_refused_by_every_handler(kept):
    for request in (
        fit_request("prophet"),
        backtest_request("prophet"),
        predict_request("prophet", "00" * 32),
    ):
        handler = getattr(ops, type(request).__name__.removesuffix("Request").lower())
        error = refused(handler, request, twin(), kept)
        assert error.code == "model.unknown_family"
        assert error.detail == {"family": "prophet"}
        # What can be asked for instead is said.
        assert "seasonal_naive" in error.message and "gbm_lags" in error.message


@pytest.mark.parametrize(
    ("family", "params", "param"),
    [
        ("persistence", {"season_length": 4}, "season_length"),
        ("seasonal_naive", {"season": 4}, "season"),
        ("seasonal_naive", {"season_length": 0}, "season_length"),
        ("seasonal_naive", {"season_length": 2.5}, "season_length"),
        ("seasonal_naive", {"season_length": True}, "season_length"),
        ("theta", {"season_length": "day"}, "season_length"),
        ("ets", {"model": "ABC"}, "model"),
        ("ets", {"model": 3}, "model"),
        ("arima", {"max_p": -1}, "max_p"),
        ("mstl", {"season_length": 4}, "season_length"),
        ("mstl", {"season_length": [1]}, "season_length"),
        ("mstl", {"season_length": []}, "season_length"),
        ("ridge_lags", {"lags": [0, 1]}, "lags"),
        ("ridge_lags", {"lags": []}, "lags"),
        ("ridge_lags", {"lags": "1,2"}, "lags"),
        ("ridge_lags", {"alpha": -1.0}, "alpha"),
        ("ridge_lags", {"date_features": ["moon"]}, "date_features"),
        ("gbm_lags", {"max_iter": 0}, "max_iter"),
        ("gbm_lags", {"learning_rate": 0}, "learning_rate"),
        ("gbm_lags", {"alpha": 1.0}, "alpha"),
    ],
)
def test_parameters_a_family_does_not_take_are_refused(kept, family, params, param):
    for handler, request in (
        (ops.fit, fit_request(family, params=params)),
        (ops.backtest, backtest_request(family, params=params)),
    ):
        error = refused(handler, request, twin(), kept)
        assert error.code == "model.bad_params"
        assert error.detail == {"param": param, "family": family}
        assert param in error.message
    assert kept.files == {}


def test_levels_measures_and_baselines_that_cannot_be_are_refused(kept, stored):
    states, state = stored
    for levels in ([0], [100], [-5], [80, 120]):
        for handler, request, artifacts in (
            (ops.backtest, backtest_request("persistence", levels=levels), kept),
            (ops.predict, predict_request("persistence", state, levels=levels), states),
        ):
            error = refused(handler, request, twin(), artifacts)
            assert (error.code, error.detail) == ("model.bad_params", {"param": "levels"})
    error = refused(ops.backtest, backtest_request("persistence", metrics=["mae", "smape"]),
                    twin(), kept)  # fmt: skip
    assert (error.code, error.detail) == ("model.bad_params", {"param": "metrics"})
    assert "smape" in error.message and "max_abs" in error.message
    # A rate of wrong whole numbers needs a target of whole numbers.
    error = refused(ops.backtest, backtest_request("persistence", metrics=["error_rate"]),
                    twin(), kept)  # fmt: skip
    assert (error.code, error.detail) == ("model.bad_params", {"param": "metrics"})
    # A relative error is not defined where the truth is zero.
    zero = frame({"node:T1": np.arange(LENGTH) % 3.0})
    error = refused(ops.backtest, backtest_request("persistence", metrics=["mape"]),
                    Frames(fit=zero), kept)  # fmt: skip
    assert error.code == "model.bad_params" and "mape" in error.message
    error = refused(ops.backtest, backtest_request("ets", baseline="theta"), twin(), kept)
    assert (error.code, error.detail) == ("model.bad_params", {"param": "baseline"})


def test_series_too_short_to_learn_from_are_refused_by_name(kept):
    short = frame({"node:T1": VALUES, "node:T2": VALUES[:7], "node:T3": VALUES[:5]},
                  starts={"node:T2": LENGTH - 7, "node:T3": LENGTH - 5})  # fmt: skip
    for family, params, need in (
        ("seasonal_naive", SMALL, 8),
        ("theta", SMALL, 10),
        ("ets", {"season_length": 1}, 10),
        ("arima", {"season_length": 1}, 10),
        ("mstl", {"season_length": [4]}, 10),
        ("ridge_lags", {"lags": [1, 2]}, 2 + 8),
        ("gbm_lags", {"lags": [1, 2]}, 2 + 8),
    ):
        error = refused(ops.fit, fit_request(family, params=params), Frames(fit=short), kept)
        assert error.code == "model.bad_frame", family
        assert error.detail["need"] == need, family
        assert error.detail["series"] == ["node:T2", "node:T3"]
        assert "node:T2" in error.message and str(need) in error.message
    # The default season is a day of steps: two of them are needed, and that is said.
    error = refused(ops.fit, fit_request("seasonal_naive"), twin(), kept)
    assert error.code == "model.bad_frame" and error.detail["need"] == 192
    assert "season_length" in error.message
    # A backtest needs every origin and a row before the first.
    error = refused(ops.backtest, backtest_request("persistence", horizon=8, step=4, windows=14),
                    twin(), kept)  # fmt: skip
    assert error.code == "model.bad_frame" and error.detail["need"] == 8 + 4 * 13 + 1
    ops.backtest(backtest_request("persistence", horizon=8, step=4, windows=13), twin(), kept)
    # A prediction needs of the history what the fit needed of the frame.
    fitted = ops.fit(fit_request("seasonal_naive", params=SMALL), twin(), kept)
    error = refused(ops.predict, predict_request("seasonal_naive", fitted.state),
                    Frames(history=frame({"node:T1": VALUES[:6]})), kept)  # fmt: skip
    assert error.code == "model.bad_frame" and error.detail["need"] == 8


def test_a_frame_that_is_not_one_is_refused(kept, stored):
    states, state = stored
    error = refused(ops.fit, fit_request("persistence"),
                    Frames(fit=TABLE.drop_columns(["y"])), kept)  # fmt: skip
    assert (error.code, error.detail) == ("model.bad_frame", {"missing": ["y"]})
    empty = frame({"node:T1": [None] * 10})
    for handler, request, artifacts in (
        (ops.fit, fit_request("persistence"), kept),
        (ops.backtest, backtest_request("persistence"), kept),
        (ops.predict, predict_request("persistence", state), states),
    ):
        error = refused(handler, request, Frames(fit=empty, history=empty), artifacts)
        assert error.code == "model.bad_frame"
    # Every series has a gap: none is left to learn from.
    holed = frame({"node:T1": [*VALUES[:20], None, *VALUES[21:]]})
    error = refused(ops.fit, fit_request("persistence"), Frames(fit=holed), kept)
    assert error.code == "model.bad_frame"


def test_a_state_is_refused_before_it_is_unpickled(kept, stored, monkeypatch):
    states, state = stored
    content = states.files[state]
    header, body = read_header(content)

    def unpickled(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a refused state was unpickled")

    def put(document: dict[str, Any], rest: bytes = body) -> str:
        return kept.put(json.dumps(document).encode() + b"\n" + rest, "application/octet-stream")

    cases = {
        # Fitted by another family than the request names.
        "family": (state, "seasonal_naive"),
        # Another format of state.
        "format": (put({**header, "format": 2}), "persistence"),
        # Made by a major version of a library this process does not have.
        "library": (
            put({**header, "versions": {**header["versions"], "statsforecast": "9.0.0"}}),
            "persistence",
        ),
        "python": (
            put({**header, "versions": {**header["versions"], "python": "4.1.0"}}),
            "persistence",
        ),
        # Cut short, and changed.
        "truncated": (kept.put(content[:-10], "x"), "persistence"),
        "half": (kept.put(content[: len(content) // 2], "x"), "persistence"),
        "altered": (kept.put(content[:-1] + bytes([content[-1] ^ 1]), "x"), "persistence"),
        # Not a state at all.
        "no header": (kept.put(b"\x80\x04 a pickle and nothing else", "x"), "persistence"),
        "not json": (kept.put(b"fitted state\n" + body, "x"), "persistence"),
        "not a document": (kept.put(b"[1, 2]\n" + body, "x"), "persistence"),
        "members": (put({"format": 1, "family": {"name": "persistence"}}), "persistence"),
        "empty": (kept.put(b"", "x"), "persistence"),
    }
    kept.files[state] = content
    monkeypatch.setattr("pickle.load", unpickled)
    monkeypatch.setattr("pickle.loads", unpickled)
    for name, (sha, family) in cases.items():
        error = refused(ops.predict, predict_request(family, sha), twin(), kept)
        assert error.code == "model.bad_state", name
    # The same header with an intact body is the state it was.
    monkeypatch.undo()
    table, _ = ops.predict(predict_request("persistence", put(header)), twin(), kept)
    assert table.num_rows == 16


def test_a_state_that_does_not_unpickle_is_refused_without_what_it_said(kept, stored):
    import hashlib

    states, state = stored
    header, _ = read_header(states.files[state])
    body = b"not a pickle: swordfish"
    header = {**header, "body": {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}}
    sha = kept.put(json.dumps(header).encode() + b"\n" + body, "x")
    error = refused(ops.predict, predict_request("persistence", sha), twin(), kept)
    assert error.code == "model.bad_state"
    assert "UnpicklingError" in error.message and "swordfish" not in error.message


def test_a_state_is_applied_on_the_grid_it_was_fitted_on(stored):
    states, state = stored
    error = refused(ops.predict, predict_request("persistence", state, step=timedelta(hours=1)),
                    twin(), states)  # fmt: skip
    assert error.code == "model.bad_frame"
    assert error.detail == {"fitted_step": STEP.total_seconds()}


def test_interventions_are_refused_by_every_shipped_family(kept, stored):
    from dta_core.families import ORDER

    states, state = stored
    held = {"target": "Tank.level", "uid": "node:T1", "value": 1.0, "start": at(LENGTH)}
    for family in ORDER:
        # Before the state is read: the family is asked first.
        error = refused(ops.predict, predict_request(family, state, interventions=[held]),
                        twin(), states)  # fmt: skip
        assert error.code == "model.not_interventional"
        assert error.detail == {"family": family}


def test_a_lag_family_fitted_with_features_needs_them_ahead(kept):
    rng = np.random.default_rng(3)
    horizon = 4
    flow = {uid: rng.uniform(0, 1, LENGTH + horizon) for uid in ("node:T1", "node:T2")}
    series = {uid: 1.0 + values[:LENGTH] for uid, values in flow.items()}
    past = {uid: values[:LENGTH] for uid, values in flow.items()}
    ahead = {uid: [*values, *[None] * horizon] for uid, values in series.items()}
    table = frame(series, features={"Tank_inflow": past})
    params = {"lags": [1], "date_features": []}
    fitted = ops.fit(fit_request("ridge_lags", horizon, params=params),
                     Frames(fit=table), kept)  # fmt: skip

    def predict(drivers: Any = None, **frames: Any) -> Any:
        asked = drivers is not None
        if asked:
            frames["drivers"] = drivers
        request = predict_request("ridge_lags", fitted.state, horizon, drivers=asked)
        return ops.predict(request, Frames(**{"history": table, **frames}), kept)

    whole = frame(ahead, features={"Tank_inflow": flow})
    assert predict(drivers=whole)[0].num_rows == 2 * horizon
    # No drivers at all.
    error = refused(predict)
    assert error.code == "model.bad_frame" and error.detail == {"features": ["Tank_inflow"]}
    assert "drivers" in error.message
    # Drivers without the feature.
    error = refused(lambda: predict(drivers=frame(ahead)))
    assert error.code == "model.bad_frame" and error.detail == {"missing": ["Tank_inflow"]}
    # Drivers that stop before the horizon does, for one tank.
    cut = {**flow, "node:T2": [*flow["node:T2"][:-1], None]}
    error = refused(lambda: predict(drivers=frame(ahead, features={"Tank_inflow": cut})))
    assert error.code == "model.bad_frame" and error.detail == {"series": ["node:T2"]}
    # Drivers that hold the past only.
    error = refused(lambda: predict(drivers=table))
    assert error.code == "model.bad_frame"
    assert error.detail == {"series": ["node:T1", "node:T2"]}
    # A history without the feature the state was fitted with.
    error = refused(lambda: predict(drivers=whole, history=frame(series)))
    assert error.code == "model.bad_frame" and error.detail == {"missing": ["Tank_inflow"]}


def test_a_library_that_raises_is_a_failure_without_its_message(kept, monkeypatch, caplog):
    from statsforecast import StatsForecast

    def raising(*args: Any, **kwargs: Any) -> None:
        raise FloatingPointError("singular matrix at node:T1 = 5.0")

    monkeypatch.setattr(StatsForecast, "fit", raising)
    with caplog.at_level("ERROR"):
        error = refused(ops.fit, fit_request("theta", params=SMALL), twin(), kept)
    assert error.code == "model.failed"
    assert error.detail == {"family": "theta", "call": "fit", "error": "FloatingPointError"}
    assert "FloatingPointError" in error.message and "singular" not in error.message
    # The library's own words are in the log.
    assert any("singular matrix" in str(record.__dict__) for record in caplog.records)
    assert kept.files == {}
