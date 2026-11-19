"""What stands in for a suspect signal, over a real store: forecast again from the
newest origin at which every input of the inferring model was observed when that model
does not read the signal, forecast once when it does; and what no score rests on.

G3's level sensor reads 2 m high from step 10 to 29. `level` (2 steps ahead) has a
model of the shipped persistence family, which reads the level's own history;
`level_far` (4 steps ahead), in the tests that have it, has code of the twin's own that
declares the flow as its only input.
"""

from __future__ import annotations

from typing import Any

from dta_runtime import fidelity
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import (
    LEVEL_MODELS,
    Oracle,
    at,
    flow,
    gauge,
    level,
    question,
    spec,
)

G1, G2, G3 = gauge("G1"), gauge("G2"), gauge("G3")
LEVEL_G1 = signal_uid("Gauge.level", G1)
LEVEL_G3 = signal_uid("Gauge.level", G3)
FLOW_G2 = signal_uid("Gauge.flow", G2)
CODE = "plant.hydraulic:Model"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def stamp(step: int) -> str:
    return at(step).strftime(STAMP)


def biased_g3(start: int, end: int):
    """G3's level read 2 m high from `start` to before `end`."""
    return lambda i: level(G3, i) + 2.0 if start <= i < end else level(G3, i)


def code_model(plant, uid: str, features: list[str]) -> None:
    plant.register({"TwinModel": [{"uid": uid, "family": CODE, "features": features}]})


def two_questions(make_plant, end: int):
    """Both questions on the level, G3 biased from 20 (both have a clean baseline of
    origins by then, and bear each other out) to before `end`."""
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "level_far": question("Gauge.level", 4, "level_far"),
        },
        models={**LEVEL_MODELS, "level_far": spec("level_far", "Gauge.level")},
    )
    plant.readings[("level", G3)] = biased_g3(20, end)
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    far = plant.activate("level_far", "Gauge.level", fitted_to=at(4))
    code_model(plant, far, ["Gauge.flow"])
    return plant


def flag_step(outcomes: dict[int, Any], signal: str) -> int:
    (step,) = [
        i
        for i, outcome in outcomes.items()
        if signal in [found["uid"] for found in outcome.flagged]
    ]
    return step


def origins_of(outcomes: dict[int, Any], signal: str) -> dict[int, str]:
    """The origin of the forecast in force for a signal at each check that inferred it."""
    return {
        i: entry["origin"]
        for i, outcome in outcomes.items()
        for entry in outcome.check["signals"]["inferred"]
        if entry["signal"] == signal
    }


def test_a_model_that_does_not_read_the_signal_keeps_it_fresh_and_it_clears(make_plant):
    plant = two_questions(make_plant, 40)
    outcomes = plant.run(range(6, 46), Oracle(plant))
    flagged = flag_step(outcomes, LEVEL_G3)
    # The code model does not read the level: it infers it although `level` has the
    # shorter horizon, and forecasts it again at every check from the step before.
    inference = outcomes[flagged].check["signals"]["inferred"][0]
    assert (inference["question"], inference["origin"]) == ("level_far", stamp(flagged - 1))
    chosen = fidelity.signal(plant.twin, LEVEL_G3)["detail"]["inference"]["chosen"]
    assert chosen == {"question": "level_far", "why": "does_not_read_it"}
    # Sound again from step 40, the readings agree with the fresh reference at 40, 41
    # and 42: cleared there, long after the first forecast's horizon of 4 steps.
    assert flagged + 4 < 40
    assert [i for i, outcome in outcomes.items() if outcome.cleared] == [42]
    assert fidelity.signal(plant.twin, LEVEL_G3)["status"] == "ok"
    origins = origins_of(outcomes, LEVEL_G3)
    assert sorted(origins) == list(range(flagged, 42))
    assert all(origin == stamp(i - 1) for i, origin in origins.items())


def test_a_model_that_reads_the_signal_forecasts_it_once_and_an_operator_clears_it(
    make_plant,
):
    plant = make_plant()
    plant.readings[("level", G3)] = biased_g3(10, 30)
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    outcomes = plant.run(range(6, 36), Oracle(plant))
    flagged = flag_step(outcomes, LEVEL_G3)
    detail = fidelity.signal(plant.twin, LEVEL_G3)["detail"]
    assert detail["inference"]["chosen"] == {"question": "level", "why": "reads_it_longest_horizon"}
    # Forecast once, from the step before the flag; unknown once the lead passes 2.
    assert set(origins_of(outcomes, LEVEL_G3).values()) == {stamp(flagged - 1)}
    assert sorted(origins_of(outcomes, LEVEL_G3)) == [flagged, flagged + 1]
    unknown = outcomes[flagged + 2].check["signals"]["unknown"]
    assert [(entry["signal"], entry["why"]) for entry in unknown] == [(LEVEL_G3, "horizon_passed")]
    # Nothing to compare the sound readings with: no check clears it.
    assert all(outcome.cleared == [] for outcome in outcomes.values())
    assert fidelity.signal(plant.twin, LEVEL_G3)["status"] == "suspect"
    cleared = fidelity.clear_signal(
        plant.twin, LEVEL_G3, by="operator", note="sensor recalibrated", now=at(35)
    )
    assert cleared["status"] == "ok"


