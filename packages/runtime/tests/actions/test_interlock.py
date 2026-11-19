"""The interlock: every autonomy against every fidelity state and set of incidents not
resolved, with and without something to make the call; approvals and rejections."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from actions_testkit import PUMP, VALVE, Plant, World, at, executor
from dta_runtime import actions
from dta_runtime.actions import ActionError, HttpExecutor

NOW = at(2)
BREACH = {"kind": "breach", "uid": "twin_incident:breach-20260105T003000Z-0a1b2c3d4e"}
SIGNAL = {"kind": "signal", "uid": "twin_incident:signal-20260105T003000Z-1b2c3d4e5f"}
STALE = {"kind": "stale", "uid": "twin_incident:stale-20260105T003000Z-2c3d4e5f60"}
RECONCILE = {"kind": "reconcile", "uid": "twin_incident:reconcile-20260105T003000Z-3d4e5f6071"}
# A signal incident whose value is unknown: the pump's `running`, or the valve's setting.
RUNNING_UNKNOWN = {
    **SIGNAL,
    "withheld": {
        "signal": "twin_signal:0a1b2c3d4e5f60718293",
        "property": "Pump.running",
        "object": PUMP,
    },
}
SETTING_UNKNOWN = {
    **SIGNAL,
    "withheld": {
        "signal": "twin_signal:1b2c3d4e5f6071829304",
        "property": "Valve.setting",
        "object": VALVE,
    },
}
# The state and the incidents not resolved of each cell of the table.
CELLS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "healthy": ("healthy", []),
    "correcting, a breach": ("correcting", [BREACH]),
    "correcting, a signal": ("correcting", [SIGNAL]),
    "correcting, the target unknown": ("correcting", [RUNNING_UNKNOWN]),
    "correcting, another value unknown": ("correcting", [SETTING_UNKNOWN]),
    "correcting, a stale": ("correcting", [STALE]),
    "correcting, a reconcile": ("correcting", [RECONCILE]),
    "correcting, a breach and a reconcile": ("correcting", [BREACH, RECONCILE]),
    "unknown": ("unknown", []),
    "failed": ("failed", [STALE]),
}
# Action, object and parameters of a proposal that passes every check, per autonomy.
PROPOSALS = {
    "advisory": ("advise_speed", PUMP, {"value": 0.9}),
    "advisory binding": ("suggest_pump", PUMP, {"value": True}),
    "supervised": ("set_valve", VALVE, {"value": 10.0}),
    "autonomous": ("set_pump", PUMP, {"value": True}),
}
# What a proposal becomes: per autonomy, per cell, with and without an executor.
EXPECTED = {
    ("advisory", True): dict.fromkeys(CELLS, "proposed"),
    ("advisory", False): dict.fromkeys(CELLS, "proposed"),
    ("advisory binding", True): dict.fromkeys(CELLS, "proposed"),
    ("advisory binding", False): dict.fromkeys(CELLS, "proposed"),
    ("supervised", True): dict.fromkeys(CELLS, "proposed"),
    ("supervised", False): dict.fromkeys(CELLS, "proposed"),
    ("autonomous", True): {
        "healthy": "executed",
        "correcting, a breach": "executed",
        "correcting, a signal": "executed",
        "correcting, the target unknown": "blocked",
        "correcting, another value unknown": "executed",
        "correcting, a stale": "blocked",
        "correcting, a reconcile": "blocked",
        "correcting, a breach and a reconcile": "blocked",
        "unknown": "blocked",
        "failed": "blocked",
    },
    # Nothing could make the call: the record stays proposed whatever the autonomy.
    ("autonomous", False): dict.fromkeys(CELLS, "proposed"),
}
# What the reason of each block names.
NAMED = {
    "correcting, the target unknown": [
        SIGNAL["uid"],
        "Pump.running",
        PUMP,
        "twin_signal:0a1b2c3d4e5f60718293",
    ],
    "correcting, a stale": ["stale", STALE["uid"]],
    "correcting, a reconcile": ["reconcile", RECONCILE["uid"]],
    "correcting, a breach and a reconcile": ["reconcile", RECONCILE["uid"]],
    "unknown": ["unknown", "no check has vouched for it yet"],
    "failed": ["failed"],
}


@pytest.fixture(scope="module")
def http(world: World, plant: Plant) -> Iterator[HttpExecutor]:
    found = executor(world.manifest)
    yield found
    found.close()


def propose(
    world: World,
    autonomy: str,
    state: str,
    sender: Any,
    incidents: list[dict[str, str]] | None = None,
    **more: Any,
) -> dict:
    action, obj, params = PROPOSALS[autonomy]
    return actions.propose(
        world.twin,
        action,
        object=obj,
        params=params,
        reason="interlock test",
        source="tester",
        state=state,
        incidents=incidents or [],
        now=NOW,
        executor=sender,
        **more,
    )


@pytest.mark.parametrize("cell", list(CELLS))
@pytest.mark.parametrize("with_executor", [True, False])
@pytest.mark.parametrize("autonomy", list(PROPOSALS))
def test_the_interlock_table(
    world: World,
    fresh_plant: Plant,
    http: HttpExecutor,
    autonomy: str,
    with_executor: bool,
    cell: str,
):
    state, incidents = CELLS[cell]
    made = propose(world, autonomy, state, http if with_executor else None, incidents)
    expected = EXPECTED[(autonomy, with_executor)][cell]
    assert made["status"] == expected
    assert made["created"] is True
    assert made["fidelity"] == state
    stored = actions.record(world.twin, made["uid"])
    assert stored["status"] == expected
    assert stored["fidelity"] == state
    assert len(fresh_plant.received) == (1 if expected == "executed" else 0)
    if expected == "blocked":
        (reason,) = stored["reasons"]
        assert reason["member"] == "fidelity"
        assert reason["message"].startswith(f"the twin is {state}")
        for word in NAMED[cell]:
            assert word in reason["message"], (word, reason["message"])
    else:
        assert stored["reasons"] == []
    if expected == "executed":
        assert stored["execution"]["http_status"] == 200
        assert stored["reconciliation"] == "pending"
    else:
        assert stored["execution"] is None
        assert stored["reconciliation"] is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [("healthy", "executed"), ("correcting", "executed"), ("unknown", "executed")],
)
def test_an_approval_executes_unless_the_twin_is_failed(
    world: World, fresh_plant: Plant, http: HttpExecutor, state: str, expected: str
):
    made = propose(world, "supervised", state, http)
    assert made["status"] == "proposed"
    approved = actions.approve(
        world.twin,
        made["uid"],
        by="operator",
        note="looks right",
        state=state,
        now=at(3),
        executor=http,
    )
    assert approved["status"] == expected
    assert approved["approval"] == {
        "decision": "approved",
        "by": "operator",
        "note": "looks right",
        "at": "2026-01-05T00:45:00Z",
        "fidelity": state,
    }
    assert len(fresh_plant.received) == 1
    assert fresh_plant.received[0].method == "PUT"
    assert actions.record(world.twin, made["uid"]) == {
        key: value for key, value in approved.items() if key != "created"
    }


def test_an_approval_while_the_twin_is_failed_blocks(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    made = propose(world, "supervised", "healthy", http)
    blocked = actions.approve(
        world.twin, made["id"], by="operator", note=None, state="failed", now=at(3), executor=http
    )
    assert blocked["status"] == "blocked"
    assert blocked["approval"]["by"] == "operator"
    assert blocked["reasons"][0]["member"] == "fidelity"
    assert fresh_plant.received == []
    # A blocked record is final.
    with pytest.raises(ActionError) as caught:
        actions.approve(
            world.twin,
            made["uid"],
            by="operator",
            note=None,
            state="healthy",
            now=at(3),
            executor=http,
        )
    assert caught.value.code == "action.not_pending"


def test_an_approval_twice_is_refused_the_second_time(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    made = propose(world, "supervised", "healthy", http)
    options = {"by": "operator", "note": None, "state": "healthy", "now": at(3), "executor": http}
    assert actions.approve(world.twin, made["uid"], **options)["status"] == "executed"
    head = world.store.head()
    with pytest.raises(ActionError) as caught:
        actions.approve(world.twin, made["uid"], **options)
    assert caught.value.code == "action.not_pending"
    assert caught.value.detail["status"] == "executed"
    assert len(fresh_plant.received) == 1
    assert world.store.head() == head


@pytest.mark.parametrize(
    ("autonomy", "state", "status"),
    [("autonomous", "healthy", "executed"), ("autonomous", "failed", "blocked")],
)
def test_a_record_that_is_not_awaiting_a_decision_cannot_be_approved_or_rejected(
    world: World, fresh_plant: Plant, http: HttpExecutor, autonomy: str, state: str, status: str
):
    made = propose(world, autonomy, state, http)
    assert made["status"] == status
    with pytest.raises(ActionError) as caught:
        actions.approve(
            world.twin,
            made["uid"],
            by="operator",
            note=None,
            state="healthy",
            now=at(3),
            executor=http,
        )
    assert caught.value.code == "action.not_pending"
    with pytest.raises(ActionError) as caught:
        actions.reject(world.twin, made["uid"], by="operator", note=None, now=at(3))
    assert caught.value.code == "action.not_pending"


def test_an_advisory_record_cannot_be_approved(world: World, http: HttpExecutor):
    for autonomy in ("advisory", "advisory binding"):
        made = propose(world, autonomy, "healthy", http)
        with pytest.raises(ActionError) as caught:
            actions.approve(
                world.twin,
                made["uid"],
                by="operator",
                note=None,
                state="healthy",
                now=at(3),
                executor=http,
            )
        assert caught.value.code == "action.not_executable"
        assert actions.record(world.twin, made["uid"])["status"] == "proposed"


def test_without_an_executor_an_approval_is_refused_and_the_record_stays_proposed(
    world: World,
):
    made = propose(world, "supervised", "healthy", None)
    with pytest.raises(ActionError) as caught:
        actions.approve(
            world.twin,
            made["uid"],
            by="operator",
            note=None,
            state="healthy",
            now=at(3),
            executor=None,
        )
    assert caught.value.code == "action.not_executable"
    assert actions.record(world.twin, made["uid"])["status"] == "proposed"


def test_a_rejection_is_recorded_and_final(world: World, fresh_plant: Plant, http: HttpExecutor):
    made = propose(world, "supervised", "healthy", http)
    turned = actions.reject(world.twin, made["uid"], by="operator", note="not now", now=at(3))
    assert turned["status"] == "rejected"
    assert turned["approval"] == {
        "decision": "rejected",
        "by": "operator",
        "note": "not now",
        "at": "2026-01-05T00:45:00Z",
    }
    assert turned["reasons"] == [{"member": "approval", "message": "turned down by operator"}]
    assert actions.record(world.twin, made["uid"])["status"] == "rejected"
    for attempt in (
        lambda: actions.reject(world.twin, made["uid"], by="operator", note=None, now=at(3)),
        lambda: actions.approve(
            world.twin,
            made["uid"],
            by="operator",
            note=None,
            state="healthy",
            now=at(3),
            executor=http,
        ),
    ):
        with pytest.raises(ActionError) as caught:
            attempt()
        assert caught.value.code == "action.not_pending"
    assert fresh_plant.received == []


def test_an_unknown_record_is_not_found(world: World, http: HttpExecutor):
    for attempt in (
        lambda: actions.approve(
            world.twin,
            "nothing-here",
            by="operator",
            note=None,
            state="healthy",
            now=at(3),
            executor=http,
        ),
        lambda: actions.reject(world.twin, "nothing-here", by="operator", note=None, now=at(3)),
        lambda: actions.record(world.twin, "twin_action_record:nothing-here"),
    ):
        with pytest.raises(ActionError) as caught:
            attempt()
        assert caught.value.code == "action.record_not_found"


def test_whoever_proposed_may_propose_again_after_a_block(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    first = propose(world, "autonomous", "correcting", http, [STALE])
    second = propose(world, "autonomous", "correcting", http, [STALE])
    assert first["status"] == second["status"] == "blocked"
    assert first["uid"] != second["uid"]
    # Once the stale incident is resolved, the same proposal is executed.
    third = propose(world, "autonomous", "correcting", http, [BREACH])
    assert third["status"] == "executed"
    assert len(fresh_plant.received) == 1


def test_records_are_read_newest_first_and_by_filter(world: World, http: HttpExecutor):
    made = propose(world, "advisory", "healthy", http)
    newest = actions.records(world.twin, limit=1)
    assert newest[0]["proposed_at"] == NOW
    mine = actions.records(world.twin, action="advise_speed", status="proposed", object=PUMP)
    assert made["uid"] in [found["uid"] for found in mine]
    assert all(found["action"] == "advise_speed" for found in mine)
    assert all(found["status"] == "proposed" for found in mine)
    assert actions.records(world.twin, source="nobody") == []
