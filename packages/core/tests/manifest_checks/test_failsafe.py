"""What a control sends when the twin fails: chosen by the user, and checked to be
something the twin can send with constants only."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from dta_core.manifest import gaps, parse_manifest, required_fields
from dta_core.manifest.checks import ontology_findings, render_summary, run_checks
from dta_core.ontology import load_ontology, type_signature

ONTOLOGY = load_ontology(
    Path(__file__).resolve().parents[1] / "ontology/fixtures/water_network.yaml"
)
SIGNATURE = type_signature(ONTOLOGY)
MISSING = "control.on_failure_missing"
INVALID = "control.on_failure_invalid"
PUMP = "contract.controls.pump_speed.on_failure"


def executor() -> dict[str, Any]:
    return {"kind": "http", "url": "http://plant/controls/{id}"}


BASE: dict[str, Any] = {
    "manifest_version": 2,
    "twin": {"id": "water", "name": "Water"},
    "contract": {
        "tier": "live",
        "addons": {"executors": True},
        "controls": {
            "pump_speed": {
                "target": "Pump.speed",
                "bounds": {"minimum": 0.5, "maximum": 1.0},
                "autonomy": "autonomous",
                "action": "set_speed",
                "on_failure": {"mode": "hold"},
            },
            "valve_setting": {
                "target": "Valve.setting",
                "bounds": {"minimum": 1.0, "maximum": 200.0},
                "autonomy": "supervised",
                "action": "set_valve",
            },
            "pump_advice": {
                "target": "Pump.speed",
                "bounds": {"minimum": 0.5, "maximum": 1.0},
                "autonomy": "advisory",
                "action": "advise_speed",
            },
        },
    },
    "ontology": {"path": "ontology/water_network.yaml", "type_signature": SIGNATURE},
    "actions": {
        "set_speed": {
            "control": "pump_speed",
            "binding": "executor",
            "executor": executor(),
            "parameters": {"value": {"type": "number"}},
        },
        "nudge_speed": {"control": "pump_speed", "binding": "executor", "executor": executor()},
        "release_pump": {
            "object_type": "Pump",
            "binding": "executor",
            "executor": executor(),
            "parameters": {"mode": {"type": "string", "allowed": ["local", "remote"]}},
            "precondition": "status:eq:open",
        },
        "release_link": {"object_type": "Link", "binding": "executor", "executor": executor()},
        "release_valve": {"object_type": "Valve", "binding": "executor", "executor": executor()},
        "note_pump": {"object_type": "Pump", "binding": "advisory"},
        "set_valve": {
            "control": "valve_setting",
            "binding": "executor",
            "executor": executor(),
            "parameters": {"value": {"type": "number"}},
        },
        "advise_speed": {"control": "pump_advice", "binding": "advisory"},
    },
}


def manifest(**on_failure: Any) -> dict[str, Any]:
    """The base with the pump's on_failure set to what is given (the control kept by name)."""
    document = copy.deepcopy(BASE)
    controls = document["contract"]["controls"]
    for cid, value in on_failure.items():
        if value is None:
            controls[cid].pop("on_failure", None)
        else:
            controls[cid]["on_failure"] = value
    return document


def failsafe_findings(document: dict[str, Any]) -> list[tuple[str, str]]:
    found = run_checks(parse_manifest(document))
    return [(f.code, f.path) for f in found if f.code in (MISSING, INVALID)]


ACCEPTED = {
    "hold": {"pump_speed": {"mode": "hold"}},
    "release": {
        "pump_speed": {"mode": "release", "action": "release_pump", "parameters": {"mode": "local"}}
    },
    "release_after_stale": {
        "pump_speed": {
            "mode": "release",
            "action": "release_pump",
            "parameters": {"mode": "remote"},
            "after_stale": "PT2H",
        }
    },
    "safe_value_own_action": {"pump_speed": {"mode": "safe_value", "parameters": {"value": 0.5}}},
    "safe_value_other_action": {
        "pump_speed": {"mode": "safe_value", "action": "nudge_speed", "parameters": {"value": 0.6}}
    },
    "supervised_safe_value": {"valve_setting": {"mode": "safe_value", "parameters": {"value": 10}}},
    "supervised_unset": {"valve_setting": None},
    "advisory_hold": {"pump_advice": {"mode": "hold"}},
}  # fmt: skip