def test_a_fresh_forecast_never_rests_on_an_input_not_observed(make_plant):
    # The flow of G2, the code model's input, sticks from step 30 (its last change at
    # 29) and is flagged at check F; no question infers it, so it is unknown from then on.
    plant = two_questions(make_plant, 60)
    plant.readings[("flow", G2)] = lambda i: flow(G2, 29) if i >= 30 else flow(G2, i)
    outcomes = plant.run(range(6, 45), Oracle(plant))
    stuck = flag_step(outcomes, FLOW_G2)
    assert flag_step(outcomes, LEVEL_G3) < stuck - 2
    assert fidelity.signal(plant.twin, FLOW_G2)["detail"]["episodes"][-1]["run_since"] == stamp(29)
    origins = origins_of(outcomes, LEVEL_G3)
    # Fresh up to the check before F (from F - 2, a frozen flow nobody knew of yet); from F
    # every newer origin holds the flow frozen or unknown: the forecast from F - 2 stays,
    # its lead grows, and past 4 steps the value is unknown.
    assert origins[stuck - 1] == stamp(stuck - 2)
    assert [origins[i] for i in (stuck, stuck + 1, stuck + 2)] == [stamp(stuck - 2)] * 3
    assert stuck + 3 not in origins
    unknown = outcomes[stuck + 3].check["signals"]["unknown"]
    assert [(entry["signal"], entry["why"]) for entry in unknown] == [(LEVEL_G3, "horizon_passed")]


def test_an_origin_with_an_inferred_feature_is_not_scored_and_opens_no_breach(make_plant):
    # The level's model reads the flow; the flow's own model reads nothing (code), so a
    # suspect flow is inferred afresh at every check. The level model is far off from
    # origin 27 on; G2's flow, frozen from 22, is flagged at 26 (its readings stray from
    # the flow question's forecast): no score, no breach rests on the flow inferred.
    plant = make_plant(
        questions={
            "level": question("Gauge.level", 2, "level"),
            "flow": question("Gauge.flow", 1, "flow"),
        },
        models={**LEVEL_MODELS, "flow": spec("flow", "Gauge.flow")},
    )
    plant.readings[("flow", G2)] = lambda i: flow(G2, 21) if i >= 22 else flow(G2, i)
    for i in range(1, 20):
        plant.sync(i)
    level_model = plant.activate("level", "Gauge.level", fitted_to=at(18))
    plant.register({"TwinModel": [{"uid": level_model, "features": ["Gauge.flow"]}]})
    code_model(plant, plant.activate("flow", "Gauge.flow", fitted_to=at(18)), [])

    def off(model, uid, origin):
        return 3.0 if model["uid"] == level_model and origin >= at(27) else 0.0

    outcomes = plant.run(range(19, 40), Oracle(plant, off))
    assert flag_step(outcomes, FLOW_G2) == 26
    assert set(origins_of(outcomes, FLOW_G2)) == set(range(26, 40))
    for i in range(29, 40):
        entry = outcomes[i].check["questions"]["level"]
        assert (entry["scored"], entry["reason"]["code"], entry["withheld"]) == (
            False,
            "input_inferred",
            [FLOW_G2],
        )
    assert not any(
        found["kind"] == "breach" for found in fidelity.incidents(plant.twin, status="unresolved")
    )


def test_origins_in_the_run_before_a_stuck_flag_leave_the_window_and_open_no_breach(
    make_plant,
):
    # The level's model reads the flow. G2's flow sticks from step 22 (its last change at
    # 21) and is flagged at 27. The model is 10 m off at origin 25 alone, scored at the
    # flag: with the seven good origins from 18 the window error would be 10 / 8, past the
    # tolerance of 0.5. The flag takes origins 22 to 25 out: they read a frozen flow.
    plant = make_plant()
    plant.readings[("flow", G2)] = lambda i: flow(G2, 21) if i >= 22 else flow(G2, i)
    for i in range(1, 20):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(18))
    plant.register({"TwinModel": [{"uid": model, "features": ["Gauge.flow"]}]})

    def off(found, uid, origin):
        return 10.0 if found["uid"] == model and origin == at(25) else 0.0

    outcomes = plant.run(range(19, 31), Oracle(plant, off))
    assert flag_step(outcomes, FLOW_G2) == 27
    before = outcomes[26].check["questions"]["level"]["window"]
    assert (before["n"], before["error"], before["breach"]) == (7, 0.0, False)
    assert outcomes[27].check["questions"]["level"]["error"] == 10.0
    window = outcomes[27].check["questions"]["level"]["window"]
    assert window["set_aside"] == [stamp(step) for step in range(22, 26)]
    assert (window["n"], window["error"], window["breach"]) == (4, 0.0, False)
    assert not any(
        found["kind"] == "breach" for found in fidelity.incidents(plant.twin, status="unresolved")
    )
