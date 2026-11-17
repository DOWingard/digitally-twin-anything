"""A frame holds, at each grid time, what was stored at the last version at or before it."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pyarrow as pa
import pytest
from dta_core.errors import DtaError
from dta_core.schema import FactKind
from dta_core.store import LIVE_BRANCH, ChangeSet, CommitMeta, Provenance
from dta_runtime import inforce
from dta_runtime.protocol import FrameRef
from dta_runtime.training import frame
from training_testkit import STEP, History, at, commit, node, tank

T1, T2, T3 = node("T1"), node("T2"), node("T3")
DEMAND = {"property": "Junction.demand", "via": "Pipe"}


def ref(history: History, start: str = "a", end: str = "g", **more: Any) -> FrameRef:
    return FrameRef(
        **{
            "target": "Tank.level",
            "branch": LIVE_BRANCH,
            "start": history.id(start),
            "end": history.id(end),
            "step": STEP,
            **more,
        }
    )


def built(history: History, start: str = "a", end: str = "g", **more: Any) -> pa.Table:
    return frame(history.twin.store, history.twin.ontology, ref(history, start, end, **more))


def cells(table: pa.Table, column: str = "y") -> dict[str, dict[float, float | None]]:
    """Per series, the column by grid time in steps after T0."""
    found: dict[str, dict[float, float | None]] = {}
    for row in table.to_pylist():
        found.setdefault(row["unique_id"], {})[(row["ds"] - at(0)) / STEP] = row[column]
    return found


def test_the_target_on_the_grid_from_the_first_version_to_the_last(small):
    table = built(small)
    assert table.schema.names == ["unique_id", "ds", "y"]
    assert table.schema.field("unique_id").type == pa.string()
    assert table.schema.field("ds").type == inforce.TIME
    assert table.schema.field("y").type == pa.float64()
    rows = [(row["unique_id"], row["ds"]) for row in table.to_pylist()]
    assert rows == sorted(rows)
    assert cells(table) == {
        # b and c share the time 2: c, the later, is in force then.
        T1: {1: 1.0, 2: 1.6, 3: 1.6, 4: 1.6, 5: 1.6, 6: 1.8, 7: 1.8},
        # Null from 3.5 on, removed at 5 (no row), back at 6.
        T2: {1: 2.0, 2: 2.0, 3: 2.0, 4: None, 6: 2.5, 7: 2.5},
        # Appears at 3.5: its first row is the next grid time.
        T3: {4: 3.0, 5: 3.0, 6: 3.0, 7: 3.0},
    }


def test_the_grid_starts_at_the_first_version_and_stops_at_the_last(small):
    # From b (time 2; c shares it and is in the range) to d (time 3.5, between grid times).
    table = built(small, "b", "d")
    assert cells(table) == {T1: {2: 1.6, 3: 1.6}, T2: {2: 2.0, 3: 2.0}}
    # A range of one version is one grid time.
    assert cells(built(small, "c", "c")) == {T1: {2: 1.6}, T2: {2: 2.0}}
    # A longer step counts from the first version's time.
    assert cells(built(small, step=3 * STEP)) == {
        T1: {1: 1.0, 4: 1.6, 7: 1.8},
        T2: {1: 2.0, 4: None, 7: 2.5},
        T3: {4: 3.0, 7: 3.0},
    }


def test_rows_ahead_hold_no_target_and_only_what_a_lag_reaches(small):
    table = built(
        small,
        "a",
        "d",
        ahead=2,
        features=[
            {"property": "Tank.level", "lag": STEP},
            {"property": "Tank.level", "lag": 2 * STEP},
        ],
    )
    assert table.schema.names == ["unique_id", "ds", "y", "Tank_level_lag1", "Tank_level_lag2"]
    assert cells(table) == {
        T1: {1: 1.0, 2: 1.6, 3: 1.6, 4: None, 5: None},
        T2: {1: 2.0, 2: 2.0, 3: 2.0, 4: None, 5: None},
        # In force at the last version (d, time 3.5), so it has the rows ahead and no other.
        T3: {4: None, 5: None},
    }
    assert cells(table, "Tank_level_lag1") == {
        # Before the first version nothing is known; 4 reads back to 3, 5 to 4, which is
        # after the last version's time.
        T1: {1: None, 2: 1.0, 3: 1.6, 4: 1.6, 5: None},
        T2: {1: None, 2: 2.0, 3: 2.0, 4: 2.0, 5: None},
        T3: {4: None, 5: None},
    }
    assert cells(table, "Tank_level_lag2") == {
        T1: {1: None, 2: None, 3: 1.0, 4: 1.6, 5: 1.6},
        T2: {1: None, 2: None, 3: 2.0, 4: 2.0, 5: 2.0},
        T3: {4: None, 5: None},
    }


def test_a_lagged_feature_is_what_was_in_force_that_long_before(small):
    table = built(
        small,
        features=[
            {"property": "Tank.level", "lag": STEP, "name": "before"},
            "Tank.max_level",
            "Node.elevation",
        ],
    )
    assert table.schema.names == ["unique_id", "ds", "y", "before", "Tank_max_level",
                                  "Node_elevation"]  # fmt: skip
    assert cells(table, "before") == {
        T1: {1: None, 2: 1.0, 3: 1.6, 4: 1.6, 5: 1.6, 6: 1.6, 7: 1.8},
        # At 6 it reads back to 5, when the tank was not there.
        T2: {1: None, 2: 2.0, 3: 2.0, 4: 2.0, 6: None, 7: 2.5},
        T3: {4: None, 5: 3.0, 6: 3.0, 7: 3.0},
    }
    assert set(table.column("Tank_max_level").to_pylist()) == {20.0}
    # A property of a class the target's type belongs to is the object's own.
    assert set(table.column("Node_elevation").to_pylist()) == {60.0}


def test_a_neighbour_aggregate_follows_the_links_in_force_at_each_time(small):
    table = built(
        small,
        features=[
            {**DEMAND, "agg": "mean"},
            {**DEMAND, "agg": "sum"},
            {**DEMAND, "agg": "min"},
            {**DEMAND, "agg": "max"},
            {**DEMAND, "agg": "count"},
        ],
    )
    assert table.schema.names[3:] == [
        "Junction_demand_mean",
        "Junction_demand_sum",
        "Junction_demand_min",
        "Junction_demand_max",
        "Junction_demand_count",
    ]
    assert cells(table, "Junction_demand_mean") == {
        # J1 and J2 until P2 is removed at 5: from then on J2 does not count (its demand
        # of 25 from 6 on is never seen), and before it does (20).
        T1: {1: 15.0, 2: 15.5, 3: 15.5, 4: 15.5, 5: 11.0, 6: 11.0, 7: 12.0},
        # J1 through P3; when the tank comes back at 6 nothing joins it.
        T2: {1: 10.0, 2: 11.0, 3: 11.0, 4: 11.0, 6: None, 7: None},
        # Joined to J2 by P4 at 7.
        T3: {4: None, 5: None, 6: None, 7: 25.0},
    }
    assert cells(table, "Junction_demand_sum")[T1] == {
        1: 30.0, 2: 31.0, 3: 31.0, 4: 31.0, 5: 11.0, 6: 11.0, 7: 12.0
    }  # fmt: skip
    assert cells(table, "Junction_demand_min")[T1] == {
        1: 10.0, 2: 11.0, 3: 11.0, 4: 11.0, 5: 11.0, 6: 11.0, 7: 12.0
    }  # fmt: skip
    assert cells(table, "Junction_demand_max")[T1] == {
        1: 20.0, 2: 20.0, 3: 20.0, 4: 20.0, 5: 11.0, 6: 11.0, 7: 12.0
    }  # fmt: skip
    assert cells(table, "Junction_demand_count") == {
        T1: {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0, 5: 1.0, 6: 1.0, 7: 1.0},
        # No neighbour at a known time is a count of zero.
        T2: {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 6: 0.0, 7: 0.0},
        T3: {4: 0.0, 5: 0.0, 6: 0.0, 7: 1.0},
    }


def test_the_direction_says_which_end_the_row_is(small):
    table = built(
        small,
        features=[
            {**DEMAND, "agg": "mean", "direction": "out", "name": "out"},
            {**DEMAND, "agg": "mean", "direction": "in", "name": "in"},
            {**DEMAND, "agg": "count", "direction": "in", "lag": STEP, "name": "in_before"},
            # A class of link types stands for every type under it.
            {"property": "Node.elevation", "via": "Link", "agg": "max", "name": "high"},
        ],
    )
    # P1 leaves T1 for J1; P2 comes into T1 from J2.
    assert cells(table, "out")[T1] == {
        1: 10.0,
        2: 11.0,
        3: 11.0,
        4: 11.0,
        5: 11.0,
        6: 11.0,
        7: 12.0,
    }
    assert cells(table, "in")[T1] == {1: 20.0, 2: 20.0, 3: 20.0, 4: 20.0, 5: None, 6: None, 7: None}
    assert cells(table, "in_before")[T1] == {
        1: None,
        2: 1.0,
        3: 1.0,
        4: 1.0,
        5: 1.0,
        6: 0.0,
        7: 0.0,
    }
    assert cells(table, "high") == {
        T1: {1: 40.0, 2: 40.0, 3: 40.0, 4: 40.0, 5: 40.0, 6: 40.0, 7: 40.0},
        T2: {1: 40.0, 2: 40.0, 3: 40.0, 4: 40.0, 6: None, 7: None},
        T3: {4: None, 5: None, 6: None, 7: 40.0},
    }


def test_only_the_series_asked_for(small):
    table = built(small, uids=[T1, node("T9")], features=[{**DEMAND, "agg": "mean"}])
    assert set(table.column("unique_id").to_pylist()) == {T1}
    assert cells(table, "Junction_demand_mean")[T1][5] == 11.0
    # The same when the target's type is also read for its neighbours.
    same = built(
        small, uids=[T2], features=[{"property": "Tank.level", "via": "Pipe", "agg": "count"}]
    )
    assert cells(same, "Tank_level_count") == {T2: {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 6: 0.0, 7: 0.0}}


def test_whole_numbers_and_booleans_are_numbers(small):
    table = frame(
        small.twin.store,
        small.twin.ontology,
        ref(small, target="Pipe.lined", features=["Pipe.length"]),
    )
    found = cells(table)
    assert found["link:P1"] == {step: 1.0 for step in range(1, 8)}
    # Removed at 5.
    assert found["link:P2"] == {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    assert found["link:P3"] == {1: None, 2: None, 3: None, 4: None}
    assert table.schema.field("y").type == pa.float64()


def refused(history: History, code: str, member: str | None = None, **more: Any) -> DtaError:
    with pytest.raises(DtaError) as caught:
        built(history, **more)
    assert caught.value.code == code, caught.value.message
    if member is not None:
        assert caught.value.detail["member"] == member, caught.value.detail
        assert member in caught.value.message
    return caught.value


@pytest.mark.parametrize(
    ("member", "more"),
    [
        ("target", {"target": "Tank.name"}),
        ("target", {"target": "Tank.volume_curve"}),
        ("target", {"target": "level"}),
        ("target", {"target": "Node.elevation"}),
        ("features[0]", {"features": ["Pipe.status"]}),
        ("features[1]", {"features": ["Tank.max_level", {"property": "Tank.level", "lag": -STEP}]}),
        ("features[0]", {"features": [{"property": "Tank.level", "lag": timedelta(minutes=20)}]}),
        ("features[0]", {"features": ["Junction.demand"]}),
        (
            "features[0]",
            {"features": [{"property": "Junction.demand", "via": "Sensor", "agg": "mean"}]},
        ),
        (
            "features[0]",
            {"features": [{"property": "Sensor.sample_interval", "via": "Pipe", "agg": "mean"}]},
        ),
        ("features[0]", {"features": [{"property": "Junction.demand", "agg": "mean"}]}),
        ("features[0]", {"features": [{"property": "Junction.demand", "via": "Pipe"}]}),
        ("features[0]", {"features": [{**DEMAND, "agg": "mean", "name": "ds"}]}),
        ("start", {"start": "g", "end": "a"}),
        ("step", {"step": timedelta(0)}),
        ("step", {"step": -STEP}),
        (
            "features",
            {
                "features": [
                    "Tank.max_level",
                    {"property": "Tank.min_level", "name": "Tank_max_level"},
                ]
            },
        ),
        ("features", {"features": [{"property": "Tank.min_level", "name": "y"}]}),
    ],
)
def test_a_reference_that_cannot_be_built_is_refused_naming_the_member(small, member, more):
    if member == "features[0]" and more["features"][0] == {**DEMAND, "agg": "mean", "name": "ds"}:
        member = "features"
    refused(small, "training.bad_spec", member, **more)


@pytest.mark.parametrize(
    ("code", "more"),
    [
        ("store.unknown_type", {"target": "Basin.level"}),
        ("store.unknown_property", {"target": "Tank.depth"}),
        ("store.unknown_type", {"features": ["Basin.level"]}),
        ("store.unknown_property", {"features": ["Tank.depth"]}),
        (
            "store.unknown_type",
            {"features": [{"property": "Junction.demand", "via": "Hose", "agg": "mean"}]},
        ),
        (
            "store.unknown_property",
            {"features": [{"property": "Junction.flow", "via": "Pipe", "agg": "mean"}]},
        ),
        ("store.unknown_version", {"start": "missing"}),
    ],
)
def test_what_does_not_exist_keeps_the_stores_own_code(small, code, more):
    if more.get("start") == "missing":
        with pytest.raises(DtaError) as caught:
            frame(
                small.twin.store,
                small.twin.ontology,
                FrameRef(
                    target="Tank.level",
                    branch=LIVE_BRANCH,
                    start="0" * 32,
                    end=small.id("g"),
                    step=STEP,
                ),
            )
        assert caught.value.code == code
        return
    refused(small, code, **more)


def test_a_frame_beyond_the_largest_is_refused_saying_how_to_narrow_it(small, monkeypatch):
    from dta_runtime.training import frames

    assert frames.MAX_CELLS == 5_000_000
    # 3 series x 7 grid times x 3 columns.
    monkeypatch.setattr(frames, "MAX_CELLS", 62)
    error = refused(small, "training.too_large")
    assert error.code == "training.too_large"
    assert "uids" in error.message and "step" in error.message
    assert error.detail == {"series": 3, "points": 7, "columns": 3}
    monkeypatch.setattr(frames, "MAX_CELLS", 63)
    assert built(small).num_rows == 17
    # Known before anything is read when the grid alone is too long.
    monkeypatch.setattr(frames, "MAX_CELLS", 20)
    assert refused(small, "training.too_large").detail == {"points": 7, "columns": 3}


def test_a_type_is_read_once_whatever_uses_it(small, monkeypatch):
    reads: list[tuple[str, str]] = []
    store = small.twin.store
    for name in ("table", "changes"):
        original = getattr(store, name)

        def counted(type_name, *args, _original=original, _name=name, **kwargs):
            reads.append((_name, type_name))
            return _original(type_name, *args, **kwargs)

        monkeypatch.setattr(store, name, counted)
    built(
        small,
        features=[
            {"property": "Tank.level", "lag": STEP},
            {**DEMAND, "agg": "mean"},
            {**DEMAND, "agg": "max", "lag": 2 * STEP},
            {**DEMAND, "agg": "count", "direction": "in"},
            {"property": "Tank.level", "via": "Pipe", "agg": "mean"},
        ],
    )
    # The state at the first version and the changes after it, once per type.
    assert sorted(reads) == sorted(
        (name, type_name)
        for name in ("table", "changes")
        for type_name in ("Tank", "Pipe", "Junction")
    )


def test_a_frame_on_a_branch_reads_that_branch(make_twin):
    twin = make_twin()
    first = commit(twin, at(1), {"Tank": [tank("T1", 1.0)]})
    commit(twin, at(2), {"Tank": [{"uid": T1, "level": 2.0}]})
    twin.store.branch_create("what_if", at=first.id)
    other = twin.store.commit(
        ChangeSet(
            upserts={"Tank": [{"uid": T1, "level": 7.0}]},
            provenance=Provenance(source="planner", kind=FactKind.simulated, t_valid=at(2)),
        ),
        CommitMeta(cause="scenario", actor="planner", t=at(2)),
        branch="what_if",
    )
    table = frame(
        twin.store,
        twin.ontology,
        FrameRef(target="Tank.level", start=first.id, end=other.id, branch="what_if", step=STEP),
    )
    assert cells(table) == {T1: {1: 1.0, 2: 7.0}}
