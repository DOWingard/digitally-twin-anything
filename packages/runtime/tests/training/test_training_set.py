"""A training set is a frame, the label of each row, and what a reader has to know of it."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from dta_core.errors import DtaError
from dta_core.store import LIVE_BRANCH
from dta_core.store.model import iso_utc
from dta_runtime.protocol import FrameRef, TrainingSetRequest
from dta_runtime.sync.records import run_uid
from dta_runtime.training import TrainingSet, frame, training_set
from training_testkit import STEP, History, at, commit, node, tank

T1, T2, T3 = node("T1"), node("T2"), node("T3")


def request(history: History, **more: Any) -> TrainingSetRequest:
    return TrainingSetRequest(
        **{
            "target": "Tank.level",
            "branch": LIVE_BRANCH,
            "start": history.id("a"),
            "end": history.id("g"),
            "step": STEP,
            **more,
        }
    )


def made(history: History, **more: Any) -> TrainingSet:
    return training_set(history.twin.store, history.twin.ontology, request(history, **more))


def test_a_label_is_the_target_a_horizon_later_and_rows_without_one_are_left_out(small):
    found = made(small, horizon=2 * STEP, features=[{"property": "Tank.level", "lag": STEP}])
    assert found.table.schema.names == ["unique_id", "ds", "y", "Tank_level_lag1", "label",
                                        "label_ds"]  # fmt: skip
    rows = found.table.to_pylist()
    labels: dict[str, dict[float, Any]] = {}
    for row in rows:
        assert row["label_ds"] == row["ds"] + 2 * STEP
        labels.setdefault(row["unique_id"], {})[(row["ds"] - at(0)) / STEP] = (
            row["y"],
            row["label"],
        )
    # The last version is at 7: a row after 5 has no label time inside the range.
    assert labels == {
        T1: {1: (1.0, 1.6), 2: (1.6, 1.6), 3: (1.6, 1.6), 4: (1.6, 1.8), 5: (1.6, 1.8)},
        # Null at 4, gone at 5: the labels of 2 and 3 are null, the rows stay.
        T2: {1: (2.0, 2.0), 2: (2.0, None), 3: (2.0, None), 4: (None, 2.5)},
        T3: {4: (3.0, 3.0), 5: (3.0, 3.0)},
    }


def test_without_a_horizon_it_is_the_frame(small):
    found = made(small, ahead=2, features=["Tank.max_level"])
    same = frame(
        small.twin.store,
        small.twin.ontology,
        FrameRef(
            **request(small, ahead=2, features=["Tank.max_level"]).model_dump(exclude={"horizon"})
        ),
    )
    assert found.table.equals(same)
    assert found.metadata["horizon_s"] is None and found.metadata["ahead"] == 2


def test_the_metadata_says_what_was_resolved(small):
    found = made(
        small,
        horizon=STEP,
        uids=[T1, T3],
        features=[
            {"property": "Tank.level", "lag": 2 * STEP},
            {"property": "Junction.demand", "via": "Pipe", "agg": "mean", "direction": "out"},
        ],
    )
    first, last = small.versions["a"], small.versions["g"]
    metadata = found.metadata
    assert json.loads(json.dumps(metadata)) == metadata
    assert metadata == {
        "target": "Tank.level",
        "branch": LIVE_BRANCH,
        "start": {"id": first.id, "seq": first.seq, "t": iso_utc(at(1))},
        "end": {"id": last.id, "seq": last.seq, "t": iso_utc(at(7))},
        "step_s": 900.0,
        "horizon_s": 900.0,
        "ahead": 0,
        "grid": {"first": iso_utc(at(1)), "last": iso_utc(at(7)), "points": 7},
        "series": 2,
        "rows": found.table.num_rows,
        "columns": [
            {"name": "unique_id", "is": "uid of the Tank"},
            {"name": "ds", "is": "grid time"},
            {"name": "y", "is": "Tank.level in force at ds"},
            {"name": "Tank_level_lag2", "is": "Tank.level 2 steps back"},
            {"name": "Junction_demand_mean", "is": "mean of Junction.demand over Pipe (out)"},
            {"name": "label", "is": "Tank.level in force at label_ds"},
            {"name": "label_ds", "is": "ds plus the horizon"},
        ],
        "backfill_until": None,
        "note": metadata["note"],
    }
    assert "no backfill" in metadata["note"]
    # T1 at 1..6, T3 at 4..6.
    assert found.table.num_rows == 9


@pytest.mark.parametrize(
    ("member", "more"),
    [
        ("horizon", {"horizon": timedelta(minutes=20)}),
        ("horizon", {"horizon": timedelta(0)}),
        ("horizon", {"horizon": -STEP}),
        (
            "features",
            {"horizon": STEP, "features": [{"property": "Tank.max_level", "name": "label"}]},
        ),
        (
            "features",
            {"horizon": STEP, "features": [{"property": "Tank.max_level", "name": "label_ds"}]},
        ),
        ("target", {"target": "Tank.name"}),
    ],
)
def test_a_training_set_that_cannot_be_built_is_refused(small, member, more):
    with pytest.raises(DtaError) as caught:
        made(small, **more)
    assert caught.value.code == "training.bad_spec"
    assert caught.value.detail["member"] == member
    # Without a horizon nothing is named `label`, so the name is free.
    if member == "features":
        assert made(small, features=more["features"]).table.num_rows


def _run(key: str, kind: str, started: float, finished: float, status: str = "succeeded") -> dict:
    return {
        "uid": run_uid(key),
        "kind": kind,
        "status": status,
        "started_at": at(started),
        "finished_at": at(finished),
        "versions_written": 1,
    }


def test_rows_a_backfill_wrote_are_said_to_be_as_the_source_reported_them_later(make_twin):
    twin = make_twin()
    first = commit(twin, at(1), {"Tank": [tank("T1", 1.0)]}, extra={"backfill": True})
    commit(
        twin,
        at(3),
        {"Tank": [{"uid": T1, "level": 2.0}], "TwinSyncRun": [_run("load-1", "backfill", 1, 3)]},
        extra={"backfill": True},
    )
    middle = commit(twin, at(4), {"Tank": [{"uid": T1, "level": 3.0}]})
    commit(
        twin,
        at(5),
        {
            "Tank": [{"uid": T1, "level": 4.0}],
            "TwinSyncRun": [
                _run("live-1", "incremental", 4, 5),
                # A backfill that failed still wrote what it wrote.
                _run("load-2", "backfill", 5, 5, status="failed"),
            ],
        },
    )
    last = commit(twin, at(6), {"Tank": [{"uid": T1, "level": 5.0}]})

    def until(end) -> TrainingSet:
        return training_set(
            twin.store,
            twin.ontology,
            TrainingSetRequest(
                target="Tank.level", branch=LIVE_BRANCH, start=first.id, end=end.id, step=STEP
            ),
        )

    found = until(last)
    assert found.metadata["backfill_until"] == iso_utc(at(5))
    note = found.metadata["note"]
    assert iso_utc(at(5)) in note and "never rewrote its past" in note
    # What the last version of the range knows of the runs, not what came after it.
    assert until(middle).metadata["backfill_until"] == iso_utc(at(3))
    assert found.table.column("y").to_pylist() == [1.0, 1.0, 2.0, 3.0, 4.0, 5.0]
