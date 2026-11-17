"""A breach of a question's tolerance: when it opens, the correction it asks for, what
becomes of it once the correction is reported back; and a warning before a breach.

The model of `level` is fitted to step 4 and predicts the plant exactly until the
test shifts its predictions. With a shift of 1.0 from origin 6 on and a tolerance of
0.5 (mae), the window holds the origins 4, 5 (error 0) and then the bad ones:
1/3 at check 8, 2/4 = 0.5 at check 9 (not over), 3/5 = 0.6 at check 10: the breach
opens at the third bad origin.
"""

from __future__ import annotations

import pytest
from dta_runtime import fidelity
from dta_runtime.fidelity import CorrectionRequest, CorrectionResult, FidelityError
from dta_runtime.fidelity.records import incident_uid, stamp
from fidelity_testkit import Oracle, at, kinds, no_offset, shifted

INCIDENT = "twin.fidelity.incident"
STATE = "twin.fidelity.state"


def breaching(make_plant, **options):
    plant = make_plant(**options)
    for i in range(1, 6):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    return plant, model, Oracle(plant, shifted(1.0, since=at(6)))


def test_a_breach_opens_at_the_third_bad_origin_and_asks_for_one_recalibration(make_plant):
    plant, model, oracle = breaching(make_plant)
    outcomes = plant.run(range(6, 12), oracle)
    windows = {i: outcomes[i].check["questions"]["level"]["window"] for i in outcomes}
    assert windows[8] == {"error": pytest.approx(1 / 3), "n": 3, "breach": False}
    assert windows[9] == {"error": pytest.approx(0.5), "n": 4, "breach": False}
    assert windows[10] == {"error": pytest.approx(0.6), "n": 5, "breach": True}
    assert all(outcomes[i].opened == [] for i in (6, 7, 8, 9))

    opened = outcomes[10]
    uid = incident_uid("breach", "level", at(10))
    assert [found["uid"] for found in opened.opened] == [uid]
    incident = opened.opened[0]
    assert (incident["kind"], incident["subject"], incident["status"]) == (
        "breach",
        "level",
        "open",
    )
    assert (incident["rung"], incident["attempt"], incident["cycle"]) == (1, 0, 1)
    assert opened.corrections == [
        CorrectionRequest(
            incident=uid,
            question="level",
            spec="level",
            rung="recalibrate",
            cycle=1,
            attempt=0,
            key=f"correction:{uid}:1:recalibrate:0",
            warning=False,
        )
    ]
    assert kinds(opened.events) == [(INCIDENT, "opened"), (STATE, "correcting")]
    assert opened.events[0]["data"]["check"] == opened.check["uid"]
    assert opened.state_after == "correcting"
    # The request is returned once; the incident waits for its answer.
    assert outcomes[11].corrections == [] and outcomes[11].events == []
    assert fidelity.state(plant.twin)["state"] == "correcting"


