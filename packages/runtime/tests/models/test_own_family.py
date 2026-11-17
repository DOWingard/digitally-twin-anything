"""A family of the twin's own: code the worker put on `sys.path`, named by its entry,
taken through a fit, a backtest fold by fold and a prediction under an intervention."""

from __future__ import annotations

import io
import pickle

import numpy as np
import pytest
from dta_core.errors import DtaError
from dta_runtime.models import ops
from dta_runtime.models.state import read_header
from models_testkit import Frames, at, backtest_request, fit_request, frame, predict_request

SHA = "cd" * 32
OWN = {"artifact": SHA, "entry": "plant_hydraulics:filling"}
LENGTH = 60
# Two tanks that fill at a rate of their own.
SERIES = {
    "node:T1": 3.0 + 0.5 * np.arange(LENGTH),
    "node:T2": 40.0 - 0.25 * np.arange(LENGTH),
}

CODE = '''
"""A tank fills at the rate it has filled at; a level that is held stays where it is
held, and the tank fills on from there."""

import pandas as pd


class Filling:
    interventional = True

    def __init__(self, step, version):
        self.step = step
        self.version = version

    def fit(self, frame, params, *, horizon):
        rates = frame.groupby("unique_id")["y"].agg(lambda y: float(y.diff().mean()))
        return {"rates": rates.to_dict(), "scale": params.get("scale", 1.0),
                "made_at": self.version}

    def predict(self, state, history, horizon, *, levels=(), drivers=None, interventions=()):
        parts = []
        for uid, rows in history.groupby("unique_id", sort=True):
            level, moment = float(rows["y"].iloc[-1]), rows["ds"].iloc[-1]
            for _ in range(horizon):
                moment = moment + self.step
                held = [
                    found.value
                    for found in interventions
                    if found.target == "Tank.level" and found.uid == uid
                    and found.start <= moment and (found.end is None or moment <= found.end)
                ]
                level = float(held[-1]) if held else level + state["rates"][uid] * state["scale"]
                parts.append({"unique_id": uid, "ds": moment, "mean": level})
        return pd.DataFrame(parts)

    def validity(self, frame):
        return {"y": (float(frame["y"].min()), float(frame["y"].max()))}


def filling(*, step, twin):
    return Filling(step, twin.at)


def broken(*, step, twin):
    raise RuntimeError("valve map missing: the secret word is swordfish")


def nothing(*, step, twin):
    return {"fit": None}


class Leaking(Filling):
    def fit(self, frame, params, *, horizon):
        raise ValueError("the secret word is swordfish")


def leaking(*, step, twin):
    return Leaking(step, twin.at)


class Shapeless(Filling):
    def predict(self, state, history, horizon, **more):
        return [1.0] * horizon


def shapeless(*, step, twin):
    return Shapeless(step, twin.at)
'''


@pytest.fixture
def twin(own_code) -> Frames:
    own_code("plant_hydraulics", CODE)
    table = frame(SERIES)
    return Frames(fit=table, history=table)


def test_a_family_of_the_twins_own_is_fitted_and_stored_under_its_entry(twin, kept):
    fitted = ops.fit(fit_request(OWN, 6, params={"scale": 1.0}), twin, kept)
    assert (fitted.series, fitted.rows, fitted.notes) == (2, 2 * LENGTH, [])
    assert fitted.validity == {"y": (3.0, 40.0)}
    header, body = read_header(kept.files[fitted.state])
    assert header["family"] == OWN
    assert header["params"] == {"scale": 1.0}
    # The factory was called with the grid's step and a reader of the twin.
    assert pickle.load(io.BytesIO(body)) == {
        "rates": {"node:T1": 0.5, "node:T2": -0.25},
        "scale": 1.0,
        "made_at": "v9",
    }


