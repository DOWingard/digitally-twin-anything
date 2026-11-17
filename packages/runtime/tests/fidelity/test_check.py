"""A check over a real store: a healthy twin, a check asked for again, the checks a
twin refuses, and what a check records."""

from __future__ import annotations

import pytest
from dta_runtime import fidelity
from dta_runtime.fidelity import FidelityError
from fidelity_testkit import (
    LEVEL_MODELS,
    Oracle,
    at,
    gauge,
    kinds,
    level,
    question,
    spec,
)

STATE = "twin.fidelity.state"


def test_a_healthy_twin(make_plant):
    plant = make_plant()
    for i in range(1, 5):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(plant)
    outcomes = plant.run(range(5, 13), oracle)

    first = outcomes[5]
    assert (first.state_before, first.state_after) == ("unknown", "healthy")
    assert kinds(first.events) == [(STATE, "healthy")]
    # The origin of the first check (step 3) is older than the end of the model's data.
    entry = first.check["questions"]["level"]
    assert not entry["scored"] and entry["reason"]["code"] == "before_fit"

    scored = outcomes[6].check["questions"]["level"]
    assert scored["scored"] and scored["origin"] == "2026-01-05T01:00:00Z"
    assert scored["model"] == model
    assert scored["error"] == 0.0
    # Two steps of four gauges.
    assert scored["n"] == 8
    assert scored["series"] == {gauge(key): 0.0 for key in ("G1", "G2", "G3", "G4")}
    assert scored["estimates"][gauge("G2")] == level(gauge("G2"), 6)
    assert scored["interval_ratio"] == pytest.approx(1.0)

    for outcome in list(outcomes.values())[1:]:
        assert outcome.events == [] and outcome.corrections == []
        assert outcome.state_after == "healthy"
    last = outcomes[12].check["questions"]["level"]
    # Origins 4 to 10.
    assert last["window"] == {"error": 0.0, "n": 7, "breach": False}
    assert fidelity.state(plant.twin)["state"] == "healthy"
    assert fidelity.state(plant.twin)["since"] == at(5)
    assert fidelity.incidents(plant.twin) == []
    errors = fidelity.errors(plant.twin, "level")
    assert [found["at"] for found in errors] == [at(i) for i in range(5, 13)]
    assert [found["scored"] for found in errors] == [False] + [True] * 7
    assert len(fidelity.checks(plant.twin, limit=3)) == 3
    assert fidelity.checks(plant.twin, limit=1)[0]["at"] == at(12)

    # Each check asked once, for the model's origin, at the right horizon.
    asked = [call for call in oracle.calls]
    assert [call["origin"] for call in asked] == [at(i) for i in range(4, 11)]
    assert {call["horizon"] for call in asked} == {2}
    assert all(call["interventions"] == [] for call in asked)


def test_a_check_asked_for_again_gives_its_outcome_and_writes_nothing(make_plant):
    plant = make_plant()
    for i in range(1, 5):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(plant)
    first = plant.run(range(5, 7), oracle)[5]
    head = plant.store.head()
    calls = len(oracle.calls)
    again = plant.check(5, oracle)
    assert again.replayed and not first.replayed
    assert plant.store.head() == head
    assert len(oracle.calls) == calls
    assert again.check == first.check
    assert (
        again.events
        == first.events
        == [
            {
                "type": STATE,
                "subject": "twin_fidelity:state",
                "data": {
                    "from": "unknown",
                    "to": "healthy",
                    "since": "2026-01-05T01:15:00Z",
                    "check": "twin_fidelity_check:20260105T011500Z",
                },
            }
        ]
    )
    assert (again.state_before, again.state_after) == (first.state_before, first.state_after)
    assert again.version == first.version


def test_a_check_before_the_latest_is_refused(make_plant):
    plant = make_plant()
    plant.sync(1)
    plant.sync(2)
    oracle = Oracle(plant)
    plant.check(2, oracle)
    with pytest.raises(FidelityError) as refused:
        plant.check(1, oracle)
    assert refused.value.code == "fidelity.out_of_order"


def test_a_static_twin_has_no_checks(make_plant):
    plant = make_plant(live=False)
    with pytest.raises(FidelityError) as refused:
        plant.check(1, Oracle(plant))
    assert refused.value.code == "fidelity.not_live"


def test_questions_that_cannot_be_scored_say_why(make_plant):
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "odd": question("Gauge.level", 1, "odd"),
            "flow": question("Gauge.flow", 2, "flow"),
        },
        models={
            **LEVEL_MODELS,
            "odd": spec("odd", "Gauge.level"),
            "flow": spec("flow", "Gauge.flow"),
        },
    )
    for i in range(1, 5):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(1))
    plant.activate("odd", "Gauge.level", fitted_to=at(1))
    outcome = plant.check(4, Oracle(plant, refuse=True))
    entries = outcome.check["questions"]
    assert entries["level"]["reason"]["code"] == "predictor_refused"
    assert entries["flow"]["reason"]["code"] == "no_active_model"
    # Step 4's sync is the newest version: the origin of a one-step horizon is step 3,
    # and its forecast ends at step 4.
    assert entries["odd"]["origin"] == "2026-01-05T00:45:00Z"
    assert outcome.state_after == "healthy"
