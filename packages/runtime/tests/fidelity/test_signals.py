"""Stuck and biased signals over a real store: when they are flagged, what is written
in their place, how they are cleared.

Stuck: the level of G1 and the flow of G2 read the value of step 21 at steps 22 to 29.
The lookback is 96 intervals, longer than the plant's history: at check c it holds every
step from 1. Both changed in each of the 21 intervals up to step 21 (k = 21, u = 0,
p0 = 1/23); their changes do not keep their sign (the level steps up three times and
down once), so they are not smooth, and with no two intervals without a change in a row
the intervals are independent draws: after n intervals without a change P = p0^n. At
check 26 (n = 5) P = 1.5e-7, not below 1e-7; at check 27 (n = 6) P = 6.7e-9.
"""

from __future__ import annotations

import json

import pytest
from dta_runtime import fidelity
from dta_runtime.fidelity import FidelityError
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import (
    LEVEL_MODELS,
    Oracle,
    at,
    flow,
    gauge,
    kinds,
    level,
    question,
    shifted,
    spec,
)

INCIDENT = "twin.fidelity.incident"
STATE = "twin.fidelity.state"
UNKNOWN = "twin.fidelity.signal_unknown"
G1, G2, G3 = gauge("G1"), gauge("G2"), gauge("G3")
LEVEL_G1 = signal_uid("Gauge.level", G1)
FLOW_G2 = signal_uid("Gauge.flow", G2)


def stored(plant, type_name, name, uid, at="head"):
    """The value of one property of one object and its provenance as stored."""
    rows = plant.store.table(
        type_name, columns=[name], filter=[("uid", "=", uid)], at=at, prov=True
    )
    row = rows.to_pylist()[0]
    provenance = json.loads(row["_prov"])
    return row[name], provenance.get(name) or provenance["*"]


def far_off(model, uid, origin):
    # The longer question predicts a little off, so that its values can be told apart.
    return 0.01 if model["uid"] == "twin_model:level_far/naive" else 0.0


