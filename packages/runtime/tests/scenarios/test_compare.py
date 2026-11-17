"""A scenario beside the live line and beside another scenario: what was in force on
each side at each grid time, nothing where a side has not got to yet, and the reads
it costs.

After the base, the live line of this module went on (times are grid times):

    1     T1 level 4.2, J1 demand 35.0
    2     T1 level 4.4
    2.6   T1 level 4.9        (between two grid times: in force at 3)
    4     T2 level 8.0        (the live head: nothing is known after 4)
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from dta_runtime import scenarios
from dta_runtime.scenarios import ScenarioError
from scenarios_testkit import (
    INFLOW,
    LAST,
    OUTFLOW,
    STEP,
    TIME,
    Counted,
    InProcess,
    World,
    closed,
    commit,
    create,
    demand,
    grid,
    level,
    node,
    prepared,
    setting,
)

STEPS = 8
LIVE_HEAD = 4
SCHEMA = pa.schema(
    [
        pa.field("target", pa.string()),
        pa.field("uid", pa.string()),
        pa.field("t", TIME),
        pa.field("scenario", pa.float64()),
        pa.field("against", pa.float64()),
        pa.field("difference", pa.float64()),
    ]
)


def live_level(key: str, j: int) -> float | None:
    """The level in force on the live line at grid time j; None after its head."""
    if j > LIVE_HEAD:
        return None
    if key == "T1":
        return [4.0, 4.2, 4.4, 4.9, 4.9][j]
    return 8.0 if j == 4 else 9.0


def live_demand(key: str, j: int) -> float | None:
    if j > LIVE_HEAD:
        return None
    if key == "J2":
        return 20.0
    return 34.0 if j == 0 else 35.0


def naive_level(key: str, j: int) -> float:
    """What level-naive predicts for grid time j; at the base, what was observed."""
    return level(key, LAST + j)


def balanced(key: str, j: int, closed_steps: int = 0) -> float:
    """What level-balance predicts: INFLOW a step while the valve is open, less OUTFLOW."""
    return level(key, LAST) + INFLOW * (j - closed_steps) - OUTFLOW * j


def expected(target: str, uid: str, ours: Any, theirs: Any, steps: int = STEPS) -> list[tuple]:
    rows = []
    for j in range(steps + 1):
        a, b = ours(j), theirs(j)
        rows.append((target, uid, grid(j), a, b, None if a is None or b is None else a - b))
    return rows


def same(table: pa.Table, rows: list[tuple]) -> None:
    assert table.schema == SCHEMA
    found = [tuple(row.values()) for row in table.to_pylist()]
    assert len(found) == len(rows)
    for got, wanted in zip(found, rows, strict=True):
        assert got[:3] == wanted[:3], (got, wanted)
        for a, b in zip(got[3:], wanted[3:], strict=True):
            assert (a is None and b is None) or a == pytest.approx(b, abs=1e-9), (got, wanted)


@pytest.fixture(scope="module")
def ran(world: World) -> dict[str, dict[str, Any]]:
    """The scenarios of the module, run before the live line went on."""
    predictor = InProcess(world)
    made = {
        "naive": create(world, world.request(models=("level-naive", "demand-last"))),
        "plain": create(world, world.request(models=("level-balance",))),
        "shut": create(
            world,
            world.request(models=("level-balance",), interventions=[closed(grid(2.5), grid(6))]),
        ),
        "set": create(world, world.request(interventions=[setting(7.5, grid(2.4), grid(5.2))])),
    }
    for found in made.values():
        scenarios.run(world.twin, found["uid"], predict=predictor, budget_s=60.0)
    made["waiting"] = create(world, world.request(models=("level-balance",)))
    commit(
        world.twin,
        grid(1),
        {
            "Tank": [{"uid": node("T1"), "level": 4.2}],
            "Junction": [{"uid": node("J1"), "demand": 35.0}],
        },
    )
    commit(world.twin, grid(2), {"Tank": [{"uid": node("T1"), "level": 4.4}]})
    commit(world.twin, grid(2.6), {"Tank": [{"uid": node("T1"), "level": 4.9}]})
    commit(world.twin, grid(LIVE_HEAD), {"Tank": [{"uid": node("T2"), "level": 8.0}]})
    return made


def test_a_scenario_beside_the_live_line_that_went_on_after_the_base(world: World, ran):
    table = scenarios.compare(
        world.twin, ran["naive"]["uid"], targets=["Tank.level", "Junction.demand"]
    )
    same(
        table,
        [
            *expected(
                "Junction.demand",
                node("J1"),
                lambda j: demand("J1", LAST),
                lambda j: live_demand("J1", j),
            ),
            *expected(
                "Junction.demand", node("J2"), lambda j: 20.0, lambda j: live_demand("J2", j)
            ),
            *expected(
                "Tank.level",
                node("T1"),
                lambda j: naive_level("T1", j),
                lambda j: live_level("T1", j),
            ),
            *expected(
                "Tank.level",
                node("T2"),
                lambda j: naive_level("T2", j),
                lambda j: live_level("T2", j),
            ),
        ],
    )
    # The live line is known up to its head and no further: null, not the last value.
    against = table.column("against").to_pylist()
    assert against[LIVE_HEAD] == 35.0 and against[LIVE_HEAD + 1 :][:4] == [None] * 4
    assert table.column("difference").null_count == 4 * (STEPS - LIVE_HEAD)
    assert table.column("scenario").null_count == 0
    # Naming the live branch is the same as naming nothing.
    assert scenarios.compare(
        world.twin, ran["naive"]["uid"], targets=["Tank.level", "Junction.demand"], against="main"
    ).equals(table)


def test_a_live_line_that_went_on_past_the_horizon_is_read_up_to_its_end(
    world: World, ran, predictor: InProcess
):
    short = create(world, world.request(horizon=2 * STEP))
    scenarios.run(world.twin, short["uid"], predict=predictor, budget_s=60.0)
    with Counted(world) as counted:
        table = scenarios.compare(world.twin, short["uid"], targets=["Tank.level"])
    same(
        table,
        [
            *expected(
                "Tank.level",
                node("T1"),
                lambda j: naive_level("T1", j),
                lambda j: live_level("T1", j),
                steps=2,
            ),
            *expected(
                "Tank.level",
                node("T2"),
                lambda j: naive_level("T2", j),
                lambda j: live_level("T2", j),
                steps=2,
            ),
        ],
    )
    # Every grid time is known on both sides, and what the live line did later is not
    # what was in force then.
    assert table.column("difference").null_count == 0
    assert (counted.calls["table"], counted.calls["changes"]) == (2, 2)


def test_a_scenario_beside_another_of_the_same_base(world: World, ran):
    shut, plain = ran["shut"], ran["plain"]
    table = scenarios.compare(world.twin, shut["uid"], targets=["Tank.level"], against=plain["uid"])
    closed_by = [0, 0, 0, 1, 2, 3, 3, 3, 3]
    same(
        table,
        [
            *expected(
                "Tank.level",
                node("T1"),
                lambda j: balanced("T1", j, closed_by[j]),
                lambda j: balanced("T1", j),
            ),
            *expected(
                "Tank.level", node("T2"), lambda j: balanced("T2", j), lambda j: balanced("T2", j)
            ),
        ],
    )
    # By hand: every step the valve is closed costs the tank one inflow.
    assert table.column("difference").to_pylist()[: STEPS + 1] == pytest.approx(
        [0.0, 0.0, 0.0, -0.5, -1.0, -1.5, -1.5, -1.5, -1.5]
    )
    assert table.column("difference").to_pylist()[STEPS + 1 :] == pytest.approx([0.0] * (STEPS + 1))
    # The other scenario is named by its uid or by its branch.
    assert scenarios.compare(
        world.twin, shut["uid"], targets=["Tank.level"], against=plain["branch"]
    ).equals(table)
    # The other way round, the difference changes sign.
    back = scenarios.compare(world.twin, plain["uid"], targets=["Tank.level"], against=shut["uid"])
    assert back.column("difference").to_pylist() == pytest.approx(
        [-found for found in table.column("difference").to_pylist()]
    )


def test_only_the_objects_asked_for(world: World, ran):
    table = scenarios.compare(
        world.twin,
        ran["shut"]["uid"],
        targets=["Tank.level", "Junction.demand"],
        against=ran["plain"]["uid"],
        uids=[node("T1"), node("J2")],
    )
    series = table.select(["target", "uid"]).group_by(["target", "uid"]).aggregate([])
    assert sorted(tuple(row.values()) for row in series.to_pylist()) == [
        ("Junction.demand", node("J2")),
        ("Tank.level", node("T1")),
    ]
    assert table.num_rows == 2 * (STEPS + 1)
    # No model of these scenarios predicts the demand: it stays as it was at the base.
    assert table.column("scenario").to_pylist()[: STEPS + 1] == [20.0] * (STEPS + 1)
    none = scenarios.compare(world.twin, ran["shut"]["uid"], targets=["Tank.level"], uids=[])
    assert none.schema == SCHEMA and none.num_rows == 0
    absent = scenarios.compare(
        world.twin, ran["shut"]["uid"], targets=["Tank.level"], uids=[node("T9")]
    )
    assert absent.schema == SCHEMA and absent.num_rows == 0


def test_a_side_that_has_not_been_filled_is_null_after_its_head(world: World, ran):
    # A scenario that was made and not run knows its base and nothing after it.
    waiting = ran["waiting"]
    table = scenarios.compare(world.twin, waiting["uid"], targets=["Tank.level"])
    same(
        table,
        [
            *expected(
                "Tank.level",
                node("T1"),
                lambda j: level("T1", LAST) if j == 0 else None,
                lambda j: live_level("T1", j),
            ),
            *expected(
                "Tank.level",
                node("T2"),
                lambda j: level("T2", LAST) if j == 0 else None,
                lambda j: live_level("T2", j),
            ),
        ],
    )
    other = scenarios.compare(
        world.twin, ran["plain"]["uid"], targets=["Tank.level"], against=waiting["uid"]
    )
    assert other.column("against").to_pylist() == [4.0, *[None] * STEPS, 9.0, *[None] * STEPS]
    assert other.column("scenario").null_count == 0

    # One that failed on the way shows what it got to.
    failed = create(world, world.request())
    too_high = prepared(
        {node("T1"): lambda j: 25.0 if j == 3 else 5.0, node("T2"): lambda j: 6.0},
        STEPS,
        world.base.t,
    )
    with pytest.raises(Exception) as caught:
        scenarios.run(world.twin, failed["uid"], predict=lambda *a, **k: too_high, budget_s=60.0)
    assert caught.value.code == "store.violation"
    partial = scenarios.compare(
        world.twin, failed["uid"], targets=["Tank.level"], uids=[node("T1")]
    )
    assert partial.column("scenario").to_pylist() == [4.0, 5.0, 5.0, *[None] * (STEPS - 2)]


def test_what_is_assumed_has_a_trajectory_too(world: World, ran):
    table = scenarios.compare(world.twin, ran["set"]["uid"], targets=["Valve.setting"])
    v1 = [row for row in table.to_pylist() if row["uid"] == "link:V1"]
    assert [row["scenario"] for row in v1] == [1.0, 1.0, 1.0, 7.5, 7.5, 7.5, 1.0, 1.0, 1.0]
    assert [row["against"] for row in v1] == [1.0] * (LIVE_HEAD + 1) + [None] * (STEPS - LIVE_HEAD)
    assert [row["difference"] for row in v1][: LIVE_HEAD + 1] == [0.0, 0.0, 0.0, 6.5, 6.5]


def test_a_class_stands_for_its_types_and_whole_numbers_are_numbers(world: World, ran):
    table = scenarios.compare(world.twin, ran["naive"]["uid"], targets=["Node.elevation"])
    assert table.column("target").unique().to_pylist() == ["Node.elevation"]
    assert table.column("uid").unique().to_pylist() == [
        node("J1"),
        node("J2"),
        node("T1"),
        node("T2"),
    ]
    assert table.num_rows == 4 * (STEPS + 1)
    assert set(table.column("scenario").to_pylist()) == {40.0, 60.0}


def test_two_reads_of_the_store_per_type_and_side_whatever_the_horizon(world: World, ran):
    uid = ran["naive"]["uid"]
    with Counted(world) as counted:
        scenarios.compare(world.twin, uid, targets=["Tank.level", "Junction.demand"])
    # Two types on two sides: the state at the base and the changes after it, each
    # once. One more read for the scenario's record.
    assert counted.calls == {**dict.fromkeys(Counted.READS, 0), "table": 4, "changes": 4, "get": 1}

    # Two properties of one type cost what one costs.
    with Counted(world) as counted:
        scenarios.compare(world.twin, uid, targets=["Tank.level", "Tank.min_level"])
    assert (counted.calls["table"], counted.calls["changes"]) == (2, 2)

    # A class costs its concrete types; another scenario's record is one more read.
    with Counted(world) as counted:
        scenarios.compare(
            world.twin, ran["shut"]["uid"], targets=["Node.elevation"], against=ran["plain"]["uid"]
        )
    assert (counted.calls["table"], counted.calls["changes"], counted.calls["get"]) == (6, 6, 2)

    # Twelve times the horizon: the same reads.
    long = create(world, world.request(horizon=96 * STEP, models=("level-naive", "demand-last")))
    tanks = prepared({node(key): lambda j: 5.0 + j / 100 for key in ("T1", "T2")}, 96, world.base.t)
    junctions = prepared({node(key): lambda j: 30.0 for key in ("J1", "J2")}, 96, world.base.t)

    def predict(record: Any, **asked: Any) -> pa.Table:
        return tanks if record["target"] == "Tank.level" else junctions

    assert scenarios.run(world.twin, long["uid"], predict=predict, budget_s=120.0)["versions"] == 96
    with Counted(world) as counted:
        table = scenarios.compare(
            world.twin, long["uid"], targets=["Tank.level", "Junction.demand"]
        )
    assert counted.calls == {**dict.fromkeys(Counted.READS, 0), "table": 4, "changes": 4, "get": 1}
    assert table.num_rows == 4 * 97
    # The tanks come after the two junctions; the last row of the first tank.
    assert table.column("scenario").to_pylist()[2 * 97 + 96] == pytest.approx(5.96)


# -- refusals ----------------------------------------------------------------------------


def refusal(world: World, uid: str, **asked: Any) -> ScenarioError:
    with pytest.raises(ScenarioError) as caught:
        scenarios.compare(world.twin, uid, **{"targets": ["Tank.level"], **asked})
    return caught.value


@pytest.mark.parametrize(
    ("targets", "members", "said"),
    [
        ([], ["targets"], "at least one Type.property"),
        ("Tank.level", ["targets"], "at least one Type.property"),
        (["Valve.status"], ["targets[0]"], "Valve.status holds enum values"),
        (["Tank.volume_curve"], ["targets[0]"], "holds lists of float values"),
        (["Tank.level", "Tank.depth"], ["targets[1]"], "Tank has no property 'depth'"),
        (["level", "Basin.level"], ["targets[0]", "targets[1]"], "not written Type.property"),
    ],
)
def test_targets_that_have_no_trajectory_are_refused(world: World, ran, targets, members, said):
    error = refusal(world, ran["naive"]["uid"], targets=targets)
    assert error.code == "scenario.bad_request"
    assert [found["member"] for found in error.detail["problems"]] == members
    assert said in error.detail["problems"][0]["message"]


def test_the_other_side_is_the_live_line_or_a_scenario_of_the_same_base(world: World, ran):
    uid = ran["naive"]["uid"]
    for against in ("nowhere", "what-if", "", None, 7):
        error = refusal(world, uid, against=against)
        assert error.code == "scenario.bad_request"
        assert [found["member"] for found in error.detail["problems"]] == ["against"]

    error = refusal(world, uid, against="twin_scenario:000000000000")
    assert error.code == "scenario.not_found"
    error = refusal(world, uid, against="scenario-000000000000")
    assert error.code == "scenario.not_found"

    # A scenario that starts at a later version of the live line.
    later = scenarios.create(world.twin, world.request(), at=world.store.head(), step=STEP)
    assert later["base_version"] != ran["naive"]["base_version"]
    for against in (later["uid"], later["branch"]):
        error = refusal(world, uid, against=against)
        assert error.code == "scenario.bad_request"
        assert [found["member"] for found in error.detail["problems"]] == ["against"]
        assert "scenarios are compared from one base" in error.detail["problems"][0]["message"]


def test_a_deleted_scenario_is_compared_with_nothing(world: World, ran):
    gone = create(world, world.request())
    scenarios.delete(world.twin, gone["uid"])
    assert refusal(world, gone["uid"]).code == "scenario.deleted"
    error = refusal(world, ran["naive"]["uid"], against=gone["uid"])
    assert error.code == "scenario.deleted" and error.detail == {"scenario": gone["uid"]}
    assert refusal(world, ran["naive"]["uid"], against=gone["branch"]).code == "scenario.deleted"
