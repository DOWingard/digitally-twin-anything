"""The correction ladder over a real store when no correction is ever accepted, and
each switch of the correction settings.

As in the breach tests, the breach opens at check 10. Every correction asked for is
reported back as not accepted right after the check that asked for it.
"""

from __future__ import annotations

import json

import pytest
from dta_runtime import fidelity
from dta_runtime.fidelity import CorrectionResult, FidelityError
from fidelity_testkit import Oracle, at, kinds, model_row, model_uid, no_offset, shifted

INCIDENT = "twin.fidelity.incident"
REMODEL = "twin.fidelity.remodel_requested"
STATE = "twin.fidelity.state"
REFUSED = CorrectionResult(accepted=False, model=None, fitted_to=None, error=0.9)


def breaching(make_plant, correction):
    plant = make_plant(correction=correction)
    for i in range(1, 6):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(plant, shifted(1.0, since=at(6)))
    outcome = plant.run(range(6, 11), oracle)[10]
    assert len(outcome.opened) == 1
    return plant, model, oracle, outcome


class Ladder:
    """Steps the plant one check at a time, refusing every correction asked for."""

    def __init__(self, plant, oracle, opened) -> None:
        self.plant = plant
        self.oracle = oracle
        self.incident = opened.opened[0]["uid"]
        self.keys: list[str] = []
        self.refuse(opened, 10)

    def refuse(self, outcome, i):
        for request in outcome.corrections:
            self.keys.append(request.key)
            fidelity.corrected(self.plant.twin, request, REFUSED, now=at(i))

    def step(self, i):
        outcome = self.plant.step(i, self.oracle)
        self.refuse(outcome, i)
        return outcome

    def asked(self, outcome):
        return [(found.rung, found.attempt, found.cycle) for found in outcome.corrections]

    def record(self):
        return fidelity.incident(self.plant.twin, self.incident)


def test_the_whole_ladder_up_to_the_retry_budget_and_a_retry(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 1, "retry_budget": 2, "remodel_timeout": 3600}
    )
    ladder = Ladder(plant, oracle, opened)
    assert ladder.asked(opened) == [("recalibrate", 0, 1)]
    assert ladder.asked(ladder.step(11)) == [("reselect", 0, 1)]

    remodel = ladder.step(12)
    assert remodel.corrections == []
    assert kinds(remodel.events) == [(INCIDENT, "changed"), (REMODEL, None)]
    event = remodel.events[1]["data"]
    assert (event["incident"], event["question"], event["spec"]) == (
        ladder.incident,
        "level",
        "level",
    )
    assert event["timeout_s"] == 3600.0 and event["requested_at"] == "2026-01-05T03:00:00Z"
    assert ladder.record()["status"] == "remodel_requested"
    assert ladder.step(13).events == []

    # The managing agent registers another candidate: rung 2 comes back.
    plant.register(
        {
            "TwinModel": [
                model_row("level", "seasonal", "Gauge.level", fitted_to=at(13), status="candidate")
            ]
        }
    )
    again = ladder.step(14)
    assert ladder.asked(again) == [("reselect", 1, 1)]
    assert kinds(again.events) == [(INCIDENT, "changed")]
    # That came to nothing too: the cycle ends and the next starts at rung 1.
    assert ladder.asked(ladder.step(15)) == [("recalibrate", 0, 2)]
    assert ladder.asked(ladder.step(16)) == [("reselect", 0, 2)]
    assert kinds(ladder.step(17).events) == [(INCIDENT, "changed"), (REMODEL, None)]
    # A changed candidate is a re-model as well.
    seasonal = model_uid("level", "seasonal")
    plant.register({"TwinModel": [{"uid": seasonal, "params": json.dumps({"season_length": 8})}]})
    assert ladder.asked(ladder.step(18)) == [("reselect", 1, 2)]
    failed = ladder.step(19)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    record = ladder.record()
    assert record["status"] == "failed"
    assert record["steps"][-1]["reason"] == "retry_budget"
    assert len(set(ladder.keys)) == len(ladder.keys) == 6
    assert fidelity.state(plant.twin)["state"] == "failed"

    retried = fidelity.retry(
        plant.twin, ladder.incident, by="operator", note="new data", now=at(19)
    )
    assert (retried["status"], retried["rung"], retried["attempt"], retried["cycle"]) == (
        "open",
        1,
        0,
        1,
    )
    assert kinds(retried["events"]) == [(INCIDENT, "changed"), (STATE, "correcting")]
    assert retried["steps"][-1]["by"] == "operator"
    after = ladder.step(20)
    # Cycle 1 again, under a key no earlier correction had.
    assert ladder.asked(after) == [("recalibrate", 0, 3)]
    assert after.corrections[0].key == f"correction:{ladder.incident}:3:recalibrate:0"
    assert len(set(ladder.keys)) == len(ladder.keys)


def test_a_remodel_that_never_comes_fails_and_a_later_fit_resolves(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 0, "reselect": False, "remodel_timeout": 3600}
    )
    # Neither rung 1 nor rung 2 is allowed: the re-model is asked for at once.
    assert opened.corrections == []
    assert kinds(opened.events) == [(INCIDENT, "opened"), (REMODEL, None), (STATE, "correcting")]
    ladder = Ladder(plant, oracle, opened)
    for i in (11, 12, 13):
        assert ladder.step(i).events == []
    failed = ladder.step(14)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    record = ladder.record()
    assert record["status"] == "failed"
    assert record["steps"][-1]["reason"] == "remodel_timeout"
    with pytest.raises(FidelityError) as refused:
        fidelity.resolve(plant.twin, ladder.incident, by="operator", note=None, now=at(14))
    assert refused.value.code == "fidelity.bad_request"

    # A model fitted after the failure, and three good origins after its data.
    oracle.offset = no_offset
    plant.sync(15)
    plant.refit(model, at(15))
    assert plant.check(15, oracle).events == []
    outcomes = plant.run(range(16, 20), oracle)
    for i in (16, 17, 18):
        assert outcomes[i].events == []
    resolved = outcomes[19]
    assert kinds(resolved.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    assert ladder.record()["steps"][-1]["action"] == "resolved"


def test_without_recalibration_the_first_step_is_reselect(make_plant):
    plant, model, oracle, opened = breaching(make_plant, {"recalibrate_attempts": 0})
    ladder = Ladder(plant, oracle, opened)
    assert ladder.asked(opened) == [("reselect", 0, 1)]
    assert kinds(ladder.step(11).events) == [(INCIDENT, "changed"), (REMODEL, None)]


def test_without_reselect_recalibration_is_followed_by_the_remodel(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 1, "reselect": False}
    )
    ladder = Ladder(plant, oracle, opened)
    assert ladder.asked(opened) == [("recalibrate", 0, 1)]
    remodel = ladder.step(11)
    assert remodel.corrections == []
    assert kinds(remodel.events) == [(INCIDENT, "changed"), (REMODEL, None)]


def test_without_remodel_a_cycle_ends_after_reselect(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 1, "agent_remodel": False, "retry_budget": 2}
    )
    ladder = Ladder(plant, oracle, opened)
    assert ladder.asked(ladder.step(11)) == [("reselect", 0, 1)]
    nexts = ladder.step(12)
    assert ladder.asked(nexts) == [("recalibrate", 0, 2)]
    assert REMODEL not in [event["type"] for event in nexts.events]
    assert ladder.asked(ladder.step(13)) == [("reselect", 0, 2)]
    failed = ladder.step(14)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    assert ladder.record()["steps"][-1]["reason"] == "retry_budget"
