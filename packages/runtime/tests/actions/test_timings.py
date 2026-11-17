"""How long the twin takes to judge and record a proposal, to reconcile many actions at
once and to list its action types, against limits set before they were measured.

Each test prints one `TIMING` line with what it measured (seen with `pytest -s`)."""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable
from typing import Any

from actions_testkit import PUMP, VALVE, Accepting, World, at, observe, register_rows
from dta_runtime import actions

PROPOSAL_LIMIT_MS = 150.0
RECONCILE_LIMIT_S = 2.0
TYPES_LIMIT_MS = 50.0
RUNS = 20
DUE = 200


def report(name: str, **measured: Any) -> None:
    print("TIMING " + json.dumps({"name": name, **measured}))


def timed_ms(call: Callable[[], Any]) -> float:
    started = time.perf_counter()
    call()
    return (time.perf_counter() - started) * 1000.0


def test_a_proposal_that_is_not_executed_is_judged_and_recorded_in_time(world: World):
    def propose(index: int) -> None:
        made = actions.propose(
            world.twin,
            "set_valve",
            object=VALVE,
            params={"value": 10.0 + index % 40},
            reason="timing",
            source="timer",
            state="healthy",
            now=at(2),
            executor=None,
        )
        assert made["status"] == "proposed"

    propose(0)
    took = [timed_ms(lambda index=index: propose(index)) for index in range(1, RUNS + 1)]
    report(
        "proposal not executed",
        runs=RUNS,
        median_ms=round(statistics.median(took), 1),
        max_ms=round(max(took), 1),
        limit_ms=PROPOSAL_LIMIT_MS,
    )
    assert max(took) <= PROPOSAL_LIMIT_MS


def test_the_action_types_are_listed_in_time(world: World):
    assert len(actions.types(world.twin)) == 5
    took = [timed_ms(lambda: actions.types(world.twin)) for _ in range(RUNS)]
    report(
        "types",
        runs=RUNS,
        median_ms=round(statistics.median(took), 1),
        max_ms=round(max(took), 1),
        limit_ms=TYPES_LIMIT_MS,
    )
    assert max(took) <= TYPES_LIMIT_MS


def test_two_hundred_due_actions_are_reconciled_in_time(make_world: Callable[[], World]):
    world = make_world()
    made = actions.propose(
        world.twin,
        "set_pump",
        object=PUMP,
        params={"value": True},
        reason=None,
        source="timer",
        state="healthy",
        now=at(2),
        executor=Accepting(),
    )
    # The executed record's own row, under 199 more uids: as many records due at once.
    stored = world.store.get(made["uid"]).values
    copies = [{**stored, "uid": f"twin_action_record:timing{index:04d}"} for index in range(1, DUE)]
    register_rows(world.twin, {"TwinActionRecord": copies})
    observe(world.twin, at(3), {"Pump": [{"uid": PUMP, "running": True}]})
    assert len(actions.records(world.twin, status="executed", reconciliation="pending")) == DUE

    started = time.perf_counter()
    found = actions.reconcile(world.twin, now=at(4))
    took = time.perf_counter() - started
    report("reconcile", records=len(found), seconds=round(took, 3), limit_s=RECONCILE_LIMIT_S)
    assert len(found) == DUE
    assert {item.outcome for item in found} == {"ok"}
    assert took <= RECONCILE_LIMIT_S
