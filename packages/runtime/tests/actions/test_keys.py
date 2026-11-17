"""A record is written before its call is made: a process that stops in between leaves
the record, and the next proposal under the same key (or the next approval) completes
it with the same idempotency key."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from actions_testkit import PUMP, VALVE, Died, Dying, Plant, World, at, executor
from dta_runtime import actions
from dta_runtime.actions import ActionError, HttpExecutor

NOW = at(2)


@pytest.fixture(scope="module")
def http(world: World, plant: Plant) -> Iterator[HttpExecutor]:
    found = executor(world.manifest)
    yield found
    found.close()


def send(world: World, sender: Any, key: str, **more: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "object": PUMP,
        "params": {"value": True},
        "reason": "keys test",
        "source": "tester",
        "state": "healthy",
        "now": NOW,
        "executor": sender,
        "key": key,
    }
    options.update(more)
    return actions.propose(world.twin, options.pop("action", "set_pump"), **options)


def test_a_process_that_stops_after_the_call_leaves_a_record_its_key_completes(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    with pytest.raises(Died):
        send(world, Dying(http), "run-7:0")
    # The record a key names is known from the key alone.
    left = actions.record(world.twin, hashlib.sha256(b"run-7:0").hexdigest()[:32])
    assert left["status"] == "approved"
    assert left["execution"] is None
    assert len(fresh_plant.received) == 1
    first_key = fresh_plant.received[0].headers["Idempotency-Key"]
    assert first_key == left["id"]

    done = send(world, http, "run-7:0")
    assert done["uid"] == left["uid"]
    assert done["created"] is False
    assert done["status"] == "executed"
    assert done["execution"]["idempotency_key"] == first_key
    assert len(fresh_plant.received) == 2
    assert fresh_plant.received[1].headers["Idempotency-Key"] == first_key
    assert actions.record(world.twin, left["uid"])["status"] == "executed"


def test_an_approval_that_stops_after_the_call_is_completed_by_the_next(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    made = send(world, http, "valve-a", action="set_valve", object=VALVE, params={"value": 9.0})
    assert made["status"] == "proposed"
    options = {"by": "operator", "note": None, "state": "healthy", "now": NOW}
    with pytest.raises(Died):
        actions.approve(world.twin, made["uid"], executor=Dying(http), **options)
    assert actions.record(world.twin, made["uid"])["status"] == "approved"
    done = actions.approve(world.twin, made["uid"], executor=http, **options)
    assert done["status"] == "executed"
    keys = [found.headers["Idempotency-Key"] for found in fresh_plant.received]
    assert keys == [made["id"], made["id"]]


def test_the_same_key_gives_the_same_record_and_does_nothing_again(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    first = send(world, http, "once")
    head = world.store.head()
    again = send(world, http, "once")
    assert again["uid"] == first["uid"]
    assert again["created"] is False
    assert again["status"] == first["status"] == "executed"
    assert len(fresh_plant.received) == 1
    assert world.store.head() == head


def test_the_same_key_for_another_proposal_is_refused(world: World, http: HttpExecutor):
    send(world, None, "taken")
    for changed in ({"params": {"value": False}}, {"object": "link:PMP-11"}, {"source": "other"}):
        with pytest.raises(ActionError) as caught:
            send(world, None, "taken", **changed)
        assert caught.value.code == "action.key_reused"


def test_a_record_left_approved_stays_so_while_nothing_can_send_it(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    with pytest.raises(Died):
        send(world, Dying(http), "no-sender")
    left = send(world, None, "no-sender")
    assert left["status"] == "approved"
    assert left["created"] is False
