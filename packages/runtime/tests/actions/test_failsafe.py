"""A control's fail-safe: when it is due, which objects it goes to, past the interlock and
not past the judgment, once per episode, what is recorded when it cannot be sent, and the
origin of a record."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from actions_testkit import BARE_VALVE, PUMP, VALVE, Plant, World, at, executor, manifest_of
from actions_testkit import register as register_manifest
from dta_core.manifest import Manifest
from dta_runtime import actions
from dta_runtime.actions import HttpExecutor, failsafe
from dta_runtime.actions.records import RECORD_TYPE, keyed_id

EPISODE = at(3)
STALE = {"kind": "stale", "uid": "twin_incident:stale-20260105T004500Z-2c3d4e5f60"}
FAILED = {"state": "failed", "incidents": [STALE]}


def failing_with(world: World, control: str = "pump_running", **on_failure: Any) -> Manifest:
    document = copy.deepcopy(world.document)
    document["contract"]["controls"][control]["on_failure"] = on_failure
    return manifest_of(document)


def safe_pump(world: World, **more: Any) -> Manifest:
    return failing_with(world, mode="safe_value", parameters={"value": False}, **more)


def released(world: World, precondition: str | None = None) -> Manifest:
    """The pump handed back to its local control by `release_pump`; registered."""
    document = copy.deepcopy(world.document)
    document["actions"]["release_pump"] = {
        "description": "Hand a pump back to its local control",
        "object_type": "Pump",
        "binding": "executor",
        "executor": {
            "kind": "http",
            "url": "${PLANT_URL}/api/v1/controls/{id}",
            "method": "POST",
            "env": ["PLANT_URL", "PLANT_TOKEN"],
            "headers": {"X-API-Key": "${PLANT_TOKEN}"},
            "body": {"mode": "{mode}"},
        },
        "parameters": {"mode": {"type": "string"}},
    }
    if precondition is not None:
        document["actions"]["release_pump"]["precondition"] = precondition
    document["contract"]["controls"]["pump_running"]["on_failure"] = {
        "mode": "release",
        "action": "release_pump",
        "parameters": {"mode": "local"},
    }
    manifest = manifest_of(document)
    register_manifest(world.twin, manifest)
    return manifest


def command(world: World, call: HttpExecutor, step: float, value: bool = True) -> dict:
    """The pump set by the twin while it is healthy: an executed autonomous action."""
    made = actions.propose(
        world.twin,
        "set_pump",
        object=PUMP,
        params={"value": value},
        reason="the tank is low",
        source="operator@plant",
        state="healthy",
        incidents=[],
        now=at(step),
        executor=call,
    )
    assert made["status"] == "executed", made
    return made


def sent_once(world: World, manifest: Manifest, call: Any, step: float, **options: Any) -> list:
    [due] = failsafe.due(manifest, state="failed", stale=[], now=at(step))
    return failsafe.send(
        world.twin,
        due,
        episode=options.get("episode", EPISODE),
        state="failed",
        incidents=[STALE],
        now=at(step),
        executor=call,
    )


def test_a_fail_safe_is_due_while_failed_and_once_data_has_been_too_old_long_enough(
    world: World,
):
    manifest = safe_pump(world, after_stale="PT30M")
    [due] = failsafe.due(manifest, state="failed", stale=[], now=at(4))
    assert (due.control, due.mode, due.action, due.parameters, due.trigger) == (
        "pump_running",
        "safe_value",
        "set_pump",
        {"value": False},
        "failed",
    )
    assert (
        due.reason()
        == "fail-safe of control pump_running (safe_value): the twin's fidelity is failed"
    )
    stale = [{"uid": STALE["uid"], "opened_at": at(3)}]
    # Open 15 minutes, then 30: due once it has been open for `after_stale`.
    assert failsafe.due(manifest, state="correcting", stale=stale, now=at(4)) == []
    [late] = failsafe.due(manifest, state="correcting", stale=stale, now=at(5))
    assert (late.trigger, late.incident, late.opened_at) == ("stale", STALE["uid"], at(3))
    assert "too old for a question since 2026-01-05T00:45:00Z" in late.reason()
    # Without `after_stale` only the failed state makes it due; a hold never is.
    plain = safe_pump(world)
    assert failsafe.due(plain, state="correcting", stale=stale, now=at(20)) == []
    assert failsafe.due(world.manifest, state="failed", stale=stale, now=at(20)) == []
    assert failsafe.due(manifest, state="healthy", stale=[], now=at(20)) == []
    # A release names its own action; a safe value names one only to replace the control's.
    [handed] = failsafe.due(
        failing_with(world, mode="release", action="suggest_pump", parameters={}),
        state="failed",
        stale=[],
        now=at(4),
    )
    assert (handed.mode, handed.action) == ("release", "suggest_pump")


def test_the_fail_safe_goes_past_the_interlock_to_each_commanded_object_once_per_episode(
    make_world: Callable[[], World], fresh_plant: Plant
):
    world = make_world()
    manifest = safe_pump(world)
    call = executor(world.manifest, fresh_plant.url)
    # Nothing commanded under the control: nothing is sent, and that is no failure.
    assert sent_once(world, manifest, call, 3) == []
    advice = actions.propose(
        world.twin,
        "suggest_pump",
        object=PUMP,
        params={"value": True},
        reason=None,
        source="operator@plant",
        state="healthy",
        incidents=[],
        now=at(2),
        executor=call,
    )
    assert advice["status"] == "proposed"
    assert failsafe.commanded(world.twin, "pump_running") == []

    command(world, call, 2)
    assert failsafe.commanded(world.twin, "pump_running") == [PUMP]
    fresh_plant.reset()
    [sent] = sent_once(world, manifest, call, 3)
    assert (sent.status, sent.created, sent.trigger, sent.object) == (
        "executed",
        True,
        "failed",
        PUMP,
    )
    key = failsafe.key_of(EPISODE, "pump_running", PUMP)
    assert key == f"failsafe:2026-01-05T00:45:00Z:pump_running:{PUMP}"
    assert sent.record == f"twin_action_record:{keyed_id(key)}"
    record = actions.record(world.twin, sent.record)
    assert (record["source"], record["status"], record["fidelity"]) == (
        "failsafe:pump_running",
        "executed",
        "failed",
    )
    assert record["params"] == {"value": False} and record["proposed_at"] == at(3)
    assert "the twin's fidelity is failed" in record["reason"]
    [received] = fresh_plant.received
    assert received.body["status"] == "off"
    assert received.headers["Idempotency-Key"] == sent.record.split(":", 1)[1]

    # The pump is handed over: due again in the episode, nothing is sent.
    assert failsafe.commanded(world.twin, "pump_running") == []
    assert sent_once(world, manifest, call, 4) == []
    # An ordinary autonomous proposal is still blocked by the interlock meanwhile.
    blocked = actions.propose(
        world.twin,
        "set_pump",
        object=PUMP,
        params={"value": True},
        reason=None,
        source="operator@plant",
        incidents=[STALE],
        now=at(4),
        executor=call,
        state="failed",
    )
    assert blocked["status"] == "blocked"
    assert len(fresh_plant.received) == 1

    # Commanded again once healthy, a later episode sends it again under a key of its own.
    command(world, call, 6)
    [again] = sent_once(world, manifest, call, 7, episode=at(7))
    assert again.executed and again.created and again.record != sent.record
    assert len(fresh_plant.received) == 3

    # The records' origins, as the store filters them.
    def sources(origin: str) -> set[str]:
        table = world.twin.store.table(
            RECORD_TYPE, columns=["source"], filter=failsafe.origin_filter(origin)
        )
        return set(table.column("source").to_pylist())

    assert sources("failsafe") == {"failsafe:pump_running"}
    assert sources("proposal") == {"operator@plant"}
    assert sources("loop") == set()
    assert [failsafe.origin_of(found) for found in ("twin_control_loop:pumping", None)] == [
        "loop",
        "proposal",
    ]


def test_a_fail_safe_whose_call_fails_is_recorded_and_not_sent_again_in_its_episode(
    make_world: Callable[[], World], fresh_plant: Plant
):
    world = make_world()
    manifest = safe_pump(world)
    call = executor(world.manifest, fresh_plant.url)
    command(world, call, 2)
    fresh_plant.mode = "500"
    [sent] = sent_once(world, manifest, call, 3)
    assert (sent.status, sent.created) == ("failed", True) and sent.error
    assert actions.record(world.twin, sent.record)["status"] == "failed"
    calls = len(fresh_plant.received)
    fresh_plant.mode = "ok"
    # The pump is still commanded, and its fail-safe of this episode was made: not again.
    assert failsafe.commanded(world.twin, "pump_running") == [PUMP]
    [again] = sent_once(world, manifest, call, 4)
    assert (again.created, again.status, again.record) == (False, "failed", sent.record)
    assert len(fresh_plant.received) == calls


def test_a_fail_safe_that_cannot_be_sent_as_the_control_says_is_recorded_with_why(
    make_world: Callable[[], World], fresh_plant: Plant
):
    world = make_world()
    call = executor(world.manifest, fresh_plant.url)
    command(world, call, 2)
    fresh_plant.reset()
    # Nothing to make the call: recorded as a failed call that says so.
    [unsent] = sent_once(world, safe_pump(world), None, 3)
    assert unsent.status == "failed" and "executors add-on is off" in str(unsent.error)
    assert actions.record(world.twin, unsent.record)["execution"]["error"] == unsent.error

    # The parameters are still judged: a value the control does not allow is refused.
    wrong = failing_with(world, mode="safe_value", parameters={"value": "off"})
    [refused] = sent_once(world, wrong, call, 4, episode=at(4))
    assert refused.status == "rejected" and refused.created
    assert actions.record(world.twin, refused.record)["status"] == "rejected"

    # And the precondition: the pump is not running, so it is not released.
    guarded = released(world, precondition="running:eq:true")
    [kept] = sent_once(world, guarded, call, 5, episode=at(5))
    assert kept.status == "rejected"
    assert [reason["member"] for reason in kept.reasons] == ["precondition"]
    assert fresh_plant.received == []

    # Without it, the release goes to the pump with the control's constants.
    [handed] = sent_once(world, released(world), call, 6, episode=at(6))
    assert handed.executed and handed.action == "release_pump"
    [received] = fresh_plant.received
    assert received.body == {"mode": "local"}
    assert actions.record(world.twin, handed.record)["source"] == "failsafe:pump_running"
    assert failsafe.commanded(world.twin, "pump_running") == []
    # The valves were never commanded under their control.
    assert failsafe.commanded(world.twin, "valve_setting") == []
    assert {VALVE, BARE_VALVE}.isdisjoint(failsafe.commanded(world.twin, "pump_running"))
