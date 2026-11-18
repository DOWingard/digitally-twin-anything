"""A series called biased by the question that would infer it is flagged only when
every other predictive question on its property bears the alarm out, on the same side.

The plant has two questions on Gauge.level: `level` (2 steps ahead, the one that would
infer the level) and `level_far` (4 steps ahead). Both models end their data at step 4,
so a check at step c scores origin c - 2 of `level` and c - 4 of `level_far`. At check
16 `level` has scored origins 4 to 14 and `level_far` 4 to 12: both test every series.
G3 is predicted 0.3 too high by `level` from origin 14 on (z = -6 at origin 14, the
lower sum 5.5 > 5); each test below chooses what `level_far` predicts.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import pytest
from dta_runtime import fidelity
from dta_runtime.actions import gate
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import LEVEL_MODELS, Oracle, at, gauge, level, question, spec

G3 = gauge("G3")
LEVEL_G3 = signal_uid("Gauge.level", G3)
NEAR = "twin_model:level/naive"
FAR = "twin_model:level_far/naive"
AUTONOMOUS = {"autonomy": "autonomous", "binding": "executor"}

Offset = Callable[[Mapping[str, Any], str, datetime], float]


def two_questions(make_plant: Callable[..., Any]) -> Any:
    return make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "level_far": question("Gauge.level", 4, "level_far"),
        },
        models={**LEVEL_MODELS, "level_far": spec("level_far", "Gauge.level")},
    )


def per_model(near: float, far: float, *, far_since: int = 12) -> Offset:
    """G3 predicted `near` off by `level` from origin 14 on and `far` off by `level_far`
    from origin `far_since` on; every other series exactly."""

    def offset(model: Mapping[str, Any], uid: str, origin: datetime) -> float:
        if uid != G3:
            return 0.0
        if model["uid"] == NEAR:
            return near if origin >= at(14) else 0.0
        return far if origin >= at(far_since) else 0.0

    return offset


def checked(
    make_plant: Callable[..., Any],
    offset: Offset,
    *,
    far_fitted_to: int = 4,
    far_interventional: bool = False,
    until: int = 17,
) -> tuple[Any, dict[int, Any]]:
    plant = two_questions(make_plant)
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    plant.activate(
        "level_far",
        "Gauge.level",
        fitted_to=at(far_fitted_to),
        interventional=far_interventional,
    )
    outcomes = plant.run(range(6, until + 1), Oracle(plant, offset))
    assert all(outcomes[i].flagged == [] for i in range(6, 16))
    return plant, outcomes


def observed(plant: Any, uid: str, at_version: Any = "head") -> tuple[float, str]:
    rows = plant.store.table(
        "Gauge", columns=["level"], filter=[("uid", "=", uid)], at=at_version, prov=True
    ).to_pylist()
    provenance = json.loads(rows[0]["_prov"])
    entry = provenance.get("level") or provenance["*"]
    return rows[0]["level"], entry["k"]


def test_both_questions_alarming_on_the_same_side_flag_the_series(make_plant):
    plant, outcomes = checked(make_plant, per_model(0.3, 0.3))
    flagged = outcomes[16]
    assert [(found["object"], found["reason"]) for found in flagged.flagged] == [(G3, "biased")]
    signals = flagged.check["signals"]
    assert signals["uncorroborated"] == []
    assert signals["alarming"]["level"] == {"tested": 4, "alarming": 1, "biased": [G3]}
    assert signals["alarming"]["level_far"] == {"tested": 4, "alarming": 1, "biased": [G3]}
    signal = fidelity.signal(plant.twin, LEVEL_G3)
    assert (signal["status"], signal["reason"]) == ("suspect", "biased")
    assert signal["detail"]["question"] == "level"
    assert signal["detail"]["test"]["s"] == pytest.approx(-5.5)
    assert [found["subject"] for found in flagged.opened] == [LEVEL_G3]
    # Inferred as before: the forecast of `level`, the question with the shorter horizon.
    value, kind = observed(plant, G3, flagged.version)
    assert value == pytest.approx(level(G3, 16) + 0.3) and kind == "inferred"
    assert [entry["question"] for entry in signals["inferred"]] == ["level"]
    assert outcomes[17].check["questions"]["level"]["left_out"] == [G3]


def uncorroborated_only(outcome: Any, why: str) -> None:
    signals = outcome.check["signals"]
    assert signals["alarming"]["level"]["biased"] == [G3]
    assert signals["uncorroborated"] == [
        {
            "signal": LEVEL_G3,
            "property": "Gauge.level",
            "object": G3,
            "question": "level",
            "by": "level_far",
            "why": why,
        }
    ]
    assert outcome.flagged == [] and outcome.opened == []


def nothing_withheld(plant: Any, outcomes: dict[int, Any]) -> None:
    assert fidelity.signals(plant.twin) == []
    assert fidelity.suspects(plant.twin) == set()
    assert fidelity.incidents(plant.twin, kind="signal") == []
    value, kind = observed(plant, G3)
    assert value == level(G3, max(outcomes)) and kind == "observed"
    for outcome in outcomes.values():
        assert outcome.check["questions"]["level"].get("left_out", []) == []


def test_a_series_the_other_question_tested_and_found_sound_is_not_biased(make_plant):
    plant, outcomes = checked(make_plant, per_model(0.3, 0.0))
    uncorroborated_only(outcomes[16], "no_alarm")
    uncorroborated_only(outcomes[17], "no_alarm")
    nothing_withheld(plant, outcomes)


def test_a_series_the_other_question_could_not_test_yet_is_not_biased(make_plant):
    # `level_far` ends its data at step 10: at check 16 it has scored origins 10 to 12.
    plant, outcomes = checked(make_plant, per_model(0.3, 0.3, far_since=10), far_fitted_to=10)
    assert outcomes[16].check["signals"]["alarming"]["level_far"]["tested"] == 0
    uncorroborated_only(outcomes[16], "not_tested")
    nothing_withheld(plant, outcomes)


def test_a_series_the_other_question_finds_off_the_other_way_is_not_biased(make_plant):
    plant, outcomes = checked(make_plant, per_model(0.3, -0.3))
    assert outcomes[16].check["signals"]["alarming"]["level_far"]["biased"] == [G3]
    uncorroborated_only(outcomes[16], "other_side")
    nothing_withheld(plant, outcomes)


def test_a_control_moving_one_series_for_the_model_that_does_not_see_it_flags_nothing(
    make_plant,
):
    # The pump switched at step 14 and moved G3's level: `level`, whose model does not
    # see the pump, is 1.0 off for G3 from then on; `level_far`, whose model sees it
    # (interventional), stays exact.
    plant, outcomes = checked(make_plant, per_model(1.0, 0.0), far_interventional=True, until=24)
    for i in range(16, 25):
        assert outcomes[i].check["signals"]["alarming"]["level"]["biased"] == [G3], i
        assert {entry["why"] for entry in outcomes[i].check["signals"]["uncorroborated"]} == {
            "no_alarm"
        }, i
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    nothing_withheld(plant, outcomes)

    # Whatever `level`'s window error comes to, no incident stops an autonomous action.
    standing = fidelity.standing(plant.twin)
    assert standing["state"] in ("healthy", "correcting")
    assert {found["kind"] for found in standing["incidents"]} <= {"breach"}
    unresolved = [{"kind": found["kind"], "uid": found["uid"]} for found in standing["incidents"]]
    decision = gate.on_proposal(
        AUTONOMOUS, has_executor=True, state=standing["state"], incidents=unresolved
    )
    assert decision == gate.EXECUTE
