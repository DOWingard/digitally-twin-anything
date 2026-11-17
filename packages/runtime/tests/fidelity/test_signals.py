"""Stuck and biased signals over a real store: when they are flagged, what is written
in their place, how they are cleared.

Stuck: the level of G1 and the flow of G2 read the value of step 21 at steps 22 to 25.
At check c the lookback holds the 24 intervals ending at c, interval m holding step
c - 23 + m. At check 24 both changed in each of the 21 intervals up to step 21
(k = 21, u = 0, p0 = 1/23) and their source delivered in the 3 since:
p0^3 = 8.2e-5 < 1e-4. At check 23 only 2 had passed.
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


def test_stuck_signals_are_flagged_estimated_and_cleared(make_plant):
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "level_far": question("Gauge.level", 4, "level_far"),
        },
        models={**LEVEL_MODELS, "level_far": spec("level_far", "Gauge.level")},
    )
    stuck_steps = range(22, 26)
    plant.readings[("level", G1)] = lambda i: level(G1, 21) if i in stuck_steps else level(G1, i)
    plant.readings[("flow", G2)] = lambda i: flow(G2, 21) if i in stuck_steps else flow(G2, i)
    for i in range(1, 20):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(18))
    plant.activate("level_far", "Gauge.level", fitted_to=at(18))
    oracle = Oracle(plant, far_off)
    plant.check(19, oracle)
    early = plant.run(range(20, 24), oracle)
    assert all(outcome.flagged == [] for outcome in early.values())

    flagged = plant.step(24, oracle)
    assert sorted(found["uid"] for found in flagged.flagged) == sorted([LEVEL_G1, FLOW_G2])
    assert {found["reason"] for found in flagged.flagged} == {"stuck"}
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    assert (signal["property"], signal["object"], signal["status"]) == (
        "Gauge.level",
        G1,
        "suspect",
    )
    assert signal["value_at_flag"] == level(G1, 21) and signal["since"] == at(24)
    test = signal["detail"]["test"]
    assert (test["k"], test["u"], test["n"]) == (21, 0, 3)
    assert test["p0"] == pytest.approx(1 / 23)
    subjects = sorted([LEVEL_G1, FLOW_G2])
    assert [found["subject"] for found in flagged.opened] == subjects
    assert kinds(flagged.events) == [
        (INCIDENT, "opened"),
        (INCIDENT, "opened"),
        (STATE, "correcting"),
    ]
    assert [event["data"]["kind"] for event in flagged.events[:2]] == ["signal", "signal"]
    assert fidelity.suspects(plant.twin) == {("Gauge.level", G1), ("Gauge.flow", G2)}

    # The level is estimated by the question with the shortest horizon; the flow, which
    # no question predicts, keeps its last value.
    value, provenance = stored(plant, "Gauge", "level", G1, at=flagged.version)
    assert value == level(G1, 24)
    assert (provenance["k"], provenance["s"]) == ("inferred", model)
    assert provenance["t"].startswith("2026-01-05T06:00:00")
    inferred = flagged.check["signals"]["inferred"]
    assert [(entry["signal"], entry["question"]) for entry in inferred] == [(LEVEL_G1, "level")]
    assert stored(plant, "Gauge", "flow", G2)[0] == flow(G2, 21)

    later = plant.step(25, oracle)
    entries = later.check["questions"]
    assert entries["level"]["left_out"] == [G1] and entries["level_far"]["left_out"] == [G1]
    assert G1 not in entries["level"]["series"]
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    # What the source read is kept on the signal, not written to the gauge.
    assert (signal["reading"], signal["reading_at"]) == (level(G1, 21), at(25))
    assert signal["clear_checks"] == 0

    sound = plant.step(26, oracle)
    assert sound.cleared == []
    assert fidelity.signal(plant.twin, LEVEL_G1)["clear_checks"] == 1
    cleared_by_hand = fidelity.clear_signal(
        plant.twin, FLOW_G2, by="operator", note="sensor cleaned", now=at(26)
    )
    assert cleared_by_hand["status"] == "ok"
    assert kinds(cleared_by_hand["events"]) == [(INCIDENT, "resolved")]
    with pytest.raises(FidelityError) as refused:
        fidelity.clear_signal(plant.twin, FLOW_G2, by="operator", note=None, now=at(26))
    assert refused.value.code == "fidelity.conflict"

    assert plant.step(27, oracle).cleared == []
    cleared = plant.step(28, oracle)
    assert [found["uid"] for found in cleared.cleared] == [LEVEL_G1]
    assert kinds(cleared.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    assert fidelity.suspects(plant.twin) == set()
    episodes = fidelity.signal(plant.twin, LEVEL_G1)["detail"]["episodes"]
    assert episodes == [
        {
            "since": "2026-01-05T06:00:00Z",
            "until": "2026-01-05T07:00:00Z",
            "reason": "stuck",
            "cleared_by": "check",
        }
    ]
    assert fidelity.signal(plant.twin, FLOW_G2)["detail"]["episodes"][0]["cleared_by"] == "operator"
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
    # predicted 0.3 too high. Baseline median 0, MAD 0: sigma is the floor 0.05 and
    # z = -6 at origin 14, scored at check 16: the lower sum is 5.5 > 5.
    oracle = Oracle(plant, shifted(0.3, since=at(14), uids=shifted_uids))
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
    assert signal["detail"]["test"]["z"] == pytest.approx([0.0, 0.0, -6.0])
    assert signal["detail"]["test"]["s"] == pytest.approx(-5.5)
    value, provenance = stored(plant, "Gauge", "level", G3, at=flagged.version)
    assert value == pytest.approx(level(G3, 16) + 0.3)
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
