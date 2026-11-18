"""The twin's actions through its API: registered with the other registries, proposed
and judged with the fidelity state of the moment, decided by an operator, sent to a real
plant over HTTP, refused with every reason, and told to a webhook."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from actions_api_testkit import (
    APPROVAL_PENDING,
    BARE_VALVE,
    BLOCKED,
    PUMP,
    VALVE,
    Acting,
    acting_document,
    at,
    code,
)
from actions_testkit import manifest_of, ontology_text
from dta_runtime.api.models import (
    ActionDecided,
    ActionRecordList,
    ActionRecordOne,
    ActionStepped,
    ActionTypeList,
    ActionTypeOne,
    ControlLoopList,
    ControlLoopOne,
    Registered,
)
from dta_runtime.twin import Bundle, install_twin

MakeActing = Callable[..., Acting]
ACTIONS = ["advise_speed", "note_valve", "set_pump", "set_valve", "suggest_pump"]
LOOPS = ["broken", "garbled", "pumping"]


def uids(scope: str, names: list[str]) -> list[str]:
    return [f"{scope}:{name}" for name in names]


def test_registration_writes_actions_and_loops_in_the_version_of_the_other_registries(
    acting: MakeActing, water_text: str
):
    world = acting()
    answer = world.registered
    Registered.model_validate(answer)
    assert answer["actions"] == {
        "created": uids("twin_action", ACTIONS),
        "changed": [],
        "unchanged": [],
        "orphaned": [],
    }
    assert answer["loops"]["created"] == uids("twin_control_loop", LOOPS)
    assert "twin_function:pump_policy" in answer["functions"]["created"]
    # One version holds every registry.
    version = answer["version"]["id"]
    assert version == world.head.id
    for type_name in ("TwinAction", "TwinControlLoop", "TwinFunction", "TwinQuestion"):
        assert world.twin.store.table(type_name, at=version).num_rows > 0, type_name
    before = world.twin.store.table("TwinAction", at=world.twin.store.versions()[1].id)
    assert before.num_rows == 0

    # Asking again changes nothing and writes nothing.
    again = world.register(world.upload(code()))
    assert again["actions"]["unchanged"] == uids("twin_action", ACTIONS)
    assert again["loops"]["unchanged"] == uids("twin_control_loop", LOOPS)
    assert again["version"]["id"] == version

    # A loop the manifest no longer declares is orphaned, and disabled.
    text = ontology_text(water_text)
    document = acting_document(text, world.telemetry, webhook=False)
    document["twin"]["revision"] = 2
    del document["control_loops"]["garbled"]
    revised = install_twin(
        world.twin.store.settings, Bundle(manifest=manifest_of(document), ontology_text=text)
    )
    revised.twin.close()
    orphaned = world.register(world.upload(code()))
    assert orphaned["loops"]["orphaned"] == ["twin_control_loop:garbled"]
    assert orphaned["actions"]["orphaned"] == []
    garbled = world.ok("/control/loops/garbled")["data"]
    assert (garbled["status"], garbled["enabled"]) == ("disabled", False)
    refused = world.post("/control/loops/garbled/enable", json={"by": "operator@plant"})
    world.refused(refused, 409, "action.loop_orphaned")


def test_the_action_types_and_loops_are_listed_as_registered(acting: MakeActing):
    world = acting()
    listed = world.ok("/actions")
    ActionTypeList.model_validate(listed)
    assert listed["executor_configured"] is True
    assert [found["id"] for found in listed["data"]] == ACTIONS
    executable = {found["id"]: found["executable"] for found in listed["data"]}
    assert executable == {
        "advise_speed": False,
        "note_valve": False,
        "set_pump": True,
        "set_valve": True,
        "suggest_pump": False,
    }
    one = world.ok("/actions/set_valve")
    ActionTypeOne.model_validate(one)
    assert one["data"]["uid"] == "twin_action:set_valve"
    assert (one["data"]["autonomy"], one["data"]["binding"]) == ("supervised", "executor")
    assert one["data"]["bounds"] == {"minimum": 1.0, "maximum": 200.0, "max_step": 50.0}
    assert (one["data"]["settle"], one["data"]["tolerance"]) == ("PT45M", 0.5)
    # The executor's call is shown by its templates; no variable's value is.
    assert "Tk-" not in str(one) and "127.0.0.1" not in str(one)
    assert world.ok("/actions/twin_action:set_pump")["data"]["id"] == "set_pump"
    world.refused(world.get("/actions/nothing"), 404, "action.not_found")

    loops = ControlLoopList.model_validate(world.ok("/control/loops")).model_dump()["data"]
    assert [found["id"] for found in loops] == LOOPS
    assert {found["enabled"] for found in loops} == {False}
    pumping = world.ok("/control/loops/pumping")["data"]
    ControlLoopOne.model_validate(
        {"data": pumping, "version": world.ok("/control/loops")["version"]}
    )
    assert pumping["controls"] == ["pump_running", "valve_setting"]
    assert (pumping["policy"], pumping["interval"]) == ("pump_policy", "PT15M")
    world.refused(world.get("/control/loops/nothing"), 404, "action.loop_not_found")


# What each autonomy comes to in each fidelity state: (proposal, after an approval). The
# twin is brought into `correcting` by a reconcile incident, which blocks an autonomous
# action; what other incidents do is the next test's.
EXPECTED = {
    "unknown": {"advisory": "proposed", "supervised": "executed", "autonomous": "blocked"},
    "healthy": {"advisory": "proposed", "supervised": "executed", "autonomous": "executed"},
    "correcting": {"advisory": "proposed", "supervised": "executed", "autonomous": "blocked"},
    "failed": {"advisory": "proposed", "supervised": "blocked", "autonomous": "blocked"},
}


@pytest.mark.parametrize("state", list(EXPECTED))
def test_each_autonomy_in_each_fidelity_state(acting: MakeActing, state: str):
    world = acting(with_webhook=True)
    world.into(state, 2)
    expected = EXPECTED[state]

    # Advisory: recorded, never executed, nothing to approve.
    advised = world.proposed("advise_speed", PUMP, 0.9)
    assert (advised["status"], advised["execution"]) == ("proposed", None)
    assert advised["fidelity"] == state
    world.refused(world.decide(advised["uid"], "approve"), 409, "action.not_executable")

    # Supervised: waits for an approval, which the interlock may still block.
    waiting = world.proposed("set_valve", VALVE, 20.0)
    assert (waiting["status"], waiting["fidelity"]) == ("proposed", state)
    approved = world.decide(waiting["uid"], "approve", note="go ahead")
    assert approved.status_code == 200, approved.text
    ActionStepped.model_validate(approved.json())
    ActionRecordOne.model_validate(world.ok(f"/actions/records/{waiting['uid']}"))
    decided = approved.json()["data"]
    assert decided["status"] == expected["supervised"]
    assert decided["approval"]["by"] == "operator@plant"

    # Autonomous: executed while the twin is healthy, or correcting with no stale or
    # reconcile incident open.
    autonomous = world.proposed("set_pump", PUMP, True)
    assert autonomous["status"] == expected["autonomous"]
    if autonomous["status"] == "blocked":
        assert autonomous["reasons"] and autonomous["execution"] is None
        assert autonomous["reasons"][0]["message"].startswith(f"the twin is {state}")
    else:
        assert autonomous["execution"]["http_status"] == 200

    executed = [found["status"] for found in (decided, autonomous)].count("executed")
    assert len(world.calls()) == executed
    blocked = [found["uid"] for found in (decided, autonomous) if found["status"] == "blocked"]
    told = world.wait_events(1 + len(blocked), APPROVAL_PENDING, BLOCKED)
    assert [event["subject"] for event in told if event["type"] == APPROVAL_PENDING] == [
        waiting["uid"]
    ]
    assert sorted(event["subject"] for event in told if event["type"] == BLOCKED) == sorted(blocked)
    assert all(event["data"]["fidelity"] == state for event in told)


# Per kind of incident open: whether an autonomous action is executed, and what the
# reason of a block says besides the incident's uid.
WITH_INCIDENT = {
    "breach": (True, None),
    "signal": (True, None),
    "stale": (False, "the data is too old to decide on"),
    "reconcile": (False, "an earlier command did not take effect"),
}


@pytest.mark.parametrize("kind", list(WITH_INCIDENT))
def test_an_autonomous_action_and_a_loop_s_run_with_an_incident_open(acting: MakeActing, kind: str):
    world = acting(with_webhook=True, retry_budget=2)
    world.into("healthy", 2)
    incident = world.opened(kind, 7)
    executes, why = WITH_INCIDENT[kind]
    assert world.state() == "correcting"

    pumped = world.proposed("set_pump", PUMP, True)
    assert pumped["fidelity"] == "correcting"
    if executes:
        assert pumped["status"] == "executed"
        assert pumped["reasons"] == [] and pumped["execution"]["http_status"] == 200
    else:
        assert pumped["status"] == "blocked" and pumped["execution"] is None
        (reason,) = pumped["reasons"]
        assert reason["member"] == "fidelity"
        assert kind in reason["message"] and incident in reason["message"]
        assert why in reason["message"]
        (told,) = world.wait_events(1, BLOCKED)
        assert (told["subject"], told["data"]["reasons"]) == (pumped["uid"], [reason])

    # A loop's run is judged the same way: its pump executed or blocked, its valve
    # waiting for an approval.
    world.post("/control/loops/pumping/enable", json={"by": "operator@plant"})
    run = world.done(world.post("/control/loops/pumping/run"))
    assert (run["state"], run["proposed"], run["awaiting"]) == ("correcting", 2, 1)
    assert (run["executed"], run["blocked"]) == ((1, 0) if executes else (0, 1))
    pump = next(found for found in run["records"] if found["action"] == "set_pump")
    if not executes:
        assert incident in world.record(pump["uid"])["reasons"][0]["message"]
    assert len(world.calls()) == (2 if executes else 0)


def test_an_autonomous_action_blocked_by_a_stale_incident_executes_once_it_resolves(
    acting: MakeActing,
):
    world = acting(retry_budget=2)
    world.into("healthy", 2)
    incident = world.opened("stale", 3)
    blocked = world.proposed("set_pump", PUMP, True)
    assert (blocked["status"], blocked["fidelity"]) == ("blocked", "correcting")
    assert incident in blocked["reasons"][0]["message"]
    world.post("/control/loops/pumping/enable", json={"by": "operator@plant"})
    run = world.done(world.post("/control/loops/pumping/run"))
    assert (run["state"], run["executed"], run["blocked"]) == ("correcting", 0, 1)
    assert world.calls() == []

    # Fresh data again: the incident resolves, and the same proposal is executed.
    assert world.check(4, fresh=True) == "healthy"
    world.clock.set(at(4))
    assert world.ok(f"/fidelity/incidents/{incident}")["data"]["status"] == "resolved"
    pumped = world.proposed("set_pump", PUMP, True)
    assert (pumped["status"], pumped["fidelity"]) == ("executed", "healthy")
    run = world.done(world.post("/control/loops/pumping/run"))
    assert (run["state"], run["executed"], run["blocked"]) == ("healthy", 1, 0)
    assert len(world.calls()) == 2


def test_the_interlock_holds_while_the_twin_failed_and_lets_go_once_it_recovered(
    acting: MakeActing,
):
    world = acting(with_webhook=True)
    world.into("healthy", 2)
    waiting = world.proposed("set_valve", VALVE, 20.0)
    assert world.check(3, fresh=False) == "failed"
    world.clock.set(at(3))

    # Approved while failed: blocked, and the plant is sent nothing.
    blocked = world.decide(waiting["uid"], "approve").json()["data"]
    assert blocked["status"] == "blocked"
    assert blocked["reasons"][0]["member"] == "fidelity"
    pumped = world.proposed("set_pump", PUMP, True)
    assert pumped["status"] == "blocked"
    assert world.calls() == []
    world.refused(world.decide(blocked["uid"], "approve"), 409, "action.not_pending")

    # A fresh check resolves the stale incident: the twin acts again.
    assert world.check(4, fresh=True) == "healthy"
    world.clock.set(at(4))
    pumped = world.proposed("set_pump", PUMP, True)
    assert pumped["status"] == "executed"
    (call,) = world.calls()
    assert call.method == "POST" and call.body["status"] == "on"
    assert call.headers["X-Object"] == PUMP
    told = world.wait_events(3, APPROVAL_PENDING, BLOCKED)
    assert [event["type"] for event in told] == [APPROVAL_PENDING, BLOCKED, BLOCKED]


def test_a_refused_proposal_is_recorded_and_answered_with_every_reason(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)

    too_far = world.propose("set_valve", VALVE, 500.0)
    body = world.refused(too_far, 422, "action.rejected")
    assert body["record"].startswith("twin_action_record:")
    assert body["errors"] and all({"member", "message"} <= set(found) for found in body["errors"])
    kept = world.record(body["record"])
    assert kept["status"] == "rejected" and kept["reasons"] == body["errors"]

    # Every reason at once: the wrong type of object, and a parameter it does not have.
    many = world.propose("set_valve", PUMP, params={"value": 20.0, "speed": 2})
    found = world.refused(many, 422, "action.rejected")
    assert len(found["errors"]) >= 2, found

    step = world.refused(world.propose("set_valve", VALVE, 80.0), 422, "action.rejected")
    assert any(
        "max_step" in reason["message"] or "step" in reason["message"] for reason in step["errors"]
    ), step
    nothing = world.refused(world.propose("open_gate", VALVE, 1.0), 422, "action.rejected")
    assert nothing["record"] is None
    missing = world.refused(
        world.propose("set_valve", BARE_VALVE + "x", 20.0), 422, "action.rejected"
    )
    assert missing["record"]

    # The body is the proposal and nothing else.
    extra = world.post(
        "/actions/set_valve/propose",
        json={"object": VALVE, "params": {"value": 20.0}, "source": "me", "when": "now"},
    )
    assert extra.status_code == 400, extra.text
    assert world.calls() == []


def test_a_key_names_one_proposal(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)
    first = world.propose("set_pump", PUMP, True, key="agent-7:pump-on")
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    ActionDecided.model_validate(first.json())
    head = world.head
    again = world.propose("set_pump", PUMP, True, key="agent-7:pump-on")
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["data"]["uid"] == first.json()["data"]["uid"]
    assert world.head == head and len(world.calls()) == 1
    other = world.propose("set_pump", PUMP, False, key="agent-7:pump-on")
    world.refused(other, 409, "action.key_reused")


def test_decisions_on_records(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)
    waiting = world.proposed("set_valve", VALVE, 20.0)
    turned_down = world.decide(waiting["uid"], "reject", note="not now")
    assert turned_down.status_code == 200, turned_down.text
    record = turned_down.json()["data"]
    assert record["status"] == "rejected"
    assert record["approval"]["decision"] == "rejected"
    assert record["approval"]["note"] == "not now"
    assert turned_down.json()["version"]["id"] == world.head.id
    world.refused(world.decide(waiting["uid"], "reject"), 409, "action.not_pending")
    world.refused(world.decide(waiting["uid"], "approve"), 409, "action.not_pending")
    nowhere = world.decide("twin_action_record:nothing", "approve")
    world.refused(nowhere, 404, "action.record_not_found")
    world.refused(
        world.get("/actions/records/twin_action_record:nothing"), 404, "action.record_not_found"
    )
    unsigned = world.post(f"/actions/records/{waiting['uid']}/approve", json={"note": "x"})
    assert unsigned.status_code == 400, unsigned.text

    # The twin's clock decides the time of a decision; without it there is none.
    second = world.proposed("set_valve", VALVE, 25.0)
    world.clock.stopped = True
    world.refused(world.decide(second["uid"], "approve"), 503, "clock.unavailable")
    world.refused(world.propose("set_valve", VALVE, 30.0), 503, "clock.unavailable")
    world.clock.stopped = False
    assert world.record(second["uid"])["status"] == "proposed"


def test_records_are_listed_newest_first_page_by_page_and_filtered(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)
    made: list[dict[str, Any]] = []
    for index in range(5):
        world.clock.set(at(2 + index * 0.1))
        made.append(world.proposed("set_valve", VALVE, 10.0 + index, source=f"agent-{index % 2}"))
    world.clock.set(at(3))
    made.append(world.proposed("advise_speed", PUMP, 0.8))
    world.propose("set_valve", VALVE, 900.0)

    listed = world.ok("/actions/records")["data"]
    assert len(listed) == 7
    stamps = [found["proposed_at"] for found in listed]
    assert stamps == sorted(stamps, reverse=True)

    pages: list[str] = []
    cursor = None
    while True:
        page = world.ok("/actions/records", limit=3, **({"cursor": cursor} if cursor else {}))
        ActionRecordList.model_validate(page)
        pages.extend(found["uid"] for found in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == [found["uid"] for found in listed]

    proposed = world.ok("/actions/records", status="proposed", action="set_valve")["data"]
    assert sorted(found["uid"] for found in proposed) == sorted(found["uid"] for found in made[:5])
    assert len(world.ok("/actions/records", source="agent-1")["data"]) == 2
    assert len(world.ok("/actions/records", object=PUMP)["data"]) == 1
    assert len(world.ok("/actions/records", status="rejected")["data"]) == 1
    assert world.ok("/actions/records", reconciliation="ok")["data"] == []


def test_without_the_executors_add_on_nothing_is_executed(acting: MakeActing):
    world = acting(executors=False, with_webhook=True)
    world.into("healthy", 2)
    listed = world.ok("/actions")
    assert listed["executor_configured"] is False
    assert {found["executable"] for found in listed["data"]} == {False}
    autonomous = world.proposed("set_pump", PUMP, True)
    assert autonomous["status"] == "proposed"
    world.refused(world.decide(autonomous["uid"], "approve"), 409, "action.not_executable")
    assert world.calls() == []
    # Nothing waits for an approval the twin could act on.
    assert world.events(APPROVAL_PENDING) == []
    ids = [found["id"] for found in world.ok("/lifecycle/schedule")["entries"]]
    assert "reconcile" not in ids
