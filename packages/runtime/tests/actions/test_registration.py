"""Action types and control loops of the manifest become records, as functions do: each
is created, changed, left unchanged or orphaned."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

from actions_testkit import TOKEN, World, manifest_of, register
from dta_runtime import actions
from dta_runtime.actions import known, rows

ACTIONS = ["advise_speed", "note_valve", "set_pump", "set_valve", "suggest_pump"]


def uids(scope: str, ids: list[str]) -> list[str]:
    return [f"{scope}:{found}" for found in ids]


def test_every_action_and_loop_of_a_new_manifest_is_created(world: World):
    found = rows(world.manifest, {})
    assert sorted(found.answer["actions"]["created"]) == uids("twin_action", ACTIONS)
    assert found.answer["loops"]["created"] == ["twin_control_loop:pumping"]
    assert all(
        not found.answer[part][outcome]
        for part in found.answer
        for outcome in ("changed", "unchanged", "orphaned")
    )
    by_uid = {row["uid"]: row for row in found.upserts["TwinAction"]}
    valve = by_uid["twin_action:set_valve"]
    assert valve["control"] == "valve_setting"
    assert valve["target"] == "Valve.setting"
    assert valve["object_type"] == "Valve"
    assert valve["autonomy"] == "supervised"
    assert valve["binding"] == "executor"
    assert json.loads(valve["bounds"]) == {"minimum": 1.0, "maximum": 200.0, "max_step": 50.0}
    assert valve["settle"] == "PT45M"
    assert valve["tolerance"] == 0.5
    # Without a settle time of its own: twice the sync interval.
    assert by_uid["twin_action:set_pump"]["settle"] == "PT30M"
    assert by_uid["twin_action:note_valve"]["autonomy"] == "advisory"
    assert by_uid["twin_action:note_valve"]["control"] is None
    executor = json.loads(by_uid["twin_action:set_pump"]["executor"])
    assert executor["env"] == ["PLANT_URL", "PLANT_TOKEN"]
    assert "${PLANT_TOKEN}" in executor["url"]
    assert TOKEN not in json.dumps(found.upserts)
    (loop,) = found.upserts["TwinControlLoop"]
    assert loop == {
        "uid": "twin_control_loop:pumping",
        "description": "Keeps the pump and the valve where the policy wants them",
        "controls": ["pump_running", "valve_setting"],
        "policy": "twin_function:pump_policy",
        "interval": "PT15M",
        "origin": "manifest",
        "status": "active",
        "enabled": False,
    }


def test_records_in_line_with_the_manifest_are_unchanged(world: World):
    found = rows(world.manifest, known(world.twin))
    assert sorted(found.answer["actions"]["unchanged"]) == uids("twin_action", ACTIONS)
    assert found.answer["loops"]["unchanged"] == ["twin_control_loop:pumping"]
    assert found.upserts == {}


def test_a_changed_action_and_loop_carry_only_what_changed(make_world: Callable[[], World]):
    world = make_world()
    actions.enable(world.twin, "pumping", by="operator", now=world.store.head().t)
    document = copy.deepcopy(world.document)
    document["contract"]["controls"]["valve_setting"]["bounds"]["maximum"] = 150.0
    document["actions"]["set_pump"]["description"] = "Start or stop the lake pump"
    document["control_loops"]["pumping"]["interval"] = "PT30M"
    found = rows(manifest_of(document), known(world.twin))
    assert sorted(found.answer["actions"]["changed"]) == uids(
        "twin_action", ["set_pump", "set_valve"]
    )
    assert found.answer["loops"]["changed"] == ["twin_control_loop:pumping"]
    by_uid = {row["uid"]: row for row in found.upserts["TwinAction"]}
    assert set(by_uid["twin_action:set_valve"]) == {"uid", "bounds"}
    assert json.loads(by_uid["twin_action:set_valve"]["bounds"])["maximum"] == 150.0
    assert by_uid["twin_action:set_pump"] == {
        "uid": "twin_action:set_pump",
        "description": "Start or stop the lake pump",
    }
    # A loop that is changed keeps whether it is enabled.
    assert found.upserts["TwinControlLoop"] == [
        {"uid": "twin_control_loop:pumping", "interval": "PT30M"}
    ]


def test_what_the_manifest_no_longer_declares_is_orphaned_once(make_world: Callable[[], World]):
    world = make_world()
    actions.enable(world.twin, "pumping", by="operator", now=world.store.head().t)
    document = copy.deepcopy(world.document)
    del document["actions"]["note_valve"]
    del document["control_loops"]["pumping"]
    smaller = manifest_of(document)
    found = register(world.twin, smaller)
    assert found.answer["actions"]["orphaned"] == ["twin_action:note_valve"]
    assert found.answer["loops"]["orphaned"] == ["twin_control_loop:pumping"]
    assert found.upserts["TwinAction"] == [{"uid": "twin_action:note_valve", "status": "disabled"}]
    assert found.upserts["TwinControlLoop"] == [
        {"uid": "twin_control_loop:pumping", "status": "disabled", "enabled": False}
    ]
    assert world.store.get("twin_action:note_valve").values["status"] == "disabled"
    loop = world.store.get("twin_control_loop:pumping").values
    assert (loop["status"], loop["enabled"]) == ("disabled", False)

    again = rows(smaller, known(world.twin))
    assert again.upserts == {}
    assert again.answer["actions"]["orphaned"] == []

    back = rows(world.manifest, known(world.twin))
    assert back.answer["actions"]["changed"] == ["twin_action:note_valve"]
    assert back.answer["loops"]["changed"] == ["twin_control_loop:pumping"]
    by_uid = {row["uid"]: row for row in back.upserts["TwinAction"]}
    assert by_uid["twin_action:note_valve"] == {"uid": "twin_action:note_valve", "status": "active"}
    # Declared again, the loop is active but stays disabled until it is enabled.
    assert back.upserts["TwinControlLoop"] == [
        {"uid": "twin_control_loop:pumping", "status": "active"}
    ]


def test_records_not_from_the_manifest_are_never_orphaned(world: World):
    stored = known(world.twin)
    stored["TwinAction"]["twin_action:by_hand"] = {
        "uid": "twin_action:by_hand",
        "origin": "runtime",
        "status": "active",
    }
    found = rows(world.manifest, stored)
    assert found.answer["actions"]["orphaned"] == []


def test_the_action_types_are_read_with_their_parameters_bounds_autonomy_and_binding(
    world: World,
):
    found = {document["id"]: document for document in actions.types(world.twin)}
    assert sorted(found) == ACTIONS
    assert found["set_valve"]["parameters"] == {"value": {"type": "number", "unit": "1"}}
    assert found["set_valve"]["bounds"] == {"minimum": 1.0, "maximum": 200.0, "max_step": 50.0}
    assert found["set_valve"]["autonomy"] == "supervised"
    assert found["set_valve"]["binding"] == "executor"
    assert found["suggest_pump"]["binding"] == "advisory"
    assert found["suggest_pump"]["autonomy"] == "autonomous"
    assert found["note_valve"]["parameters"]["priority"] == {
        "type": "integer",
        "minimum": 1.0,
        "maximum": 3.0,
    }
    assert found["set_pump"]["executor"]["headers"] == {
        "X-API-Key": "${PLANT_TOKEN}",
        "X-Object": "{uid}",
    }
    first = world.store.resolve("#1")[0]
    assert actions.types(world.twin, at=first) == []