def test_an_accepted_correction_is_awaited_and_resolves_when_it_holds(make_plant):
    plant, model, oracle = breaching(make_plant)
    request = plant.run(range(6, 11), oracle)[10].corrections[0]
    plant.step(11, oracle)
    done = fidelity.corrected(
        plant.twin,
        request,
        CorrectionResult(accepted=True, model=model, fitted_to=at(11), error=0.1),
        now=at(11),
    )
    assert done["awaiting"] == at(11) and done["detail"]["pending"] is None
    assert kinds(done["events"]) == [(INCIDENT, "changed")]
    assert [step["action"] for step in done["steps"]] == ["opened", "request", "result"]
    # Reported again: nothing is waiting for it any more.
    head = plant.store.head()
    again = fidelity.corrected(
        plant.twin,
        request,
        CorrectionResult(accepted=True, model=model, fitted_to=at(11), error=0.1),
        now=at(11),
    )
    assert again["version"] is None and again["events"] == []
    assert plant.store.head() == head

    plant.refit(model, at(11))
    oracle.offset = no_offset
    outcomes = plant.run(range(12, 16), oracle)
    reasons = outcomes[12].check["questions"]["level"]["reason"]
    assert reasons["code"] == "before_fit"
    for i in (12, 13, 14):
        assert outcomes[i].events == []
    held = outcomes[15]
    assert [found["subject"] for found in held.resolved] == ["level"]
    assert kinds(held.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    record = fidelity.incident(plant.twin, request.incident)
    assert record["status"] == "resolved" and record["resolved_at"] == at(15)
    assert record["resolved_version"] == held.version.id
    assert [step["action"] for step in record["steps"]][-1] == "held"
    assert record["steps"][-1]["n"] == 3


def test_a_correction_that_does_not_hold_moves_to_the_next_attempt(make_plant):
    plant, model, oracle = breaching(make_plant)
    request = plant.run(range(6, 11), oracle)[10].corrections[0]
    fidelity.corrected(
        plant.twin,
        request,
        CorrectionResult(accepted=True, model=model, fitted_to=at(11), error=0.1),
        now=at(10),
    )
    plant.sync(11)
    plant.refit(model, at(11))
    plant.check(11, oracle)
    moved = plant.run(range(12, 16), oracle)[15]
    assert kinds(moved.events) == [(INCIDENT, "changed")]
    assert [found.key for found in moved.corrections] == [
        f"correction:{request.incident}:1:recalibrate:1"
    ]
    record = fidelity.incident(plant.twin, request.incident)
    assert [step["action"] for step in record["steps"]][-2:] == ["not_held", "request"]
    assert (record["rung"], record["attempt"], record["awaiting"]) == (1, 1, None)


def test_a_correction_is_reported_back_only_for_its_incident(make_plant):
    plant, model, oracle = breaching(make_plant)
    request = plant.run(range(6, 11), oracle)[10].corrections[0]
    result = CorrectionResult(accepted=True, model=model, fitted_to=None, error=None)
    with pytest.raises(FidelityError) as refused:
        fidelity.corrected(plant.twin, request, result, now=at(10))
    assert refused.value.code == "fidelity.bad_request"
    unknown = CorrectionRequest(
        incident="twin_incident:breach-none",
        question="level",
        spec="level",
        rung="recalibrate",
        cycle=1,
        attempt=0,
        key="correction:twin_incident:breach-none:1:recalibrate:0",
        warning=False,
    )
    with pytest.raises(FidelityError) as missing:
        fidelity.corrected(
            plant.twin, unknown, CorrectionResult(False, None, None, None), now=at(10)
        )
    assert missing.value.code == "fidelity.not_found"


def test_a_warning_asks_for_one_recalibration_and_stays_healthy(make_plant):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    # e = 0.48 at every origin: x = 0.96, S grows by 0.16 an origin and passes 1.0 at
    # the seventh (check 12); the window error stays under the tolerance.
    oracle = Oracle(plant, shifted(0.48, since=at(4)))
    outcomes = plant.run(range(6, 21), oracle)
    sums = {i: outcomes[i].check["questions"]["level"]["warning"]["sum"] for i in outcomes}
    assert sums[11] == pytest.approx(0.96)
    assert all(outcomes[i].corrections == [] for i in range(6, 12))
    warned = outcomes[12]
    pseudo = f"warning-level-{stamp(at(12))}"
    assert warned.corrections == [
        CorrectionRequest(
            incident=pseudo,
            question="level",
            spec="level",
            rung="recalibrate",
            cycle=0,
            attempt=0,
            key=f"correction:{pseudo}:0:recalibrate:0",
            warning=True,
        )
    ]
    entry = warned.check["questions"]["level"]
    assert entry["warning"]["raised"] and entry["warning"]["sum"] == 0.0
    assert entry["window"]["breach"] is False
    assert warned.events == [] and warned.opened == []
    assert warned.state_after == "healthy"
    head = plant.store.head()
    result = CorrectionResult(accepted=True, model=model, fitted_to=at(12), error=0.1)
    assert fidelity.corrected(plant.twin, warned.corrections[0], result, now=at(12)) == {}
    assert plant.store.head() == head
    # The sum passes 1.0 again seven origins later, inside the window: no second warning.
    assert sums[19] == pytest.approx(1.12)
    assert all(outcomes[i].corrections == [] for i in range(13, 21))
    assert fidelity.incidents(plant.twin) == []
