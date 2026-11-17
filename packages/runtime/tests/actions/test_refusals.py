"""A proposal that cannot be carried out as asked is recorded as rejected, with every
reason, and raised as `action.rejected`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from actions_testkit import BARE_VALVE, PUMP, VALVE, World, at, manifest_of, register
from dta_runtime import actions
from dta_runtime.actions import ActionError

NOW = at(2)


def refused(world: World, action: str, obj: str, params: Any, **more: Any) -> ActionError:
    options = {"reason": "test", "source": "tester", "state": "healthy", "executor": None}
    options.update(more)
    with pytest.raises(ActionError) as caught:
        actions.propose(world.twin, action, object=obj, params=params, now=NOW, **options)
    assert caught.value.code == "action.rejected"
    return caught.value


def kept(world: World, error: ActionError) -> dict[str, Any]:
    """The record of a refusal, as the head holds it, checked against what was raised."""
    uid = error.detail["record"]
    record = actions.record(world.twin, uid)
    assert record["status"] == "rejected"
    assert record["reasons"] == error.detail["reasons"]
    assert record["execution"] is None
    return record


def members(error: ActionError) -> list[str]:
    return [found["member"] for found in error.detail["reasons"]]


def test_an_unknown_action_is_refused_and_no_record_can_name_it(world: World):
    head = world.store.head()
    error = refused(world, "open_floodgate", VALVE, {"value": 1})
    assert error.detail["record"] is None
    assert members(error) == ["action"]
    assert "open_floodgate" in error.detail["reasons"][0]["message"]
    assert world.store.head() == head


def test_an_unknown_object_is_recorded_and_raised(world: World):
    error = refused(world, "set_valve", "link:VLV-404", {"value": 10.0})
    record = kept(world, error)
    assert members(error) == ["object"]
    assert record["object"] == "link:VLV-404"
    assert record["action"] == "set_valve"
    assert record["source"] == "tester"
    assert record["fidelity"] == "healthy"
    assert record["proposed_at"] == NOW
    assert record["params"] == {"value": 10.0}


def test_an_object_of_another_type_is_refused(world: World):
    error = refused(world, "set_pump", VALVE, {"value": True})
    kept(world, error)
    assert members(error) == ["object"]
    assert "Valve" in error.detail["reasons"][0]["message"]
    assert "Pump" in error.detail["reasons"][0]["message"]


@pytest.mark.parametrize(
    ("action", "obj", "params", "member", "words"),
    [
        ("set_valve", VALVE, {}, "params.value", "missing"),
        ("set_valve", VALVE, {"value": 10.0, "speed": 1}, "params.speed", "not a parameter"),
        ("set_valve", VALVE, {"value": "ten"}, "params.value", "must be a number"),
        ("set_valve", VALVE, {"value": True}, "params.value", "must be a number"),
        ("set_valve", VALVE, {"value": float("nan")}, "params.value", "must be a number"),
        ("set_pump", PUMP, {"value": 1}, "params.value", "true or false"),
        ("note_valve", VALVE, {"text": "x", "priority": 2.5}, "params.priority", "whole number"),
        ("note_valve", VALVE, {"text": 7, "priority": 2}, "params.text", "must be text"),
        ("note_valve", VALVE, {"text": "x", "priority": 0}, "params.priority", "minimum"),
        ("note_valve", VALVE, {"text": "x", "priority": 4}, "params.priority", "maximum"),
        ("set_valve", VALVE, ["value", 10], "params", "must be an object"),
    ],
)
def test_missing_extra_and_mistyped_parameters_are_refused(
    world: World, action: str, obj: str, params: Any, member: str, words: str
):
    error = refused(world, action, obj, params)
    kept(world, error)
    assert members(error) == [member]
    assert words in error.detail["reasons"][0]["message"]


@pytest.mark.parametrize(
    ("action", "obj", "value", "words"),
    [
        ("set_valve", VALVE, 0.5, "below the control's minimum 1"),
        ("set_valve", VALVE, 250.0, "above the control's maximum 200"),
        ("advise_speed", PUMP, 0.85, "not one of the control's values"),
        ("set_valve", VALVE, 60.0, "larger than the control's max_step 50"),
        ("set_valve", BARE_VALVE, 10.0, "max_step cannot be checked"),
    ],
)
def test_each_bound_of_the_control_is_enforced(
    world: World, action: str, obj: str, value: float, words: str
):
    error = refused(world, action, obj, {"value": value})
    kept(world, error)
    assert set(members(error)) == {"params.value"}
    # 250 is above the maximum and, from 5, a larger step than max_step allows.
    assert any(words in found["message"] for found in error.detail["reasons"])


def test_a_step_of_exactly_max_step_from_the_value_in_force_is_accepted(world: World):
    # VLV-1's setting in force is 5.0.
    made = actions.propose(
        world.twin,
        "set_valve",
        object=VALVE,
        params={"value": 55.0},
        reason=None,
        source="tester",
        state="healthy",
        now=NOW,
        executor=None,
    )
    assert made["status"] == "proposed"
    assert made["reasons"] == []


def test_a_value_the_controlled_property_cannot_hold_is_refused(world: World):
    document = json.loads(json.dumps(world.document))
    # Undeclared, `value` takes the type of the property the control sets.
    document["actions"]["advise_speed"]["parameters"] = {}
    register(world.twin, manifest_of(document))
    try:
        error = refused(world, "advise_speed", PUMP, {"value": "fast"})
        assert members(error) == ["params.value"]
        assert "must be a number" in error.detail["reasons"][0]["message"]
        # Declared as text, it is still held by a property of numbers.
        document["actions"]["advise_speed"]["parameters"] = {"value": {"type": "string"}}
        register(world.twin, manifest_of(document))
        error = refused(world, "advise_speed", PUMP, {"value": "fast"})
        assert members(error) == ["params.value"]
        assert "speed holds numbers" in error.detail["reasons"][0]["message"]
    finally:
        register(world.twin, world.manifest)


def test_every_reason_is_given_at_once(world: World):
    error = refused(world, "set_pump", VALVE, {"speed": 2})
    assert members(error) == ["object", "params.value", "params.speed"]
    assert "(and 2 more)" in error.message
    kept(world, error)


def test_a_disabled_action_is_refused(world: World):
    document = json.loads(json.dumps(world.document))
    del document["actions"]["note_valve"]
    register(world.twin, manifest_of(document))
    try:
        error = refused(world, "note_valve", VALVE, {"text": "x", "priority": 1})
        assert members(error) == ["action"]
        assert "disabled" in error.detail["reasons"][0]["message"]
        kept(world, error)
    finally:
        register(world.twin, world.manifest)


def test_a_refusal_proposed_again_under_its_key_is_raised_again_without_a_write(world: World):
    first = refused(world, "set_valve", VALVE, {"value": 999.0}, key="refused-once")
    head = world.store.head()
    second = refused(world, "set_valve", VALVE, {"value": 999.0}, key="refused-once")
    assert second.detail["record"] == first.detail["record"]
    assert second.detail["reasons"] == first.detail["reasons"]
    assert world.store.head() == head


def test_a_fidelity_state_that_does_not_exist_is_refused(world: World):
    with pytest.raises(ActionError) as caught:
        actions.propose(
            world.twin,
            "set_valve",
            object=VALVE,
            params={"value": 10.0},
            reason=None,
            source="tester",
            state="fine",
            now=NOW,
            executor=None,
        )
    assert caught.value.code == "action.bad_state"
