"""Reconciliation: an executed action is compared, once it is due, with the value
observed in force afterwards."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from actions_testkit import PUMP, VALVE, Accepting, World, at, infer, observe
from dta_runtime import actions


def executed(world: World, now: datetime, sender: Accepting | None = None, **more: Any) -> dict:
    """An executed record: a pump started, or a valve set and approved."""
    sender = sender or Accepting()
    options: dict[str, Any] = {
        "object": PUMP,
        "params": {"value": True},
        "reason": None,
        "source": "tester",
        "state": "healthy",
        "now": now,
        "executor": sender,
    }
    options.update(more)
    action = options.pop("action", "set_pump")
    made = actions.propose(world.twin, action, **options)
    if made["status"] == "proposed":
        made = actions.approve(
            world.twin,
            made["uid"],
            by="operator",
            note=None,
            state="healthy",
            now=now,
            executor=sender,
        )
    assert made["status"] == "executed"
    assert made["reconciliation"] == "pending"
    return made


def test_an_action_is_reconciled_once_it_is_due_and_once_only(make_world: Callable[[], World]):
    world = make_world()
    made = executed(world, at(2))
    observe(world.twin, at(3), {"Pump": [{"uid": PUMP, "running": True}]})
    # Due 30 minutes (twice the sync interval) after it took effect.
    assert actions.reconcile(world.twin, now=at(3.9)) == []
    assert actions.record(world.twin, made["uid"])["reconciliation"] == "pending"

    before = len(world.store.versions())
    (found,) = actions.reconcile(world.twin, now=at(4))
    assert len(world.store.versions()) == before + 1
    assert found.as_dict() == {
        "record": made["uid"],
        "action": "set_pump",
        "control": "pump_running",
        "object": PUMP,
        "target": "Pump.running",
        "value": True,
        "tolerance": 0.0,
        "outcome": "ok",
        "observed": True,
        "observed_at": at(3),
        "observed_source": "scada",
        "effective_at": at(2),
        "reconciled_at": at(4),
    }
    stored = actions.record(world.twin, made["uid"])
    assert stored["reconciliation"] == "ok"
    assert stored["reconciled_at"] == at(4)
    assert stored["observed"]["value"] is True
    assert stored["observed"]["kind"] == "observed"
    assert stored["observed"]["t"] == "2026-01-05T00:45:00Z"

    head = world.store.head()
    assert actions.reconcile(world.twin, now=at(9)) == []
    assert world.store.head() == head


def test_an_action_whose_effect_is_not_observed_is_a_mismatch(make_world: Callable[[], World]):
    world = make_world()
    made = executed(world, at(2))
    # The pump was seen stopped before and nothing newer was seen.
    (found,) = actions.reconcile(world.twin, now=at(4))
    assert (found.outcome, found.observed, found.observed_at) == ("mismatch", False, at(1))
    assert actions.record(world.twin, made["uid"])["reconciliation"] == "mismatch"


def test_a_value_within_the_tolerance_is_ok_and_one_outside_it_a_mismatch(
    make_world: Callable[[], World],
):
    world = make_world()
    first = executed(world, at(2), action="set_valve", object=VALVE, params={"value": 10.0})
    observe(world.twin, at(3), {"Valve": [{"uid": VALVE, "setting": 10.4}]})
    # set_valve settles in 45 minutes.
    assert actions.reconcile(world.twin, now=at(4.9)) == []
    (found,) = actions.reconcile(world.twin, now=at(5))
    assert (found.record, found.outcome, found.tolerance) == (first["uid"], "ok", 0.5)
    assert actions.record(world.twin, first["uid"])["observed"]["difference"] == pytest.approx(0.4)

    second = executed(world, at(5), action="set_valve", object=VALVE, params={"value": 20.0})
    observe(world.twin, at(6), {"Valve": [{"uid": VALVE, "setting": 20.6}]})
    (found,) = actions.reconcile(world.twin, now=at(8))
    assert (found.record, found.outcome, found.observed) == (second["uid"], "mismatch", 20.6)


def test_an_inferred_value_in_force_does_not_count(make_world: Callable[[], World]):
    world = make_world()
    made = executed(world, at(2))
    infer(world.twin, at(3), {"Pump": [{"uid": PUMP, "running": True}]})
    assert actions.reconcile(world.twin, now=at(4)) == []
    assert actions.record(world.twin, made["uid"])["reconciliation"] == "pending"
    observe(world.twin, at(4), {"Pump": [{"uid": PUMP, "running": True}]})
    (found,) = actions.reconcile(world.twin, now=at(4))
    assert (found.outcome, found.observed_source) == ("ok", "scada")


def test_the_effective_time_the_system_gave_is_what_settling_counts_from(
    make_world: Callable[[], World],
):
    world = make_world()
    made = executed(world, at(2), sender=Accepting(effective_at=at(3)))
    assert made["effective_at"] == at(3)
    observe(world.twin, at(3), {"Pump": [{"uid": PUMP, "running": True}]})
    assert actions.reconcile(world.twin, now=at(4)) == []
    (found,) = actions.reconcile(world.twin, now=at(5))
    assert found.effective_at == at(3)
