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
        make_plant, {"recalibrate_attempts": 1, "retry_budget": 3, "remodel_timeout": 36000}
    )
    ladder = Ladder(plant, oracle, opened)
    assert ladder.asked(opened) == [("recalibrate", 0, 1)]
    assert ladder.asked(ladder.step(11)) == [("reselect", 0, 1)]

    # Cycle 1 has spent its rungs: the re-model is asked for, and cycle 2 is due one
    # fidelity interval later.
    remodel = ladder.step(12)
    assert remodel.corrections == []
    assert kinds(remodel.events) == [(INCIDENT, "changed"), (REMODEL, None)]
    event = remodel.events[1]["data"]
    assert (event["incident"], event["question"], event["spec"]) == (
        ladder.incident,
        "level",
        "level",
    )
    assert event["timeout_s"] == 36000.0 and event["requested_at"] == "2026-01-05T03:00:00Z"
    assert remodel.events[0]["data"]["next_cycle_at"] == "2026-01-05T03:15:00Z"
    record = ladder.record()
    assert record["status"] == "remodel_requested"
    assert record["detail"]["next_cycle_at"] == "2026-01-05T03:15:00Z"
    assert [(s["action"], s["cycle"], s["next_cycle_at"]) for s in record["steps"][-2:]] == [
        ("remodel_requested", 1, "2026-01-05T03:15:00Z"),
        ("cycle_ended", 1, "2026-01-05T03:15:00Z"),
    ]

    # Cycle 2 runs on newer data with the request open.
    second = ladder.step(13)
    assert ladder.asked(second) == [("recalibrate", 0, 2)]
    assert kinds(second.events) == [(INCIDENT, "changed")]
    assert ladder.record()["status"] == "remodel_requested"

    # The managing agent registers another candidate: rung 2 comes back.
    plant.register(
        {
            "TwinModel": [
                model_row("level", "seasonal", "Gauge.level", fitted_to=at(13), status="candidate")
            ]
        }
    )
    again = ladder.step(14)
    assert ladder.asked(again) == [("reselect", 1, 2)]
    assert kinds(again.events) == [(INCIDENT, "changed")]
    assert ladder.record()["status"] == "open"
    # That came to nothing too: cycle 3 is due two intervals after cycle 2 ended.
    assert ladder.step(15).corrections == []
    assert ladder.record()["detail"]["next_cycle_at"] == "2026-01-05T04:15:00Z"
    assert ladder.step(16).events == []
    assert ladder.asked(ladder.step(17)) == [("recalibrate", 0, 3)]
    assert ladder.asked(ladder.step(18)) == [("reselect", 0, 3)]
    # Cycle 3, the last, ends without a re-model: it is asked for again, and the
    # incident waits on it rather than failing.
    spent = ladder.step(19)
    assert kinds(spent.events) == [(INCIDENT, "changed"), (REMODEL, None)]
    record = ladder.record()
    assert record["status"] == "remodel_requested"
    assert record["steps"][-1]["action"] == "cycles_spent"
    assert record["detail"]["next_cycle_at"] is None
    assert ladder.step(20).events == []
    # A changed candidate is a re-model as well.
    seasonal = model_uid("level", "seasonal")
    plant.register({"TwinModel": [{"uid": seasonal, "params": json.dumps({"season_length": 8})}]})
    assert ladder.asked(ladder.step(21)) == [("reselect", 1, 3)]
    failed = ladder.step(22)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    record = ladder.record()
    assert record["status"] == "failed"
    assert record["steps"][-1]["reason"] == "retry_budget"
    assert len(set(ladder.keys)) == len(ladder.keys) == 7
    assert fidelity.state(plant.twin)["state"] == "failed"

    retried = fidelity.retry(
        plant.twin, ladder.incident, by="operator", note="new data", now=at(22)
    )
    assert (retried["status"], retried["rung"], retried["attempt"], retried["cycle"]) == (
        "open",
        1,
        0,
        1,
    )
    assert kinds(retried["events"]) == [(INCIDENT, "changed"), (STATE, "correcting")]
    assert retried["steps"][-1]["by"] == "operator"
    after = ladder.step(23)
    # Cycle 1 again, under a key no earlier correction had.
    assert ladder.asked(after) == [("recalibrate", 0, 4)]
    assert after.corrections[0].key == f"correction:{ladder.incident}:4:recalibrate:0"
    assert len(set(ladder.keys)) == len(ladder.keys)


def test_a_late_cycle_whose_model_holds_resolves_the_incident_its_request_open(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 1, "retry_budget": 3, "remodel_timeout": 36000}
    )
    ladder = Ladder(plant, oracle, opened)
    ladder.step(11)
    assert kinds(ladder.step(12).events) == [(INCIDENT, "changed"), (REMODEL, None)]
    # Cycle 2's recalibration, on data the first cycle did not have, is accepted.
    plant.sync(13)
    late = plant.check(13, oracle)
    (request,) = late.corrections
    assert (request.rung, request.cycle) == ("recalibrate", 2)
    done = fidelity.corrected(
        plant.twin,
        request,
        CorrectionResult(accepted=True, model=model, fitted_to=at(13), error=0.1),
        now=at(13),
    )
    assert done["status"] == "remodel_requested" and done["awaiting"] == at(13)
    assert done["steps"][-1]["cycle"] == 2
    plant.refit(model, at(13))
    oracle.offset = no_offset
    outcomes = plant.run(range(14, 18), oracle)
    held = outcomes[17]
    assert kinds(held.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    record = ladder.record()
    assert record["status"] == "resolved"
    assert (record["steps"][-1]["action"], record["steps"][-1]["cycle"]) == ("held", 2)


def test_spent_cycles_leave_the_request_open_until_its_timeout(make_plant):
    plant, model, oracle, opened = breaching(
        make_plant, {"recalibrate_attempts": 1, "retry_budget": 1, "remodel_timeout": 3600}
    )
    ladder = Ladder(plant, oracle, opened)
    ladder.step(11)
    asked = ladder.step(12)
    assert kinds(asked.events) == [(INCIDENT, "changed"), (REMODEL, None)]
    assert ladder.record()["steps"][-1]["action"] == "cycles_spent"
    for i in (13, 14, 15):
        assert ladder.step(i).events == []
    failed = ladder.step(16)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    assert ladder.record()["steps"][-1]["reason"] == "remodel_timeout"


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
    # Cycle 2 starts one fidelity interval after cycle 1 ended.
    ended = ladder.step(12)
    assert ended.corrections == []
    assert REMODEL not in [event["type"] for event in ended.events]
    assert ladder.record()["detail"]["next_cycle_at"] == at(13).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ladder.asked(ladder.step(13)) == [("recalibrate", 0, 2)]
    assert ladder.asked(ladder.step(14)) == [("reselect", 0, 2)]
    failed = ladder.step(15)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    assert ladder.record()["steps"][-1]["reason"] == "retry_budget"