def test_stuck_signals_are_flagged_inferred_then_unknown_and_cleared(make_plant):
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "level_far": question("Gauge.level", 4, "level_far"),
        },
        models={**LEVEL_MODELS, "level_far": spec("level_far", "Gauge.level")},
    )
    stuck_steps = range(22, 30)
    plant.readings[("level", G1)] = lambda i: level(G1, 21) if i in stuck_steps else level(G1, i)
    plant.readings[("flow", G2)] = lambda i: flow(G2, 21) if i in stuck_steps else flow(G2, i)
    for i in range(1, 20):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(18))
    plant.activate("level_far", "Gauge.level", fitted_to=at(18))
    oracle = Oracle(plant, far_off)
    plant.check(19, oracle)
    early = plant.run(range(20, 27), oracle)
    assert all(outcome.flagged == [] for outcome in early.values())
    assert early[26].check["signals"]["tested"]["stuck"] > 0

    flagged = plant.step(27, oracle)
    assert sorted(found["uid"] for found in flagged.flagged) == sorted([LEVEL_G1, FLOW_G2])
    assert {found["reason"] for found in flagged.flagged} == {"stuck"}
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    assert (signal["property"], signal["object"], signal["status"]) == (
        "Gauge.level",
        G1,
        "suspect",
    )
    assert signal["value_at_flag"] == level(G1, 21) and signal["since"] == at(27)
    test = signal["detail"]["test"]
    assert (test["k"], test["u"], test["n"], test["model"]) == (21, 0, 6, "independent")
    assert test["p0"] == pytest.approx(1 / 23)
    assert test["p"] == pytest.approx((1 / 23) ** 6)
    assert test["needed"] == 6 and test["alpha"] == 1e-7
    subjects = sorted([LEVEL_G1, FLOW_G2])
    assert [found["subject"] for found in flagged.opened] == subjects
    # No question predicts the flow: nothing stands in for it, and its value is unknown
    # from the check that flagged it.
    assert kinds(flagged.events) == [
        (INCIDENT, "opened"),
        (INCIDENT, "opened"),
        (UNKNOWN, None),
        (STATE, "correcting"),
    ]
    assert [event["data"]["kind"] for event in flagged.events[:2]] == ["signal", "signal"]
    assert flagged.events[2]["data"]["signal"] == FLOW_G2
    assert flagged.events[2]["data"]["why"] == "no_inference"
    assert fidelity.suspects(plant.twin) == {("Gauge.level", G1), ("Gauge.flow", G2)}

    # The level is inferred from the forecast the question with the shortest horizon
    # made from the version of step 26, the reading before the flagged one: lead 1.
    inference = signal["detail"]["inference"]
    assert (inference["question"], inference["origin"], inference["horizon_steps"]) == (
        "level",
        "2026-01-05T06:30:00Z",
        2,
    )
    assert oracle.calls[-1]["origin"] == at(26) and oracle.calls[-1]["horizon"] == 2
    value, provenance = stored(plant, "Gauge", "level", G1, at=flagged.version)
    assert value == level(G1, 27)
    assert (provenance["k"], provenance["s"]) == ("inferred", model)
    assert provenance["t"].startswith("2026-01-05T06:45:00")
    inferred = flagged.check["signals"]["inferred"]
    assert [(entry["signal"], entry["question"], entry["lead_steps"]) for entry in inferred] == [
        (LEVEL_G1, "level", 1)
    ]
    value, provenance = stored(plant, "Gauge", "flow", G2, at=flagged.version)
    assert value is None
    assert (provenance["k"], provenance["s"], provenance["c"]) == ("inferred", FLOW_G2, 0.0)

    later = plant.step(28, oracle)
    entries = later.check["questions"]
    assert entries["level"]["left_out"] == [G1] and entries["level_far"]["left_out"] == [G1]
    assert G1 not in entries["level"]["series"]
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    # What the source read is kept on the signal, not written to the gauge.
    assert (signal["reading"], signal["reading_at"]) == (level(G1, 21), at(28))
    # Lead 2, the question's horizon: the forecast made at the flag, not a new one.
    assert stored(plant, "Gauge", "level", G1, at=later.version)[0] == level(G1, 28)
    assert later.check["signals"]["inferred"][0]["lead_steps"] == 2
    assert later.events == []

    # Lead 3 is past the horizon the question was validated for: the value is unknown.
    passed = plant.step(29, oracle)
    assert kinds(passed.events) == [(UNKNOWN, None)]
    data = passed.events[0]["data"]
    assert (data["signal"], data["why"], data["questions"]) == (
        LEVEL_G1,
        "horizon_passed",
        ["level", "level_far"],
    )
    value, provenance = stored(plant, "Gauge", "level", G1, at=passed.version)
    assert value is None and provenance["s"] == LEVEL_G1
    incident = next(
        found for found in fidelity.incidents(plant.twin) if found["subject"] == LEVEL_G1
    )
    assert incident["status"] == "open"
    assert incident["detail"]["unanswered"] == ["level", "level_far"]
    assert incident["steps"][-1]["action"] == "unknown"
    assert incident["steps"][-1]["why"] == "horizon_passed"
    assert fidelity.state(plant.twin)["state"] == "correcting"
    # What the interlock and the readers of values are handed: G2's flow has been unknown
    # since its flag, no question inferring it.
    standing = fidelity.standing(plant.twin)
    withheld = [found["withheld"] for found in standing["incidents"] if "withheld" in found]
    assert sorted(withheld, key=lambda cell: cell["property"]) == [
        {"signal": FLOW_G2, "property": "Gauge.flow", "object": G2},
        {"signal": LEVEL_G1, "property": "Gauge.level", "object": G1},
    ]
    unknown = fidelity.unknown(plant.twin)
    assert sorted(unknown) == [("Gauge.flow", G2), ("Gauge.level", G1)]
    assert fidelity.withheld(unknown, ["Gauge.level"], uids=[G1])[0]["why"] == "horizon_passed"
    assert fidelity.withheld(unknown, ["Gauge.flow"])[0]["why"] == "no_inference"
    assert fidelity.withheld(unknown, ["Gauge.level"], uids=[G2]) == []
    cleared_by_hand = fidelity.clear_signal(
        plant.twin, FLOW_G2, by="operator", note="sensor cleaned", now=at(29)
    )
    assert cleared_by_hand["status"] == "ok"
    assert kinds(cleared_by_hand["events"]) == [(INCIDENT, "resolved")]
    with pytest.raises(FidelityError) as refused:
        fidelity.clear_signal(plant.twin, FLOW_G2, by="operator", note=None, now=at(29))
    assert refused.value.code == "fidelity.conflict"

    # Unknown it stays, said once; nothing reading it is scored. The raw readings move
    # again from step 30: changes in 3 intervals since the flag by step 32.
    still = plant.run(range(30, 32), oracle)
    assert all(outcome.cleared == [] and outcome.events == [] for outcome in still.values())
    assert still[31].check["questions"]["level"]["reason"]["code"] == "input_unknown"
    assert still[31].check["questions"]["level"]["withheld"] == [LEVEL_G1]
    assert stored(plant, "Gauge", "level", G1, at=still[31].version)[0] is None
    cleared = plant.step(32, oracle)
    assert [found["uid"] for found in cleared.cleared] == [LEVEL_G1]
    assert kinds(cleared.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    assert fidelity.suspects(plant.twin) == set()
    episodes = fidelity.signal(plant.twin, LEVEL_G1)["detail"]["episodes"]
    assert episodes == [
        {
            "since": "2026-01-05T06:45:00Z",
            "until": "2026-01-05T08:00:00Z",
            "reason": "stuck",
            "cleared_by": "check",
            "unknown_since": "2026-01-05T07:15:00Z",
        }
    ]
    steps = fidelity.incident(plant.twin, incident["uid"])["steps"]
    assert steps[-1]["action"] == "cleared"
    assert (steps[-1]["changes"], steps[-1]["run"]) == (3, 0)
    assert fidelity.signal(plant.twin, FLOW_G2)["detail"]["episodes"][0]["cleared_by"] == "operator"
    # Synced again once cleared, and not flagged again: the test reads the raw readings
    # in place of what the check wrote while it was suspect.
    after = plant.run(range(33, 40), oracle)
    assert all(outcome.flagged == [] for outcome in after.values())
    assert stored(plant, "Gauge", "level", G1)[0] == level(G1, 39)
    # The pump's status seldom changes and is never called stuck.
    assert {found["property"] for found in fidelity.signals(plant.twin)} == {
        "Gauge.level",
        "Gauge.flow",
    }
    assert fidelity.incidents(plant.twin, status="unresolved") == []


def biased_plant(make_plant, shifted_uids):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    # Origins 4 to 13 are predicted exactly; from origin 14 on the chosen series are
    # predicted 1.8 too high. Baseline median 0, MAD 0: sigma is the floor 0.05 and
    # z = -36 at origin 14, scored at check 16: the lower sum is 35.5 > 5, and the
    # newest three depart by 0.6 on average, above the tolerance 0.5.
    oracle = Oracle(plant, shifted(1.8, since=at(14), uids=shifted_uids))
    return plant, model, plant.run(range(6, 18), oracle)


def test_a_series_off_on_its_own_is_biased(make_plant):
    plant, model, outcomes = biased_plant(make_plant, [G3])
    assert all(outcomes[i].flagged == [] for i in range(6, 16))
    flagged = outcomes[16]
    assert [(found["object"], found["reason"]) for found in flagged.flagged] == [(G3, "biased")]
    assert flagged.check["signals"]["alarming"]["level"] == {
        "tested": 4,
        "alarming": 1,
        "biased": [G3],
    }
    signal = fidelity.signal(plant.twin, signal_uid("Gauge.level", G3))
    assert signal["value_at_flag"] == level(G3, 16)
    assert signal["detail"]["question"] == "level"
    assert signal["detail"]["test"]["z"] == pytest.approx([0.0, 0.0, -36.0])
    assert signal["detail"]["test"]["s"] == pytest.approx(-35.5)
    value, provenance = stored(plant, "Gauge", "level", G3, at=flagged.version)
    assert value == pytest.approx(level(G3, 16) + 1.8)
    assert (provenance["k"], provenance["s"]) == ("inferred", model)
    assert outcomes[17].check["questions"]["level"]["left_out"] == [G3]


def test_most_series_off_together_is_the_model_and_flags_none(make_plant):
    plant, model, outcomes = biased_plant(make_plant, [G1, G2, G3])
    assert outcomes[16].check["signals"]["alarming"]["level"] == {
        "tested": 4,
        "alarming": 3,
        "biased": [],
    }
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    assert fidelity.signals(plant.twin) == []
