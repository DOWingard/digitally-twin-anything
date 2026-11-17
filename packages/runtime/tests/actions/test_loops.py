"""Control loops: enabled and disabled, and runs that ask a policy and propose what it
answers."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from actions_testkit import (
    LOOP,
    POLICY,
    POLICY_BUDGET_MS,
    PUMP,
    VALVE,
    World,
    at,
    manifest_of,
    register,
)
from dta_core.errors import DtaError
from dta_runtime import actions
from dta_runtime.actions import ActionError, Execution

NOW = at(2)


class Answering:
    """A policy that gives the answer it was handed, or raises."""

    def __init__(self, answer: Any = None, raises: BaseException | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple[str, float]] = []

    def __call__(self, function: str, *, deadline_s: float) -> Any:
        self.calls.append((function, deadline_s))
        if self.raises is not None:
            raise self.raises
        return self.answer


class Scripted:
    """Executes, then fails every call after the first."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        self.calls += 1
        if self.calls == 1:
            return Execution("executed", http_status=200, duration_ms=1.0, attempts=1)
        return Execution(
            "failed", http_status=500, duration_ms=1.0, attempts=1, error="the system answered 500"
        )


def item(action: str, obj: str, value: Any, reason: str | None = "policy") -> dict[str, Any]:
    return {"action": action, "object": obj, "params": {"value": value}, "reason": reason}


def keyed(key: str) -> str:
    return "twin_action_record:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def loop_record(world: World, at_version: Any = "head") -> dict[str, Any]:
    return actions.control_loop(world.twin, "pumping", at=at_version)


def run(world: World, policy: Any, key: str, state: str = "healthy", sender: Any = None) -> dict:
    return actions.run_loop(
        world.twin,
        "pumping",
        policy=policy,
        state=state,
        now=NOW,
        executor=sender,
        key=key,
    )


def from_loop(world: World) -> int:
    return len(actions.records(world.twin, source=LOOP))


@pytest.fixture(scope="module")
def enabled(world: World) -> World:
    actions.enable(world.twin, "pumping", by="operator", now=NOW)
    return world


def test_a_loop_is_registered_disabled_and_refuses_to_run(make_world: Callable[[], World]):
    world = make_world()
    (found,) = actions.loops(world.twin)
    assert found == {
        "id": "pumping",
        "uid": LOOP,
        "description": "Keeps the pump and the valve where the policy wants them",
        "controls": ["pump_running", "valve_setting"],
        "policy": POLICY,
        "interval": "PT15M",
        "origin": "manifest",
        "status": "active",
        "enabled": False,
        "last_run_at": None,
        "last_run": None,
    }
    policy = Answering({"actions": []})
    with pytest.raises(ActionError) as caught:
        run(world, policy, "loop:pumping:1")
    assert caught.value.code == "action.loop_disabled"
    assert policy.calls == []


def test_a_loop_is_enabled_and_disabled_by_someone(make_world: Callable[[], World]):
    world = make_world()
    turned = actions.enable(world.twin, "pumping", by="operator", now=NOW)
    assert turned["enabled"] is True
    (version,) = world.store.versions(newest_first=True, limit=1)
    assert dict(version.extra) == {"loop": LOOP, "enabled": True, "by": "operator"}
    assert version.cause == "registration"
    head = world.store.head()
    assert actions.enable(world.twin, LOOP, by="operator", now=NOW)["enabled"] is True
    assert world.store.head() == head
    assert actions.disable(world.twin, "pumping", by="operator", now=NOW)["enabled"] is False
    assert loop_record(world)["enabled"] is False


def test_an_unknown_loop_is_not_found(world: World):
    for attempt in (
        lambda: actions.enable(world.twin, "nowhere", by="operator", now=NOW),
        lambda: actions.control_loop(world.twin, "nowhere"),
        lambda: actions.run_loop(
            world.twin,
            "nowhere",
            policy=Answering({"actions": []}),
            state="healthy",
            now=NOW,
            executor=None,
            key="k",
        ),
    ):
        with pytest.raises(ActionError) as caught:
            attempt()
        assert caught.value.code == "action.loop_not_found"


def test_a_loop_the_manifest_no_longer_declares_cannot_be_enabled(
    make_world: Callable[[], World],
):
    world = make_world()
    document = copy.deepcopy(world.document)
    del document["control_loops"]["pumping"]
    register(world.twin, manifest_of(document))
    with pytest.raises(ActionError) as caught:
        actions.enable(world.twin, "pumping", by="operator", now=NOW)
    assert caught.value.code == "action.loop_orphaned"


def test_a_run_proposes_every_action_and_keeps_what_came_of_each(enabled: World):
    world = enabled
    policy = Answering(
        {
            "actions": [
                item("set_pump", PUMP, True),
                item("set_valve", VALVE, 10.0),
                item("set_valve", VALVE, 999.0),
                item("set_pump", VALVE, True),
                item("set_pump", PUMP, False, reason=None),
            ],
            "notes": ["the cheap hours start now"],
        }
    )
    before = len(world.store.versions())
    found = run(world, policy, "loop:pumping:mixed", sender=Scripted())
    assert policy.calls == [(POLICY, POLICY_BUDGET_MS / 1000.0)]
    assert {name: found[name] for name in ("proposed", "executed", "awaiting", "rejected")} == {
        "proposed": 5,
        "executed": 1,
        "awaiting": 1,
        "rejected": 2,
    }
    assert (found["blocked"], found["failed"]) == (0, 1)
    assert found["status"] == "succeeded"
    assert found["loop"] == "pumping"
    assert found["notes"] == ["the cheap hours start now"]
    assert found["error"] is None
    assert [entry["status"] for entry in found["records"]] == [
        "executed",
        "proposed",
        "rejected",
        "rejected",
        "failed",
    ]
    assert [entry["uid"] for entry in found["records"]] == [
        keyed(f"loop:pumping:mixed:{index}") for index in range(5)
    ]
    for entry in found["records"]:
        record = actions.record(world.twin, entry["uid"])
        assert record["source"] == LOOP
        assert record["status"] == entry["status"]
    # Two writes for each call made, one for each other proposal; the loop's record is
    # in the version of the last.
    versions = world.store.versions()
    assert len(versions) == before + 7
    stored = loop_record(world)
    assert stored["last_run_at"] == NOW
    assert stored["last_run"] == {key: value for key, value in found.items() if key != "loop"}
    assert loop_record(world, versions[-2].ref)["last_run"] is None


