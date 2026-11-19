"""A control's fail-safe, sent by the API at a fidelity check: once per failure, to the
objects the twin commanded, past the interlock, recorded with its origin, listed and
filtered by it, told to the webhook with a severity, and armed again once the twin is
healthy. The twin's state is moved by the fidelity library; the check job of the API is
run at the same time, a replay, and at later times."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from actions_api_testkit import PUMP, VALVE, Acting, at
from dta_core.store.model import iso_utc
from dta_runtime import fidelity
from dta_runtime.api.fidelity_work import check_work
from dta_runtime.api.models import ActionRecordList, ActionTypeList, NotificationList

MakeActing = Callable[..., Acting]
FAILSAFE = "twin.action.failsafe"
FAILSAFE_FAILED = "twin.action.failsafe_failed"


def safe_pump(document: dict[str, Any]) -> None:
    """The pump is stopped when the twin fails; the valve, supervised, holds."""
    controls = document["contract"]["controls"]
    controls["pump_running"]["on_failure"] = {
        "mode": "safe_value",
        "parameters": {"value": False},
    }
    controls["valve_setting"]["on_failure"] = {"mode": "hold"}


def stale_pump(document: dict[str, Any]) -> None:
    """The pump is stopped once data has been too old for half an hour, too."""
    safe_pump(document)
    document["contract"]["controls"]["pump_running"]["on_failure"]["after_stale"] = "PT30M"


def checked(world: Acting, step: float) -> dict[str, Any]:
    """The API's check job at a step, run here; its result."""
    outcome = check_work(world.services, at(step))()
    assert outcome.error is None, outcome.error
    return outcome.result


def test_the_check_that_fails_the_twin_sends_the_fail_safe_once_and_tells_it(
    acting: MakeActing,
):
    world = acting(with_webhook=True, changes=safe_pump)
    world.into("healthy", 2)
    assert checked(world, 2)["failsafe"] == []
    listed = world.ok("/actions")
    ActionTypeList.model_validate(listed)
    failures = {found["id"]: found["on_failure"] for found in listed["data"]}
    # A safe value sent by the control's own action names it.
    assert failures["set_pump"] == {
        "mode": "safe_value",
        "action": "set_pump",
        "parameters": {"value": False},
    }
    assert failures["suggest_pump"] == failures["set_pump"]
    assert failures["set_valve"] == {"mode": "hold", "parameters": {}}
    assert failures["note_valve"] is None

    commanded = world.proposed("set_pump", PUMP, True)
    assert commanded["status"] == "executed" and commanded["origin"] == "proposal"
    world.plant.reset()
    assert world.check(3, fresh=False) == "failed"
    summary = checked(world, 3)
    assert summary["replayed"] is True and summary["state"] == "failed"
    (sent,) = summary["failsafe"]
    assert (sent["control"], sent["object"], sent["action"], sent["mode"]) == (
        "pump_running",
        PUMP,
        "set_pump",
        "safe_value",
    )
    assert (sent["status"], sent["trigger"], sent["told"]) == ("executed", "failed", FAILSAFE)
    assert sent["episode"] == iso_utc(at(3))
    (call,) = world.calls()
    assert call.body["status"] == "off" and call.headers["X-Object"] == PUMP

    record = world.record(sent["record"])
    assert (record["source"], record["origin"], record["status"]) == (
        "failsafe:pump_running",
        "failsafe",
        "executed",
    )
    assert record["fidelity"] == "failed" and record["params"] == {"value": False}
    (told,) = world.wait_events(1, FAILSAFE)
    assert (told["subject"], told["severity"], told["time"]) == (
        sent["record"],
        "warning",
        "2026-01-05T00:45:00Z",
    )
    assert told["data"]["record"] == sent["record"] and told["data"]["object"] == PUMP
    notices = world.ok("/lifecycle/notifications")
    NotificationList.model_validate(notices)
    assert [found["severity"] for found in notices["data"] if found["type"] == FAILSAFE] == [
        "warning"
    ]

    # Listed by origin.
    by_origin = world.ok("/actions/records", origin="failsafe")
    ActionRecordList.model_validate(by_origin)
    assert [found["uid"] for found in by_origin["data"]] == [sent["record"]]
    others = world.ok("/actions/records", origin="proposal")["data"]
    assert [found["uid"] for found in others] == [commanded["uid"]]
    assert world.ok("/actions/records", origin="loop")["data"] == []
    world.refused(world.get("/actions/records", origin="nobody"), 400, "api.bad_request")

    # A later check while the twin stays failed sends nothing more, nor does its replay.
    world.clock.set(at(4))
    later = checked(world, 4)
    assert (later["replayed"], later["state"], later["failsafe"]) == (False, "failed", [])
    assert checked(world, 3)["failsafe"] == []
    # An ordinary autonomous proposal is still blocked meanwhile.
    blocked = world.proposed("set_pump", PUMP, True)
    assert blocked["status"] == "blocked"
    assert len(world.calls()) == 1

    # Healthy again, then commanded and failed again: another episode, another fail-safe.
    assert world.check(5, fresh=True) == "healthy"
    world.clock.set(at(5))
    assert fidelity.unhealthy_since(world.twin) is None
    assert world.proposed("set_pump", PUMP, True)["status"] == "executed"
    assert world.check(6, fresh=False) == "failed"
    assert fidelity.unhealthy_since(world.twin) == at(6)
    (again,) = checked(world, 6)["failsafe"]
    assert again["status"] == "executed" and again["record"] != sent["record"]
    assert again["episode"] == iso_utc(at(6))
    assert len(world.calls()) == 3
    assert len(world.wait_events(2, FAILSAFE)) == 2
    assert world.events(FAILSAFE_FAILED) == []


