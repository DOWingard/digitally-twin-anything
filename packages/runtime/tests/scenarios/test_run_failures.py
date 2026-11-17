"""What a run is refused for, and how it fails: the record says so, the branch holds
nothing of an answer that was refused, and a second run starts from a fresh branch."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pyarrow as pa
import pytest
from dta_core.errors import DtaError
from dta_runtime import scenarios
from dta_runtime.models.errors import ModelError
from dta_runtime.scenarios import ScenarioError
from scenarios_testkit import (
    LAST,
    TIME,
    InProcess,
    World,
    branch_names,
    branch_versions,
    cells,
    create,
    grid,
    level,
    link,
    model,
    node,
    origins,
    prepared,
    setting,
)

STEPS = 8


def record(world: World, made: dict[str, Any]) -> dict[str, Any]:
    return scenarios.get(world.twin, made["uid"])


def flat(world: World) -> pa.Table:
    """A prediction of the right shape for the tanks: a level that stays where it is."""
    return prepared({node("T1"): lambda j: 5.0, node("T2"): lambda j: 6.0}, STEPS, world.base.t)


def giving(table: Any) -> Callable[..., Any]:
    def predict(record: Any, **asked: Any) -> Any:
        return table

    return predict


def failure(world: World, made: dict[str, Any], predict: Any, budget_s: float = 60.0) -> DtaError:
    with pytest.raises(DtaError) as caught:
        scenarios.run(world.twin, made["uid"], predict=predict, budget_s=budget_s)
    return caught.value


def succeeds_afresh(world: World, made: dict[str, Any], assumed_at_base: int = 0) -> None:
    """A second run of a failed scenario fills a branch made again from the base."""
    done = scenarios.run(world.twin, made["uid"], predict=giving(flat(world)), budget_s=60.0)
    assert (done["status"], done["error"], done["versions"]) == ("done", None, STEPS)
    added = branch_versions(world, made["branch"])
    assert [v.t for v in added] == [world.base.t] * assumed_at_base + [
        grid(j) for j in range(1, STEPS + 1)
    ]
    assert cells(world, "Tank", "level", "head", made["branch"])[node("T1")][0] == 5.0


# -- refusals ----------------------------------------------------------------------------


@pytest.mark.parametrize("uid", ["twin_scenario:000000000000", "nonsense", "node:T1", ""])
def test_a_scenario_that_is_not_there_is_not_found(world: World, predictor: InProcess, uid: str):
    for call in (
        lambda: scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0),
        lambda: scenarios.get(world.twin, uid),
        lambda: scenarios.delete(world.twin, uid),
        lambda: scenarios.compare(world.twin, uid, targets=["Tank.level"]),
    ):
        with pytest.raises(ScenarioError) as caught:
            call()
        assert caught.value.code == "scenario.not_found"
    assert predictor.calls == []


def test_a_scenario_that_is_running_is_neither_run_nor_deleted(world: World, predictor: InProcess):
    made = create(world, world.request())
    uid = made["uid"]
    seen: list[str] = []

    def reentering(record: Any, **asked: Any) -> pa.Table:
        # Asked while the run is under way, as a second request would.
        for call in (
            lambda: scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0),
            lambda: scenarios.delete(world.twin, uid),
        ):
            with pytest.raises(ScenarioError) as caught:
                call()
            seen.append(caught.value.code)
        seen.append(scenarios.get(world.twin, uid)["status"])
        return predictor(record, **asked)

    done = scenarios.run(world.twin, uid, predict=reentering, budget_s=60.0)
    assert seen == ["scenario.running", "scenario.running", "running"]
    assert done["status"] == "done" and len(predictor.calls) == 1
    assert len(branch_versions(world, made["branch"])) == STEPS


def test_a_scenario_is_run_once_and_not_after_it_is_deleted(world: World, predictor: InProcess):
    made = create(world, world.request())
    uid = made["uid"]
    scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0)
    heads = (world.store.head().id, world.store.head(made["branch"]).id)
    with pytest.raises(ScenarioError) as caught:
        scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0)
    assert caught.value.code == "scenario.already_run"
    assert caught.value.detail == {"scenario": uid, "status": "done"}
    assert (world.store.head().id, world.store.head(made["branch"]).id) == heads

    scenarios.delete(world.twin, uid)
    live = world.store.head().id
    with pytest.raises(ScenarioError) as caught:
        scenarios.run(world.twin, uid, predict=predictor, budget_s=60.0)
    assert caught.value.code == "scenario.deleted"
    assert world.store.head().id == live and made["branch"] not in branch_names(world)
    assert len(predictor.calls) == 1


# -- a predictor that raises -------------------------------------------------------------


def test_a_predictor_that_refuses_fails_the_run_with_its_own_code(world: World):
    made = create(world, world.request(interventions=[setting(7.5, grid(0))]))
    branch = made["branch"]
    live = world.store.head()
    first = world.store.head(branch).id
    refused = ModelError(
        "the state is of another family", code="model.bad_state", detail={"family": "ets"}
    )

    def refusing(record: Any, **asked: Any) -> pa.Table:
        raise refused

    error = failure(world, made, refusing)
    assert error is refused and error.code == "model.bad_state"

    found = record(world, made)
    assert found["status"] == "failed"
    assert found["error"] == "model.bad_state: the state is of another family"
    # Nothing was written to the branch, and the live line holds only the record.
    assert found["exists"] is True and world.store.head(branch).id == first
    moved = world.store.versions(f"#{live.seq + 1}..head")
    assert [(v.cause, v.extra["status"]) for v in moved] == [
        ("registration", "running"),
        ("registration", "failed"),
    ]
    assert {kind for kind, _ in origins(world)} == {"observed"}

    # The second run starts from a branch made again: the base, what is assumed there.
    succeeds_afresh(world, made, assumed_at_base=1)
    again = branch_versions(world, branch)
    assert again[0].id != first
    assert cells(world, "Valve", "setting", again[0].ref, branch)[link("V1")][0] == 7.5
    assert record(world, made)["error"] is None


def test_what_a_predictor_raises_outside_the_twins_errors_is_not_kept_in_the_record(world: World):
    made = create(world, world.request())

    def broken(record: Any, **asked: Any) -> pa.Table:
        raise RuntimeError("valve map missing: the secret word is swordfish")

    with pytest.raises(RuntimeError, match="swordfish"):
        scenarios.run(world.twin, made["uid"], predict=broken, budget_s=60.0)
    found = record(world, made)
    assert found["status"] == "failed"
    assert found["error"] == "scenario.error: RuntimeError was raised; the cause is in the log"
    assert branch_versions(world, made["branch"]) == []
    succeeds_afresh(world, made)


# -- a predictor that answers with something that cannot be written ------------------------


def rows(table: pa.Table) -> list[dict[str, Any]]:
    return table.to_pylist()


def rebuilt(found: list[dict[str, Any]]) -> pa.Table:
    return pa.table(
        {
            "unique_id": pa.array([row["unique_id"] for row in found], pa.string()),
            "ds": pa.array([row["ds"] for row in found], TIME),
            "mean": pa.array([row["mean"] for row in found], pa.float64()),
        }
    )


def changed(table: pa.Table, index: int, **cells_: Any) -> pa.Table:
    found = rows(table)
    found[index] = {**found[index], **cells_}
    return rebuilt(found)


def renamed(table: pa.Table, old: str, new: str) -> pa.Table:
    return rebuilt(
        [
            {**row, "unique_id": new if row["unique_id"] == old else row["unique_id"]}
            for row in rows(table)
        ]
    )


FIVE_MINUTES = timedelta(minutes=5)

BAD: list[tuple[str, Callable[[pa.Table], Any], str]] = [
    ("a list of rows", lambda t: t.to_pylist(), "gave a list, not a table"),
    ("nothing", lambda t: None, "gave a NoneType, not a table"),
    ("no mean", lambda t: t.drop_columns(["mean"]), "has no column mean"),
    (
        "no series and no time",
        lambda t: t.select(["mean"]),
        "has no column unique_id, ds",
    ),
    (
        "single-precision means",
        lambda t: t.set_column(2, "mean", t.column("mean").cast(pa.float32())),
        "mean is float, not double",
    ),
    (
        "times without a zone",
        lambda t: t.set_column(1, "ds", t.column("ds").cast(pa.timestamp("us"))),
        "ds is timestamp[us], not timestamp[us, tz=UTC]",
    ),
    (
        "times in milliseconds",
        lambda t: t.set_column(1, "ds", t.column("ds").cast(pa.timestamp("ms", tz="UTC"))),
        "ds is timestamp[ms, tz=UTC]",
    ),
    (
        "series named by numbers",
        lambda t: t.set_column(0, "unique_id", pa.array(range(t.num_rows), pa.int64())),
        "unique_id is int64, not string",
    ),
    ("no rows", lambda t: t.slice(0, 0), "holds no rows"),
    (
        "a series without a name",
        lambda t: changed(t, 3, unique_id=None),
        "1 rows have no unique_id",
    ),
    ("a row without a time", lambda t: changed(t, 3, ds=None), "1 rows have no ds"),
    (
        "a value that is missing",
        lambda t: changed(t, 3, mean=None),
        "1 predicted values are null or not finite, the first for node:T1",
    ),
    (
        "a value that is not a number",
        lambda t: changed(t, 11, mean=math.nan),
        "1 predicted values are null or not finite, the first for node:T2",
    ),
    (
        "a value that is infinite",
        lambda t: changed(changed(t, 0, mean=math.inf), 9, mean=-math.inf),
        "2 predicted values are null or not finite, the first for node:T1, node:T2",
    ),
    (
        "a time between two grid times",
        lambda t: changed(t, 2, ds=t.column("ds")[2].as_py() + FIVE_MINUTES),
        "1 rows are at times that are not the base time plus 1 to 8 steps of 900 s: "
        "2026-01-05T06:50:00+00:00",
    ),
    (
        "a value for the base time",
        lambda t: changed(t, 0, ds=grid(0)),
        "not the base time plus 1 to 8 steps",
    ),
    (
        "a value after the horizon",
        lambda t: changed(t, 7, ds=grid(9)),
        "not the base time plus 1 to 8 steps",
    ),
    (
        "a value given twice",
        lambda t: rebuilt([*rows(t), rows(t)[4]]),
        "1 pairs of series and time are given more than once, the first for node:T1",
    ),
    (
        "one time twice in place of another",
        lambda t: changed(t, 5, ds=grid(5)),
        "1 pairs of series and time are given more than once, the first for node:T1",
    ),
    (
        "a series that stops early",
        lambda t: t.slice(0, t.num_rows - 1),
        "1 series do not have a value at each of the 8 steps, the first node:T2",
    ),
    (
        "a series of an object that is not there",
        lambda t: renamed(t, node("T2"), node("T9")),
        "1 series are of objects that are not Tank on the scenario's branch: node:T9",
    ),
    (
        "a series of an object of another type",
        lambda t: renamed(t, node("T1"), node("J1")),
        "1 series are of objects that are not Tank on the scenario's branch: node:J1",
    ),
]


@pytest.mark.parametrize(
    ("spoil", "said"), [pytest.param(spoil, said, id=name) for name, spoil, said in BAD]
)
def test_an_answer_that_cannot_be_written_fails_the_run_before_anything_is_written(
    world: World, spoil: Callable[[pa.Table], Any], said: str
):
    made = create(world, world.request())
    error = failure(world, made, giving(spoil(flat(world))))
    assert error.code == "scenario.bad_prediction"
    assert said in error.message, error.message
    assert error.message.startswith(f"{model('level-naive')}: ")
    assert error.detail["model"] == model("level-naive")

    found = record(world, made)
    assert found["status"] == "failed"
    assert found["error"] == f"scenario.bad_prediction: {error.message}"
    assert branch_versions(world, made["branch"]) == []
    succeeds_afresh(world, made)


def test_further_columns_of_an_answer_are_let_through_unread(world: World):
    made = create(world, world.request())
    table = flat(world)
    wide = table.append_column("lo_80", table.column("mean")).append_column(
        "hi_80", table.column("mean")
    )
    done = scenarios.run(world.twin, made["uid"], predict=giving(wide), budget_s=60.0)
    assert done["status"] == "done"
    # Rows in any order are the same answer.
    again = create(world, world.request())
    shuffled = table.take(pa.array(list(reversed(range(table.num_rows)))))
    scenarios.run(world.twin, again["uid"], predict=giving(shuffled), budget_s=60.0)
    for j, version in enumerate(branch_versions(world, again["branch"]), start=1):
        assert version.t == grid(j)
        assert cells(world, "Tank", "level", version.ref, again["branch"])[node("T2")][0] == 6.0


def test_no_answer_is_written_unless_every_model_answered_well(world: World, predictor: InProcess):
    made = create(world, world.request(models=("level-naive", "demand-last")))
    asked: list[str] = []

    def second_is_bad(record: Any, **more: Any) -> pa.Table:
        asked.append(record["uid"])
        if record["uid"] == model("demand-last"):
            return flat(world).slice(0, 3)
        return predictor(record, **more)

    error = failure(world, made, second_is_bad)
    assert error.code == "scenario.bad_prediction" and error.detail["model"] == model("demand-last")
    assert asked == [model("level-naive"), model("demand-last")]
    assert branch_versions(world, made["branch"]) == []


def test_a_version_the_store_refuses_fails_the_run_and_what_was_written_stays(world: World):
    made = create(world, world.request())
    branch = made["branch"]
    # The third level is above the tank's ceiling: a rule the database holds.
    too_high = prepared(
        {node("T1"): lambda j: 25.0 if j == 3 else 5.0, node("T2"): lambda j: 6.0},
        STEPS,
        world.base.t,
    )
    error = failure(world, made, giving(too_high))
    assert error.code == "store.violation"
    found = record(world, made)
    assert found["status"] == "failed" and found["error"].startswith("store.violation: ")
    left = branch_versions(world, branch)
    assert [v.t for v in left] == [grid(1), grid(2)]
    assert found["head"] == {"seq": world.base.seq + 2, "t": grid(2)}

    succeeds_afresh(world, made)
    assert branch_versions(world, branch)[0].id != left[0].id


# -- time ----------------------------------------------------------------------------------


def test_a_budget_that_runs_out_between_two_models_fails_the_run(
    world: World, predictor: InProcess
):
    made = create(world, world.request(models=("level-naive", "demand-last")))
    uid = made["uid"]

    def slow(record: Any, **asked: Any) -> pa.Table:
        time.sleep(0.4)
        return predictor(record, **asked)

    error = failure(world, made, slow, budget_s=0.3)
    assert error.code == "scenario.timeout"
    assert error.detail == {
        "scenario": uid,
        "model": model("demand-last"),
        "answered": 1,
        "budget_s": 0.3,
    }
    # The first was asked with what was left of the budget; the second never.
    assert [call["model"] for call in predictor.calls] == [model("level-naive")]
    assert 0 < predictor.calls[0]["deadline_s"] <= 0.3
    found = record(world, made)
    assert found["status"] == "failed" and found["error"].startswith("scenario.timeout: ")
    assert branch_versions(world, made["branch"]) == []

    # No time at all: not even the first is asked.
    error = failure(world, made, slow, budget_s=0.0)
    assert error.code == "scenario.timeout" and error.detail["answered"] == 0
    assert len(predictor.calls) == 1

    done = scenarios.run(world.twin, uid, predict=slow, budget_s=60.0)
    assert done["status"] == "done" and done["duration_ms"] >= 800.0
    assert len(branch_versions(world, made["branch"])) == STEPS
    assert cells(world, "Tank", "level", "head", made["branch"])[node("T1")][0] == level(
        "T1", LAST + STEPS
    )


def test_a_failed_scenario_whose_branch_is_gone_is_run_from_a_new_one(world: World):
    made = create(world, world.request(interventions=[setting(7.5, grid(0))]))
    # Created, and its branch removed behind its back: the run finds nothing to fill.
    world.store.branch_delete(made["branch"])
    error = failure(world, made, giving(flat(world)))
    assert error.code == "store.unknown_branch"
    found = record(world, made)
    assert (found["status"], found["exists"]) == ("failed", False)
    succeeds_afresh(world, made, assumed_at_base=1)
