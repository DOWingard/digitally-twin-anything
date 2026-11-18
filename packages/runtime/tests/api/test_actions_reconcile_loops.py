"""Executed actions compared with what the plant then reported, through the API: a
mismatch opens a reconcile incident and is told, a later action that had its effect
closes it. And control loops run as jobs, their policy in the twin's own code run by the
real worker."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from actions_api_testkit import (
    APPROVAL_PENDING,
    INCIDENT,
    MISMATCH,
    VALVE,
    Acting,
    at,
)
from actions_testkit import link, observe
from dta_runtime.api.models import ControlLoopOne, ReconcileOut

MakeActing = Callable[..., Acting]


def valve_at(world: Acting, step: float, setting: float) -> None:
    observe(world.twin, at(step), {"Valve": [link(VALVE, valve_type="tcv", setting=setting)]})


def executed_valve(world: Acting, value: float) -> dict:
    waiting = world.proposed("set_valve", VALVE, value)
    approved = world.decide(waiting["uid"], "approve")
    assert approved.status_code == 200, approved.text
    record = approved.json()["data"]
    assert record["status"] == "executed", record
    assert record["reconciliation"] == "pending"
    return record


def test_a_mismatch_opens_an_incident_and_an_action_that_had_its_effect_closes_it(
    acting: MakeActing,
):
    world = acting(with_webhook=True)
    world.into("healthy", 2)
    first = executed_valve(world, 20.0)

    # Before its settle time has passed, nothing is due.
    early = world.post("/actions/reconcile", headers={"Idempotency-Key": "reconcile:early"})
    assert early.status_code == 200, early.text
    assert early.json()["data"] == [] and early.json()["opened"] == []

    # The plant reports 11 where 20 was set: a mismatch, an incident, the twin correcting.
    valve_at(world, 4, 11.0)
    world.clock.set(at(6))
    asked = world.post("/actions/reconcile", headers={"Idempotency-Key": "reconcile:one"})
    assert asked.status_code == 200, asked.text
    answer = asked.json()
    ReconcileOut.model_validate(answer)
    (outcome,) = answer["data"]
    assert (outcome["record"], outcome["outcome"]) == (first["uid"], "mismatch")
    assert (outcome["value"], outcome["observed"], outcome["tolerance"]) == (20.0, 11.0, 0.5)
    (incident,) = answer["opened"]
    assert outcome["incidents"] == [incident]
    assert answer["version"]["id"] == world.head.id
    assert world.state() == "correcting"
    assert world.record(first["uid"])["reconciliation"] == "mismatch"
    opened = world.ok(f"/fidelity/incidents/{incident}")["data"]
    assert (opened["kind"], opened["subject"], opened["status"]) == (
        "reconcile",
        first["uid"],
        "open",
    )
    told = world.wait_events(2, MISMATCH, INCIDENT)
    mismatch = next(event for event in told if event["type"] == MISMATCH)
    assert mismatch["subject"] == first["uid"]
    assert mismatch["data"]["incident"] == incident
    assert [event["type"] for event in answer["events"]].count(MISMATCH) == 1

    # The same key gives the same answer and does nothing again.
    head = world.head
    again = world.post("/actions/reconcile", headers={"Idempotency-Key": "reconcile:one"})
    assert again.json() == answer
    assert world.head == head

    # Approved while correcting, a new setting is executed and observed as set.
    second = executed_valve(world, 30.0)
    assert second["fidelity"] == "correcting"
    valve_at(world, 8, 30.2)
    world.clock.set(at(10))
    closing = world.post("/actions/reconcile")
    assert closing.status_code == 200, closing.text
    (ok,) = closing.json()["data"]
    assert (ok["record"], ok["outcome"], ok["incidents"]) == (second["uid"], "ok", [incident])
    assert closing.json()["closed"] == [incident]
    assert world.state() == "healthy"
    assert world.ok(f"/fidelity/incidents/{incident}")["data"]["status"] == "resolved"
    assert world.record(second["uid"])["reconciliation"] == "ok"
    assert world.ok("/actions/records", reconciliation="ok")["data"][0]["uid"] == second["uid"]
    assert world.post("/actions/reconcile").json()["data"] == []


def test_a_loop_runs_its_policy_through_the_worker_and_proposes_what_it_names(
    acting: MakeActing,
):
    world = acting(with_webhook=True)
    world.into("healthy", 2)
    run_path = "/control/loops/pumping/run"
    world.refused(world.post(run_path), 409, "action.loop_disabled")

    enabled = world.post("/control/loops/pumping/enable", json={"by": "operator@plant"})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["data"]["enabled"] is True
    head = world.head
    assert world.post("/control/loops/pumping/enable", json={"by": "x"}).status_code == 200
    assert world.head == head

    asked = world.post(run_path, headers={"Idempotency-Key": "run-1"})
    assert asked.status_code == 202, asked.text
    job = world.wait(asked)
    assert job["state"] == "succeeded", job
    assert job["kind"] == "loop"
    run = job["result"]
    assert (run["key"], run["status"], run["state"]) == ("run-1", "succeeded", "healthy")
    assert (run["proposed"], run["executed"], run["awaiting"]) == (2, 1, 1)
    assert run["notes"] == ["the valve stands at 5"]
    assert run["told"] == [APPROVAL_PENDING]
    statuses = {found["action"]: found["status"] for found in run["records"]}
    assert statuses == {"set_pump": "executed", "set_valve": "proposed"}
    (call,) = world.calls()
    assert call.body["status"] == "on"
    for found in run["records"]:
        record = world.record(found["uid"])
        assert record["source"] == "twin_control_loop:pumping"
        assert record["fidelity"] == "healthy"
    waiting = next(found for found in run["records"] if found["action"] == "set_valve")
    assert world.record(waiting["uid"])["params"] == {"value": 15.0}
    world.wait_events(1, APPROVAL_PENDING)

    loop = ControlLoopOne.model_validate(world.ok("/control/loops/pumping")).model_dump()["data"]
    assert loop["last_run"]["key"] == "run-1"
    assert loop["last_run"]["status"] == "succeeded"

    # The same key gives the same job back.
    repeated = world.post(run_path, headers={"Idempotency-Key": "run-1"})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["job"]["id"] == job["id"]

    # Disabled, it is not run.
    disabled = world.post("/control/loops/pumping/disable", json={"by": "operator@plant"})
    assert disabled.json()["data"]["enabled"] is False
    world.refused(world.post(run_path), 409, "action.loop_disabled")
    world.refused(world.post("/control/loops/nothing/run"), 404, "action.loop_not_found")
    world.refused(
        world.post("/control/loops/nothing/enable", json={"by": "x"}),
        404,
        "action.loop_not_found",
    )


@pytest.mark.parametrize("state", ["unknown", "failed"])
def test_a_loop_run_while_the_twin_is_unknown_or_failed_proposes_and_blocks(
    acting: MakeActing, state: str
):
    world = acting()
    world.into(state, 2)
    world.post("/control/loops/pumping/enable", json={"by": "operator@plant"})
    run = world.done(world.post("/control/loops/pumping/run"))
    assert (run["state"], run["proposed"], run["blocked"], run["executed"]) == (
        state,
        2,
        1,
        0,
    )
    assert run["key"].startswith("loop:pumping:")
    blocked = next(found for found in run["records"] if found["status"] == "blocked")
    (reason,) = world.record(blocked["uid"])["reasons"]
    assert reason["message"].startswith(f"the twin is {state}")
    assert world.calls() == []


def test_a_policy_that_fails_or_answers_nonsense_fails_its_run(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)
    for loop, code in (("broken", "action.policy_failed"), ("garbled", "action.bad_policy_answer")):
        world.post(f"/control/loops/{loop}/enable", json={"by": "operator@plant"})
        job = world.wait(world.post(f"/control/loops/{loop}/run"))
        assert job["state"] == "failed", job
        assert job["error"]["code"] == code, job
        assert job["result"]["status"] == "failed"
        assert job["result"]["proposed"] == 0
        assert job["result"]["loop"] == loop
        last = world.ok(f"/control/loops/{loop}")["data"]["last_run"]
        assert last["status"] == "failed" and last["error"], last
    assert world.ok("/actions/records")["data"] == []
    assert world.calls() == []
    assert world.ok("/actions/records", source="twin_control_loop:broken")["data"] == []