def test_it_predicts_under_an_intervention(twin, kept):
    fitted = ops.fit(fit_request(OWN, 6), twin, kept)
    plain, result = ops.predict(predict_request(OWN, fitted.state, 6), twin, kept)
    assert plain.schema.names == ["unique_id", "ds", "mean"]
    assert plain.column("mean").to_pylist() == pytest.approx(
        [3.0 + 0.5 * step for step in range(LENGTH, LENGTH + 6)]
        + [40.0 - 0.25 * step for step in range(LENGTH, LENGTH + 6)]
    )
    assert result.notes == []

    # The first tank is held at 10 for the second and third step ahead.
    held = {"target": "Tank.level", "uid": "node:T1", "value": 10.0,
            "start": at(LENGTH + 1), "end": at(LENGTH + 2)}  # fmt: skip
    found, result = ops.predict(
        predict_request(OWN, fitted.state, 6, interventions=[held]), twin, kept
    )
    first = [row["mean"] for row in found.to_pylist() if row["unique_id"] == "node:T1"]
    assert first == pytest.approx([33.0, 10.0, 10.0, 10.5, 11.0, 11.5])
    # The other tank is as it was.
    assert found.column("mean").to_pylist()[6:] == plain.column("mean").to_pylist()[6:]
    # Until the end of the horizon, when no end is given.
    found, _ = ops.predict(
        predict_request(OWN, fitted.state, 6, interventions=[{**held, "end": None}]), twin, kept
    )
    assert found.column("mean").to_pylist()[:6] == pytest.approx([33.0] + [10.0] * 5)

    # It gives no intervals of its own: asked for, they are left out and that is said.
    found, result = ops.predict(predict_request(OWN, fitted.state, 6, levels=[80]), twin, kept)
    assert found.schema.names == ["unique_id", "ds", "mean"]
    assert result.notes == ["no_intervals"]


def test_it_is_backtested_fold_by_fold_and_its_residuals_give_it_intervals(twin, kept):
    result = ops.backtest(
        backtest_request(OWN, metrics=["mae", "max_abs"], baseline="persistence", horizon=6),
        twin,
        kept,
    )
    # A constant rate is what it models: no error, where persistence is off by the rate.
    assert result.scores == pytest.approx({"mae": 0.0, "max_abs": 0.0}, abs=1e-9)
    assert result.baseline_scores == pytest.approx({"mae": (0.5 + 0.25) / 2 * 3.5, "max_abs": 3.0})
    assert (result.folds, result.series, result.refit) == (5, 2, 1)
    # Half the true rate: off by a quarter of a unit more with every step ahead.
    slow = ops.backtest(
        backtest_request(OWN, params={"scale": 0.5}, metrics=["max_abs"], horizon=6, refit=False),
        twin,
        kept,
    )
    assert slow.scores == pytest.approx({"max_abs": 0.25 * 6})
    assert slow.refit is False

    fitted = ops.fit(fit_request(OWN, 6, params={"scale": 0.5}), twin, kept)
    found, predicted = ops.predict(
        predict_request(OWN, fitted.state, 6, levels=[80], residuals=slow.residuals), twin, kept
    )
    assert found.schema.names == ["unique_id", "ds", "mean", "lo_80", "hi_80"]
    assert predicted.notes == []
    widths = [
        row["hi_80"] - row["mean"] for row in found.to_pylist() if row["unique_id"] == "node:T1"
    ]
    # The 0.8 quantile of |error| k steps ahead, over two tanks off by 0.25 k and 0.125 k.
    assert widths == pytest.approx(
        [float(np.quantile([0.25 * k] * 5 + [0.125 * k] * 5, 0.8)) for k in range(1, 7)]
    )


@pytest.mark.parametrize(
    ("entry", "code", "said", "unsaid"),
    [
        ("plant_hydraulics:broken", "model.bad_family", "RuntimeError", "swordfish"),
        ("plant_hydraulics:absent", "model.bad_family", "AttributeError", "plant_hydraulics.py"),
        ("no_such_module:filling", "model.bad_family", "ModuleNotFoundError", "sys.path"),
        (
            "plant_hydraulics:nothing",
            "model.bad_family",
            "interventional, fit, predict, validity",
            "",
        ),
        ("plant_hydraulics:leaking", "model.failed", "ValueError", "swordfish"),
    ],
)
def test_code_of_the_twins_own_that_fails_is_refused_without_its_message(
    twin, kept, entry, code, said, unsaid
):
    with pytest.raises(DtaError) as caught:
        ops.fit(fit_request({"artifact": SHA, "entry": entry}), twin, kept)
    assert caught.value.code == code
    assert said in caught.value.message
    # What the code said goes to the log, not into the answer.
    assert not unsaid or unsaid not in str(caught.value.message) + str(caught.value.detail)


def test_a_prediction_that_is_not_a_frame_of_the_columns_is_refused(twin, kept):
    family = {"artifact": SHA, "entry": "plant_hydraulics:shapeless"}
    fitted = ops.fit(fit_request(family, 6), twin, kept)
    with pytest.raises(DtaError) as caught:
        ops.predict(predict_request(family, fitted.state, 6), twin, kept)
    assert caught.value.code == "model.bad_family"