@pytest.mark.parametrize("change", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_fail_safe_the_twin_can_send_is_accepted(change: dict[str, Any]):
    document = manifest(**change)
    assert failsafe_findings(document) == []
    found = ontology_findings(parse_manifest(document), ONTOLOGY, SIGNATURE)
    assert [f for f in found if f.code == INVALID] == []


def test_an_autonomous_control_that_calls_the_real_system_must_choose():
    document = manifest(pump_speed=None)
    assert failsafe_findings(document) == [(MISSING, PUMP)]
    # It is a gap for the user as well, settled in discussion like the autonomy.
    gap = next(state for state in gaps(parse_manifest(document)) if state.path == PUMP)
    assert (gap.owner.value, gap.status.value) == ("user", "unknown")
    assert "not a safety function" in gap.description
    # A supervised or an advisory control may leave it out: the twin only acts when told.
    paths = {state.path for state in required_fields(parse_manifest(document))}
    assert "contract.controls.valve_setting.on_failure" not in paths
    assert "contract.controls.pump_advice.on_failure" not in paths


def test_a_chosen_mode_is_the_user_s_to_confirm():
    document = manifest(pump_speed={"mode": "safe_value", "parameters": {"value": 0.5}})
    state = next(
        state for state in required_fields(parse_manifest(document)) if state.path == f"{PUMP}.mode"
    )
    assert (state.owner.value, state.status.value) == ("user", "proposed")


def test_a_version_1_manifest_cannot_choose_and_is_told_why():
    document = manifest(pump_speed=None)
    document["manifest_version"] = 1
    found = run_checks(parse_manifest(document))
    (missing,) = [f for f in found if f.code == MISSING]
    assert "manifest_version 2" in missing.message


def test_an_autonomous_control_without_an_executor_is_reported_elsewhere():
    document = manifest(pump_speed=None)
    document["actions"]["set_speed"]["binding"] = "advisory"
    del document["actions"]["set_speed"]["executor"]
    found = run_checks(parse_manifest(document))
    assert [f.code for f in found if f.code in (MISSING, INVALID)] == []
    assert "control.binding" in {f.code for f in found}


REFUSED = [
    ("hold_with_action", {"mode": "hold", "action": "release_pump"}, f"{PUMP}.action"),
    ("hold_with_parameters", {"mode": "hold", "parameters": {"value": 1}}, f"{PUMP}.parameters"),
    ("hold_with_after_stale", {"mode": "hold", "after_stale": "PT1H"}, f"{PUMP}.after_stale"),
    ("release_without_action", {"mode": "release"}, f"{PUMP}.action"),
    ("release_own_action", {"mode": "release", "action": "set_speed", "parameters": {"value": 0.5}},
     f"{PUMP}.action"),
    ("release_undeclared", {"mode": "release", "action": "nothing"}, f"{PUMP}.action"),
    ("release_advisory_binding", {"mode": "release", "action": "note_pump"}, f"{PUMP}.action"),
    ("parameter_left_open", {"mode": "release", "action": "release_pump"}, f"{PUMP}.parameters"),
    ("parameter_unknown", {"mode": "release", "action": "release_link", "parameters": {"x": 1}},
     f"{PUMP}.parameters.x"),
    ("parameter_type", {"mode": "release", "action": "release_pump", "parameters": {"mode": 3}},
     f"{PUMP}.parameters.mode"),
    ("parameter_not_allowed", {"mode": "release", "action": "release_pump",
                               "parameters": {"mode": "off"}}, f"{PUMP}.parameters.mode"),
    ("safe_value_missing", {"mode": "safe_value"}, f"{PUMP}.parameters"),
    ("safe_value_out_of_bounds", {"mode": "safe_value", "parameters": {"value": 3.0}},
     f"{PUMP}.parameters.value"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("on_failure", "path"), [case[1:] for case in REFUSED], ids=[case[0] for case in REFUSED]
)
def test_a_fail_safe_the_twin_cannot_send_is_refused(on_failure: dict[str, Any], path: str):
    assert failsafe_findings(manifest(pump_speed=on_failure)) == [(INVALID, path)]


@pytest.mark.parametrize("mode", ["release", "safe_value"])
def test_an_advisory_control_has_nothing_to_release_or_set(mode: str):
    on_failure = {"mode": mode, "action": "release_pump", "parameters": {"mode": "local"}}
    found = failsafe_findings(manifest(pump_advice=on_failure))
    assert found == [(INVALID, "contract.controls.pump_advice.on_failure.mode")]


def test_the_action_must_act_on_what_the_control_sets():
    """Judged against the ontology: an action on a supertype covers the control's type."""
    wrong = manifest(pump_speed={"mode": "release", "action": "release_valve"})
    assert failsafe_findings(wrong) == []
    found = ontology_findings(parse_manifest(wrong), ONTOLOGY, SIGNATURE)
    assert [(f.code, f.path) for f in found if f.code == INVALID] == [(INVALID, f"{PUMP}.action")]
    assert "does not cover Pump" in next(f.message for f in found if f.code == INVALID)
    wider = manifest(pump_speed={"mode": "release", "action": "release_link"})
    found = ontology_findings(parse_manifest(wider), ONTOLOGY, SIGNATURE)
    assert [f for f in found if f.code == INVALID] == []


def test_a_safe_value_must_fit_the_property_it_sets():
    document = manifest(
        pump_speed={"mode": "safe_value", "action": "nudge_speed", "parameters": {"value": "fast"}}
    )
    # Bounds that allow the text, so that only the property's kind refuses it.
    document["contract"]["controls"]["pump_speed"]["bounds"] = {"allowed": ["fast", "slow"]}
    assert failsafe_findings(document) == []
    found = ontology_findings(parse_manifest(document), ONTOLOGY, SIGNATURE)
    assert [(f.code, f.path) for f in found if f.code == INVALID] == [
        (INVALID, f"{PUMP}.parameters.value")
    ]


def test_the_summary_says_each_choice_in_words():
    document = manifest(
        pump_speed={
            "mode": "release",
            "action": "release_pump",
            "parameters": {"mode": "local"},
            "after_stale": "PT2H",
        },
        valve_setting={"mode": "safe_value", "parameters": {"value": 10}},
    )
    lines = render_summary(parse_manifest(document)).splitlines()
    assert (
        "| pump_speed | release: `release_pump` with mode = local hands each object the twin "
        "commanded back, once, when its fidelity becomes failed, or once data has been too old "
        "for 2 h |"
    ) in lines
    assert (
        "| valve_setting | safe value: `set_valve` with value = 10 is sent to each object the "
        "twin commanded, once, when its fidelity becomes failed |"
    ) in lines
    assert "| pump_advice | not chosen: nothing is sent |" in lines
    assert any(line.startswith("This is not a safety function") for line in lines)
    held = render_summary(parse_manifest(manifest(pump_speed={"mode": "hold"}))).splitlines()
    assert (
        "| pump_speed | hold: nothing is sent; the real system keeps the twin's last command |"
        in held
    )