def test_an_autonomous_action_of_a_run_is_blocked_while_the_twin_is_not_healthy(enabled: World):
    found = run(
        enabled,
        Answering({"actions": [item("set_pump", PUMP, True)]}),
        "loop:pumping:blocked",
        state="correcting",
        sender=Scripted(),
    )
    assert (found["proposed"], found["blocked"], found["executed"]) == (1, 1, 0)
    assert found["state"] == "correcting"


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (RuntimeError("the solver diverged"), "RuntimeError was raised; the cause is in the log"),
        (DtaError("no answer", code="worker.timeout"), "worker.timeout: no answer"),
    ],
)
def test_a_policy_that_raises_fails_the_run_and_proposes_nothing(
    enabled: World, raised: BaseException, error: str
):
    world = enabled
    count = from_loop(world)
    before = len(world.store.versions())
    key = f"loop:pumping:raises:{type(raised).__name__}"
    with pytest.raises(ActionError) as caught:
        run(world, Answering(raises=raised), key)
    assert caught.value.code == "action.policy_failed"
    assert caught.value.detail["run"]["error"] == error
    assert from_loop(world) == count
    assert len(world.store.versions()) == before + 1
    last = loop_record(world)["last_run"]
    assert (last["key"], last["status"], last["error"], last["proposed"]) == (
        key,
        "failed",
        error,
        0,
    )


BAD_ANSWERS = {
    "not an object": (["set_pump"], "not an object"),
    "no actions": ({"notes": []}, "actions is not a list"),
    "actions not a list": ({"actions": {"set_pump": PUMP}}, "actions is not a list"),
    "another member": ({"actions": [], "plan": "x"}, "members it may not have: plan"),
    "notes not texts": ({"actions": [], "notes": [3]}, "notes is not a list of texts"),
    "too many": (
        {"actions": [item("set_valve", VALVE, 10.0)] * 21},
        "21 actions; an answer names at most 20",
    ),
    "an action not an object": ({"actions": ["set_pump"]}, "actions[0] is not an object"),
    "an unknown action": (
        {"actions": [item("explode", PUMP, True)]},
        "actions[0].action: there is no action type 'explode'",
    ),
    "no object": (
        {"actions": [{"action": "set_pump", "params": {"value": True}}]},
        "actions[0].object is not the uid of an object",
    ),
    "params not an object": (
        {"actions": [{"action": "set_pump", "object": PUMP, "params": [True]}]},
        "actions[0].params is not an object",
    ),
    "a member too many": (
        {"actions": [{**item("set_pump", PUMP, True), "when": "now"}]},
        "actions[0] has members it may not have: when",
    ),
    "outside the loop's controls": (
        {"actions": [item("set_pump", PUMP, True), item("advise_speed", PUMP, 0.9)]},
        "actions[1].action: advise_speed carries out control 'pump_speed', not one of the "
        "loop's (pump_running, valve_setting)",
    ),
    "no control at all": (
        {"actions": [{"action": "note_valve", "object": VALVE, "params": {}}]},
        "actions[0].action: note_valve carries out no control",
    ),
}


@pytest.mark.parametrize("case", list(BAD_ANSWERS))
def test_an_answer_that_cannot_be_used_whole_fails_the_run_before_anything_is_proposed(
    enabled: World, case: str
):
    world = enabled
    answer, problem = BAD_ANSWERS[case]
    count = from_loop(world)
    key = f"loop:pumping:bad:{case}"
    with pytest.raises(ActionError) as caught:
        run(world, Answering(answer), key, sender=Scripted())
    assert caught.value.code == "action.bad_policy_answer"
    assert any(problem in found for found in caught.value.detail["problems"])
    assert from_loop(world) == count
    last = loop_record(world)["last_run"]
    assert (last["key"], last["status"], last["proposed"]) == (key, "failed", 0)
    assert problem in last["error"]


def test_the_same_run_key_twice_runs_once(enabled: World):
    world = enabled
    policy = Answering({"actions": [item("set_valve", VALVE, 12.0)], "notes": []})
    first = run(world, policy, "loop:pumping:twice")
    head = world.store.head()
    second = run(world, policy, "loop:pumping:twice")
    assert len(policy.calls) == 1
    assert world.store.head() == head
    assert second["repeated"] is True
    assert second["records"] == first["records"]
    assert second["awaiting"] == first["awaiting"] == 1


def test_a_run_that_proposes_nothing_is_recorded_in_a_version_of_its_own(enabled: World):
    world = enabled
    before = len(world.store.versions())
    found = run(world, Answering({"actions": [], "notes": ["nothing to do"]}), "loop:pumping:idle")
    assert (found["status"], found["proposed"], found["notes"]) == (
        "succeeded",
        0,
        ["nothing to do"],
    )
    assert len(world.store.versions()) == before + 1
    assert loop_record(world)["last_run"]["key"] == "loop:pumping:idle"
