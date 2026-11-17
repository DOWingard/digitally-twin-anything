"""The handlers on frames the twin builds from a real store: what a fit, a backtest and
a prediction read is what `training.frame` gives for the reference they were handed."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from dta_core.store import LIVE_BRANCH
from dta_runtime.models import ops
from dta_runtime.protocol import BacktestRequest, FitRequest, FrameRef, PredictRequest
from dta_runtime.training import frame
from dta_runtime.twin import Twin
from models_testkit import RUN, Kept
from training_testkit import STEP, at, commit, node, tank

VERSIONS = 60


def levels(index: int) -> dict[str, float]:
    """Two tanks, one filling and one draining, each by the same amount every step."""
    return {"T1": 2.0 + 0.25 * index, "T2": 18.0 - 0.125 * index}


class Stored:
    """A reader of the twin over its store, as the worker's is over the API."""

    branch = LIVE_BRANCH

    def __init__(self, twin: Twin, version: str) -> None:
        self._twin = twin
        self.at = version
        self.asked: list[FrameRef] = []

    def get(self, path: str, **params: Any) -> Any:
        raise AssertionError(f"a handler read {path}")

    def table(self, path: str, **params: Any) -> pa.Table:
        raise AssertionError(f"a handler read {path}")

    def frame(self, ref: FrameRef) -> pa.Table:
        self.asked.append(ref)
        return frame(self._twin.store, self._twin.ontology, ref)


@pytest.fixture(scope="module")
def history(water_twin: Twin) -> dict[int, str]:
    """Version ids by their place: version i is stored at step i."""
    ids = {}
    for index in range(1, VERSIONS + 1):
        rows = [tank(key, level) for key, level in levels(index).items()]
        ids[index] = commit(water_twin, at(index), {"Tank": rows}).id
    return ids


def ref(history: dict[int, str], first: int, last: int) -> dict[str, Any]:
    return {
        "target": "Tank.level",
        "branch": LIVE_BRANCH,
        "start": history[first],
        "end": history[last],
        "step": STEP,
    }


LINE = {"lags": [1, 2], "date_features": [], "alpha": 1e-9}


def test_a_fit_and_a_prediction_read_the_frames_their_references_name(water_twin, history):
    kept = Kept()
    twin = Stored(water_twin, history[VERSIONS])
    fitted = ops.fit(
        FitRequest(**RUN, family={"name": "ridge_lags"}, params=LINE, frame=ref(history, 1, 40),
                   horizon=4),
        twin,
        kept,
    )  # fmt: skip
    assert [asked.end for asked in twin.asked] == [history[40]]
    assert (fitted.series, fitted.rows) == (2, 80)
    assert (fitted.fitted_from, fitted.fitted_to) == (at(1), at(40))
    assert fitted.validity == {"y": (2.25, 17.875)}

    # Twenty versions later: the state is applied to what the store holds by then.
    table, result = ops.predict(
        PredictRequest(**RUN, family={"name": "ridge_lags"}, state=fitted.state,
                       history=ref(history, 30, VERSIONS), horizon=4, levels=[80]),
        twin,
        kept,
    )  # fmt: skip
    rows = table.to_pylist()
    assert [(row["unique_id"], row["ds"]) for row in rows] == [
        (node(key), at(VERSIONS + ahead)) for key in ("T1", "T2") for ahead in range(1, 5)
    ]
    # A level that changes by the same amount every step goes on doing so.
    assert [row["mean"] for row in rows] == pytest.approx(
        [levels(VERSIONS + ahead)[key] for key in ("T1", "T2") for ahead in range(1, 5)],
        abs=1e-4,
    )
    assert all(row["lo_80"] <= row["mean"] <= row["hi_80"] for row in rows)
    # The range the fit saw is of every series together: neither tank has left it.
    assert result.notes == []
    assert (result.rows, result.series) == (8, 2)


def test_a_backtest_reads_the_frame_its_reference_names(water_twin, history):
    kept = Kept()
    twin = Stored(water_twin, history[VERSIONS])
    result = ops.backtest(
        BacktestRequest(**RUN, family={"name": "persistence"}, frame=ref(history, 1, VERSIONS),
                        horizon=4, step=2, windows=3, metrics=["mae", "max_abs"],
                        baseline="persistence"),
        twin,
        kept,
    )  # fmt: skip
    assert len(twin.asked) == 1
    # Persistence is off by the change of k steps, k steps ahead: (1 + 2 + 3 + 4) / 4
    # changes on average, 4 at most.
    assert result.scores == pytest.approx({"mae": (0.25 + 0.125) / 2 * 2.5, "max_abs": 1.0})
    assert result.baseline_scores == result.scores
    assert (result.folds, result.series, result.notes) == (3, 2, [])
    residuals = pa.ipc.open_file(pa.BufferReader(kept.files[result.residuals])).read_all()
    assert residuals.num_rows == 4 * 5
