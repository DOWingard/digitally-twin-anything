"""Reading a scenario's record, listing the scenarios and removing one."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dta_runtime import scenarios
from dta_runtime.scenarios import ScenarioError
from scenarios_testkit import (
    STEP,
    InProcess,
    World,
    branch_names,
    branch_versions,
    commit,
    create,
    grid,
    node,
    origins,
    setting,
)

STEPS = 8


def record_of(made: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in made.items() if key != "created"}


def refused(world: World) -> Callable[..., Any]:
    def predict(record: Any, **asked: Any) -> Any:
        raise ScenarioError("the worker is away", code="worker.unavailable")

    return predict


# -- get ---------------------------------------------------------------------------------


def test_get_gives_the_record_and_where_its_branch_is(world: World, predictor: InProcess):
    base = world.base
    made = create(world, world.request(description="a dry week"))
    uid = made["uid"]
    created_at = world.store.head()
    assert scenarios.get(world.twin, uid) == {
        **record_of(made),
        "exists": True,
        "head": {"seq": base.seq, "t": base.t},
    }

    scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0)
    found = scenarios.get(world.twin, uid)
    assert found == {
        **record_of(made),
        "status": "done",
        "exists": True,
        "head": {"seq": base.seq + STEPS, "t": grid(STEPS)},
    }
    assert found["id"] == uid.split(":")[1]

    # At an earlier version of the live line, the record is what it was then; where
    # the branch is, is asked of the branch as it is now.
    earlier = scenarios.get(world.twin, uid, at=created_at)
    assert earlier["status"] == "created" and earlier["head"] == found["head"]
    assert scenarios.get(world.twin, uid, at=f"#{created_at.seq}")["status"] == "created"
    with pytest.raises(ScenarioError) as caught:
        scenarios.get(world.twin, uid, at=base)
    assert caught.value.code == "scenario.not_found"
    assert caught.value.detail == {"scenario": uid}


# -- delete ------------------------------------------------------------------------------


def test_delete_removes_the_branch_and_keeps_the_record(world: World, predictor: InProcess):
    made = create(world, world.request(interventions=[setting(7.5, grid(0))]))
    uid, branch = made["uid"], made["branch"]
    scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0)
    live = world.store.head()

    gone = scenarios.delete(world.twin, uid)
    assert gone == {**record_of(made), "status": "deleted", "exists": False}
    assert branch not in branch_names(world)
    assert world.store.get(uid).values["status"] == "deleted"
    moved = world.store.versions(f"#{live.seq + 1}..head")
    assert [(v.cause, v.actor, v.t, v.extra) for v in moved] == [
        ("registration", "scenarios", live.t, {"scenario": uid, "status": "deleted"})
    ]
    assert scenarios.get(world.twin, uid) == gone
    # What the branch held went with it: the live line never had any of it.
    assert {kind for kind, _ in origins(world)} == {"observed"}

    # Deleting it again changes nothing.
    head = world.store.head().id
    assert scenarios.delete(world.twin, uid) == gone
    assert world.store.head().id == head


def test_a_scenario_is_deleted_whatever_became_of_it(world: World):
    # Never run.
    made = create(world, world.request())
    assert scenarios.delete(world.twin, made["uid"])["status"] == "deleted"
    assert made["branch"] not in branch_names(world)

    # Failed: the record goes on saying why.
    failed = create(world, world.request())
    with pytest.raises(ScenarioError):
        scenarios.run(world.twin, failed["uid"], predict=refused(world), budget_s=60.0)
    gone = scenarios.delete(world.twin, failed["uid"])
    assert (gone["status"], gone["error"]) == ("deleted", "worker.unavailable: the worker is away")

    # Its branch already gone.
    orphan = create(world, world.request())
    world.store.branch_delete(orphan["branch"])
    assert scenarios.get(world.twin, orphan["uid"])["exists"] is False
    assert scenarios.delete(world.twin, orphan["uid"])["status"] == "deleted"


# -- list --------------------------------------------------------------------------------


def test_the_scenarios_are_listed_by_base_time_and_then_name(make_world: Callable[[], World]):
    world = make_world()
    assert scenarios.list_all(world.twin) == []
    assert scenarios.list_all(world.twin, include_deleted=True) == []

    beta = create(world, world.request(name="beta"))
    alpha = create(world, world.request(name="alpha", interventions=[setting(7.5, grid(0))]))
    # One that starts from a later version of the live line.
    later = commit(world.twin, grid(1), {"Tank": [{"uid": node("T1"), "level": 4.2}]})
    early = scenarios.create(world.twin, world.request(name="aardvark"), at=later, step=STEP)
    assert early["base_t"] == grid(1)

    listed = scenarios.list_all(world.twin)
    assert [found["name"] for found in listed] == ["alpha", "beta", "aardvark"]
    assert listed == [record_of(alpha), record_of(beta), record_of(early)]
    assert [found["id"] for found in listed] == [alpha["id"], beta["id"], early["id"]]
    # Each is what `get` gives of the record.
    for found in listed:
        got = scenarios.get(world.twin, found["uid"])
        assert {key: got[key] for key in found} == found

    scenarios.delete(world.twin, beta["uid"])
    assert [found["name"] for found in scenarios.list_all(world.twin)] == ["alpha", "aardvark"]
    everything = scenarios.list_all(world.twin, include_deleted=True)
    assert [(found["name"], found["status"]) for found in everything] == [
        ("alpha", "created"),
        ("beta", "deleted"),
        ("aardvark", "created"),
    ]
    assert len(branch_versions(world, alpha["branch"])) == 1
