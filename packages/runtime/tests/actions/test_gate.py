"""The interlock as a plain function: every autonomy against every fidelity state and
every set of incidents not resolved, and why each block is a block."""

from __future__ import annotations

from typing import Any

import pytest
from dta_runtime.actions import gate

BREACH = {"kind": "breach", "uid": "twin_incident:breach-20260105T003000Z-0a1b2c3d4e"}
SIGNAL = {"kind": "signal", "uid": "twin_incident:signal-20260105T003000Z-1b2c3d4e5f"}
STALE = {"kind": "stale", "uid": "twin_incident:stale-20260105T003000Z-2c3d4e5f60"}
RECONCILE = {"kind": "reconcile", "uid": "twin_incident:reconcile-20260105T003000Z-3d4e5f6071"}

# Each cell of the table: the state and the incidents not resolved.
CELLS: dict[str, tuple[str, list[dict[str, str]]]] = {
    "healthy": ("healthy", []),
    "correcting, a breach": ("correcting", [BREACH]),
    "correcting, a signal": ("correcting", [SIGNAL]),
    "correcting, a breach and a signal": ("correcting", [BREACH, SIGNAL]),
    "correcting, a stale": ("correcting", [STALE]),
    "correcting, a reconcile": ("correcting", [RECONCILE]),
    "correcting, a breach and a stale": ("correcting", [BREACH, STALE]),
    "correcting, a stale and a reconcile": ("correcting", [STALE, RECONCILE]),
    "unknown": ("unknown", []),
    "failed": ("failed", [{**STALE}]),
}
AUTONOMOUS_EXECUTES = {
    "healthy",
    "correcting, a breach",
    "correcting, a signal",
    "correcting, a breach and a signal",
}
ACTIONS: dict[str, dict[str, Any]] = {
    "advisory": {"autonomy": "advisory", "binding": "executor"},
    "advisory binding": {"autonomy": "autonomous", "binding": "advisory"},
    "supervised": {"autonomy": "supervised", "binding": "executor"},
    "autonomous": {"autonomy": "autonomous", "binding": "executor"},
}


def decided(action: str, cell: str, has_executor: bool = True) -> str:
    state, incidents = CELLS[cell]
    return gate.on_proposal(
        ACTIONS[action], has_executor=has_executor, state=state, incidents=incidents
    )


@pytest.mark.parametrize("cell", list(CELLS))
def test_an_autonomous_action_executes_only_in_its_cells(cell: str):
    expected = gate.EXECUTE if cell in AUTONOMOUS_EXECUTES else gate.BLOCK
    assert decided("autonomous", cell) == expected
    # Nothing to make the call: it is only proposed.
    assert decided("autonomous", cell, has_executor=False) == gate.PROPOSE


@pytest.mark.parametrize("cell", list(CELLS))
@pytest.mark.parametrize("action", ["advisory", "advisory binding", "supervised"])
def test_advisory_and_supervised_actions_are_proposed_in_every_cell(action: str, cell: str):
    assert decided(action, cell) == gate.PROPOSE
    assert decided(action, cell, has_executor=False) == gate.PROPOSE


@pytest.mark.parametrize("cell", list(CELLS))
def test_an_approval_is_blocked_only_while_the_twin_is_failed(cell: str):
    state, _ = CELLS[cell]
    assert gate.on_approval(state) == (gate.BLOCK if state == "failed" else gate.EXECUTE)


@pytest.mark.parametrize(
    ("cell", "named"),
    [
        ("unknown", [["unknown", "no check has vouched for it yet"]]),
        ("failed", [["failed", "could not put itself right"]]),
        ("correcting, a stale", [["stale", STALE["uid"], "the data is too old to decide on"]]),
        (
            "correcting, a reconcile",
            [["reconcile", RECONCILE["uid"], "an earlier command did not take effect"]],
        ),
        ("correcting, a breach and a stale", [["stale", STALE["uid"]]]),
        ("correcting, a stale and a reconcile", [[STALE["uid"]], [RECONCILE["uid"]]]),
    ],
)
def test_each_block_names_what_blocks(cell: str, named: list[list[str]]):
    state, incidents = CELLS[cell]
    reasons = gate.blocked_reasons(state, approval=False, incidents=incidents)
    assert [reason["member"] for reason in reasons] == ["fidelity"] * len(named)
    for reason, words in zip(reasons, named, strict=True):
        assert reason["message"].startswith(f"the twin is {state}")
        for word in words:
            assert word in reason["message"], (word, reason["message"])
    # A breach or a signal never is the reason.
    assert not any(
        found["uid"] in reason["message"] for reason in reasons for found in (BREACH, SIGNAL)
    )


def test_a_blocked_approval_names_the_failed_state():
    (reason,) = gate.blocked_reasons("failed", approval=True)
    assert reason == {
        "member": "fidelity",
        "message": "the twin is failed: an approved action is not executed while it is failed",
    }


def test_only_stale_and_reconcile_incidents_block():
    assert gate.blocking([BREACH, STALE, SIGNAL, RECONCILE]) == [STALE, RECONCILE]
    assert gate.blocking([BREACH, SIGNAL]) == []


WITHHELD = {
    **SIGNAL,
    "withheld": {
        "signal": "twin_signal:0a1b2c3d4e5f60718293",
        "property": "CentrifugalPump.speed",
        "object": "pump:P1",
    },
}
ON_SPEED = {**ACTIONS["autonomous"], "target": "Pump.speed"}


def on(action: dict[str, Any], incident: dict[str, Any], object_uid: str) -> str:
    return gate.on_proposal(
        action, has_executor=True, state="correcting", incidents=[incident], object_uid=object_uid
    )


def test_an_autonomous_action_on_an_unknown_target_is_blocked():
    # The target names the abstract type, the signal the concrete one: the object decides.
    assert on(ON_SPEED, WITHHELD, "pump:P1") == gate.BLOCK
    # Another object, another property, or a signal whose value is still inferred.
    assert on(ON_SPEED, WITHHELD, "pump:P2") == gate.EXECUTE
    assert on({**ON_SPEED, "target": "Pump.valve"}, WITHHELD, "pump:P1") == gate.EXECUTE
    assert on(ON_SPEED, SIGNAL, "pump:P1") == gate.EXECUTE
    # Supervised: proposed, as in every cell.
    assert on({**ON_SPEED, "autonomy": "supervised"}, WITHHELD, "pump:P1") == gate.PROPOSE
    (reason,) = gate.blocked_reasons(
        "correcting", approval=False, incidents=[WITHHELD], action=ON_SPEED, object_uid="pump:P1"
    )
    assert reason == {
        "member": "fidelity",
        "message": f"the twin is correcting and its signal incident {SIGNAL['uid']} is not "
        "resolved: the value of CentrifugalPump.speed of pump:P1, the control's target, is "
        "unknown (signal twin_signal:0a1b2c3d4e5f60718293)",
    }
    assert gate.blocking([WITHHELD, STALE], ON_SPEED, "pump:P1") == [WITHHELD, STALE]
    assert gate.blocking([WITHHELD, STALE]) == [STALE]
