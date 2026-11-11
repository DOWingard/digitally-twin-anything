"""The agreements between the API, the worker and the code that computes."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pyarrow as pa
import pytest
from dta_core.families import FAMILIES
from dta_core.functions import REGISTERED_ENTRY
from dta_runtime.protocol import (
    OPS,
    Answer,
    BacktestRequest,
    FamilyRef,
    FeatureRef,
    FitRequest,
    FrameRef,
    FunctionRequest,
    Interval,
    PredictRequest,
    TrainingSetRequest,
    WorkerRequest,
    answer_document,
    prediction_columns,
)
from dta_runtime.protocol.worker import ENTRY
from pydantic import TypeAdapter, ValidationError

REQUEST = TypeAdapter(WorkerRequest)
RUN = {"budget_s": 30, "trace_id": "0af7651916cd43dd8448eb211c80319c"}
FRAME = {"target": "Tank.level", "start": "v1", "end": "v9", "step": "PT15M"}
SHA = "ab" * 32


def test_a_request_is_told_apart_by_its_op():
    cases = {
        "function": ({"artifact": SHA, "entry": "plant:low_pressure", "at": "v9"}, FunctionRequest),
        "fit": ({"family": {"name": "theta"}, "frame": FRAME, "horizon": 8}, FitRequest),
        "backtest": (
            {
                "family": {"artifact": SHA, "entry": "plant.hydraulics:Network"},
                "frame": FRAME,
                "horizon": 8,
                "step": 4,
                "windows": 20,
                "refit": 5,
                "metrics": ["mae"],
                "baseline": "seasonal_naive",
            },
            BacktestRequest,
        ),
        "predict": (
            {"family": {"name": "ets"}, "state": SHA, "history": FRAME, "horizon": 8},
            PredictRequest,
        ),
    }
    assert set(cases) == set(OPS)
    for op, (body, kind) in cases.items():
        request = REQUEST.validate_python({"op": op, **RUN, **body})
        assert type(request) is kind
        # What is sent is what is read back.
        assert REQUEST.validate_json(REQUEST.dump_json(request)) == request
    fit = REQUEST.validate_python({"op": "fit", **RUN, **cases["fit"][0]})
    assert fit.frame.step == timedelta(minutes=15) and fit.frame.branch == "live"
    # Without a refit the worker chooses one that fits the budget.
    chosen = {**cases["backtest"][0]}
    del chosen["refit"]
    assert REQUEST.validate_python({"op": "backtest", **RUN, **chosen}).refit is None


@pytest.mark.parametrize(
    "body",
    [
        {"op": "train", **RUN},
        {"op": "fit", **RUN, "family": {"name": "theta"}, "horizon": 8},
        {"op": "fit", **RUN, "family": {"name": "theta"}, "frame": FRAME},
        {"op": "fit", **RUN, "family": {"name": "theta"}, "frame": FRAME, "horizon": 0},
        {"op": "fit", **RUN, "family": {"name": "theta"}, "frame": FRAME, "horizon": 8, "extra": 1},
        {
            "op": "fit",
            "trace_id": "t",
            "budget_s": 0,
            "family": {"name": "theta"},
            "frame": FRAME,
            "horizon": 8,
        },
        {
            "op": "fit",
            **RUN,
            "family": {"name": "theta"},
            "frame": {**FRAME, "ahead": -1},
            "horizon": 8,
        },
        {
            "op": "predict",
            **RUN,
            "family": {"name": "ets"},
            "state": "short",
            "history": FRAME,
            "horizon": 8,
        },
        {"op": "function", **RUN, "artifact": SHA, "entry": "low_pressure", "at": "v9"},
    ],
)
def test_a_request_that_cannot_be_run_is_refused(body):
    with pytest.raises(ValidationError):
        REQUEST.validate_python(body)


def test_a_frame_names_its_features_and_how_each_is_read():
    step = timedelta(minutes=15)
    frame = FrameRef(
        **FRAME,
        features=[
            "Tank.inflow",
            {"property": "Tank.level", "lag": "PT30M"},
            {"property": "Pump.speed", "via": "Feeds", "agg": "mean", "direction": "in"},
            {"property": "Pump.speed", "via": "Feeds", "agg": "max", "lag": "PT1H", "name": "peak"},
        ],
        ahead=8,
    )
    assert [feature.column(step) for feature in frame.features] == [
        "Tank_inflow",
        "Tank_level_lag2",
        "Pump_speed_mean",
        "peak",
    ]
    assert frame.features[0] == FeatureRef(property="Tank.inflow")
    assert frame.features[0].lag == timedelta(0) and frame.features[0].direction == "both"
    assert FrameRef.model_validate_json(frame.model_dump_json()) == frame
    # A training set is a frame, with the label of each row when it has a horizon.
    wanted = TrainingSetRequest(**FRAME, features=["Tank.inflow"], horizon="PT2H")
    assert wanted.horizon == timedelta(hours=2) and TrainingSetRequest(**FRAME).horizon is None
    with pytest.raises(ValidationError):
        FeatureRef(property="Tank.level", agg="median", via="Feeds")


def test_a_family_is_a_shipped_name_or_code_never_both_or_neither():
    assert FamilyRef(name=next(iter(FAMILIES))).name == "persistence"
    assert FamilyRef(artifact=SHA, entry="plant:Network").entry == "plant:Network"
    for bad in (
        {},
        {"name": "theta", "artifact": SHA, "entry": "a:b"},
        {"artifact": SHA},
        {"entry": "a:b"},
        {"name": "theta", "entry": "a:b"},
    ):
        with pytest.raises(ValidationError):
            FamilyRef(**bad)
    # The worker and the manifest's checks agree on how code is named.
    assert ENTRY == REGISTERED_ENTRY.pattern


def test_the_columns_of_a_prediction_and_the_document_of_an_answer():
    assert prediction_columns([]) == ["unique_id", "ds", "mean"]
    assert prediction_columns([80, 95]) == [
        "unique_id",
        "ds",
        "mean",
        "lo_80",
        "hi_80",
        "lo_95",
        "hi_95",
    ]
    version = {"id": "v9", "seq": 9, "t": "2026-01-05T02:00:00Z"}
    document = answer_document(
        Answer(7.94, unit="m", interval=Interval(7.8, 8.1, 95), notes=("outside_validity",)),
        version=version,
        function="f_level_ahead",
        model="m_level/theta",
        provisional=True,
        reasons=["fewer origins than min_folds"],
    )
    assert document == {
        "value": 7.94,
        "unit": "m",
        "interval": {"low": 7.8, "high": 8.1, "level": 95},
        "notes": ["outside_validity"],
        "version": version,
        "function": "f_level_ahead",
        "model": "m_level/theta",
        "provisional": True,
        "reasons": ["fewer origins than min_folds"],
    }
    with pytest.raises(TypeError):
        answer_document(
            Answer(pa.table({"uid": ["a"]})),
            version=version,
            function="f",
            model=None,
            provisional=False,
        )


def test_every_op_has_a_handler_and_a_pool():
    from dta_runtime.protocol import BATCH_OPS, HANDLERS, runs_own_code

    assert set(HANDLERS) == set(OPS)
    assert all(REGISTERED_ENTRY.match(where) for where in HANDLERS.values())
    assert BATCH_OPS == {"fit", "backtest"}
    # Code the twin brought: a registered function, or a family given as an artifact.
    shipped = REQUEST.validate_python(
        {"op": "fit", **RUN, "family": {"name": "theta"}, "frame": FRAME, "horizon": 4}
    )
    own = REQUEST.validate_python(
        {
            "op": "fit",
            **RUN,
            "family": {"artifact": SHA, "entry": "plant:Network"},
            "frame": FRAME,
            "horizon": 4,
        }
    )
    function = REQUEST.validate_python(
        {"op": "function", **RUN, "artifact": SHA, "entry": "plant:low", "at": "v9"}
    )
    assert [runs_own_code(found) for found in (shipped, own, function)] == [False, True, True]


def test_the_agreements_bring_no_model_library_with_them():
    """The API imports them; it must not pay for, or hold, what only the worker runs."""
    code = (
        "import sys, dta_runtime.protocol\n"
        "heavy = {'pandas', 'statsforecast', 'mlforecast', 'sklearn', 'scipy'} & set(sys.modules)\n"
        "print(sorted(heavy))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"
