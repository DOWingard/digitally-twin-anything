"""The twin's own executed actions reach the models that hold under interventions, and
no other."""

from __future__ import annotations

import json
from datetime import timedelta

from dta_runtime.protocol.worker import InterventionRef
from fidelity_testkit import LEVEL_MODELS, Oracle, at, gauge, question, spec

ACTION = "twin_action:set_level"
G1, G2 = gauge("G1"), gauge("G2")


def record(number, *, value, effective, status="executed", obj=G1, params=None):
    return {
        "uid": f"twin_action_record:{number}",
        "action": ACTION,
        "object": obj,
        "params": json.dumps(params if params is not None else {"value": value}),
        "source": "operator",
        "proposed_at": effective - timedelta(minutes=5),
        "at_version": "unknown",
        "status": status,
        "effective_at": effective,
    }


def test_only_interventional_models_are_given_the_actions_taken(make_plant):
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "level_ctl": question("Gauge.level", 2, "level_ctl"),
        },
        models={**LEVEL_MODELS, "level_ctl": spec("level_ctl", "Gauge.level", interventional=True)},
    )
    for i in range(1, 10):
        plant.sync(i)
    plain = plant.activate("level", "Gauge.level", fitted_to=at(4))
    held = plant.activate("level_ctl", "Gauge.level", fitted_to=at(4), interventional=True)
    action = {
        "uid": ACTION,
        "control": "set_level",
        "target": "Gauge.level",
        "object_type": "Gauge",
        "binding": "executor",
        "status": "active",
    }
    records = [
        record(1, value=15.0, effective=at(8.5)),
        record(2, value=16.0, effective=at(9.5)),
        record(3, value=17.0, effective=at(9), obj=G2),
        # Outside the origin's horizon, not executed, or setting no value: none counts.
        record(4, value=18.0, effective=at(7)),
        record(5, value=19.0, effective=at(9), status="approved"),
        record(6, value=None, effective=at(9), params={"mode": "auto"}),
    ]
    plant.register({"TwinAction": [action], "TwinActionRecord": records})
    oracle = Oracle(plant)
    plant.step(10, oracle)
    calls = {call["model"]: call for call in oracle.calls}
    assert set(calls) == {plain, held}
    assert calls[plain]["interventions"] == []
    assert calls[held]["origin"] == at(8)
    assert calls[held]["interventions"] == [
        InterventionRef(target="Gauge.level", uid=G1, value=15.0, start=at(8.5), end=at(9.5)),
        InterventionRef(target="Gauge.level", uid=G2, value=17.0, start=at(9)),
        InterventionRef(target="Gauge.level", uid=G1, value=16.0, start=at(9.5)),
    ]