def test_a_fail_safe_whose_call_fails_is_told_at_the_highest_severity_once(
    acting: MakeActing,
):
    world = acting(with_webhook=True, changes=safe_pump)
    world.into("healthy", 2)
    world.proposed("set_pump", PUMP, True)
    # The valve, approved and executed, is under a control that holds.
    waiting = world.proposed("set_valve", VALVE, 20.0)
    assert world.decide(waiting["uid"], "approve").json()["data"]["status"] == "executed"
    world.plant.reset()
    world.plant.mode = "500"
    assert world.check(3, fresh=False) == "failed"
    (sent,) = checked(world, 3)["failsafe"]
    assert (sent["object"], sent["status"], sent["told"]) == (PUMP, "failed", FAILSAFE_FAILED)
    assert sent["error"]
    assert world.record(sent["record"])["status"] == "failed"
    (told,) = world.wait_events(1, FAILSAFE_FAILED)
    assert told["severity"] == "critical" and told["data"]["status"] == "failed"
    calls = len(world.calls())
    assert calls == 1

    # The pump is still commanded, and this episode's fail-safe was made: not again.
    world.plant.mode = "ok"
    world.clock.set(at(4))
    (again,) = checked(world, 4)["failsafe"]
    assert (again["created"], again["told"], again["record"]) == (False, None, sent["record"])
    assert len(world.calls()) == calls
    assert len(world.events(FAILSAFE_FAILED)) == 1
    assert world.events(FAILSAFE) == []


def test_old_data_sends_the_fail_safe_once_it_has_been_too_old_long_enough(
    acting: MakeActing,
):
    world = acting(with_webhook=True, changes=stale_pump, retry_budget=10)
    world.into("healthy", 2)
    world.proposed("set_pump", PUMP, True)
    world.plant.reset()
    # Open at step 3: not yet at step 4, half an hour old at step 5.
    for step in (3, 4):
        assert world.check(step, fresh=False) == "correcting"
        assert checked(world, step)["failsafe"] == []
    assert world.calls() == []
    assert world.check(5, fresh=False) == "correcting"
    (sent,) = checked(world, 5)["failsafe"]
    assert (sent["trigger"], sent["status"], sent["episode"]) == (
        "stale",
        "executed",
        iso_utc(at(3)),
    )
    record = world.record(sent["record"])
    assert "too old for a question since 2026-01-05T00:45:00Z" in record["reason"]
    assert record["fidelity"] == "correcting"
    assert len(world.calls()) == 1
    (told,) = world.wait_events(1, FAILSAFE)
    assert told["data"]["trigger"] == "stale"
    # Still stale at the next check: the pump was handed over, nothing more is sent.
    assert world.check(6, fresh=False) == "correcting"
    assert checked(world, 6)["failsafe"] == []
    assert len(world.calls()) == 1


def test_a_control_that_holds_sends_nothing(acting: MakeActing):
    world = acting(with_webhook=True)
    world.into("healthy", 2)
    world.proposed("set_pump", PUMP, True)
    world.plant.reset()
    assert world.check(3, fresh=False) == "failed"
    assert checked(world, 3)["failsafe"] == []
    assert world.calls() == []
    assert world.ok("/actions/records", origin="failsafe")["data"] == []
    assert world.ok("/actions/set_pump")["data"]["on_failure"] == {
        "mode": "hold",
        "parameters": {},
    }
