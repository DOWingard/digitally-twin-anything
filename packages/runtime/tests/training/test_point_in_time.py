"""The point-in-time rule as a property of a generated history: every cell of a frame is
what plain reads of the store give at the version in force at the cell's time, and what
is stored later never changes an earlier row."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from dta_core.errors import DtaError
from dta_core.store import LIVE_BRANCH, VersionRef
from dta_runtime.protocol import FrameRef, TrainingSetRequest
from dta_runtime.training import frame, frames, training_set
from training_testkit import GENERATED_VERSIONS, JUNCTIONS, STEP, TANKS, History, Plain, node

HALF = STEP / 2
NEAR = {"property": "Junction.demand", "via": "Pipe"}
FEATURES: list[dict[str, Any]] = [
    {"property": "Tank.level", "lag": STEP, "name": "before"},
    {"property": "Tank.level", "lag": 3 * STEP, "name": "long_before"},
    {**NEAR, "agg": "mean", "name": "near"},
    {**NEAR, "agg": "count", "direction": "out", "lag": 2 * STEP, "name": "out_before"},
    {**NEAR, "agg": "max", "direction": "in", "name": "in_high"},
    {**NEAR, "agg": "sum", "lag": STEP, "name": "sum_before"},
    {**NEAR, "agg": "min", "name": "low"},
]
# (first version, last version, step, rows ahead)
RANGES = [(3, 78, STEP, 3), (0, GENERATED_VERSIONS - 1, HALF, 0), (10, 41, STEP, 2)]


def _versions(history: History, first: int, last: int) -> list[VersionRef]:
    return [history.versions[f"v{index}"] for index in range(first, last + 1)]


def _ref(history: History, first: int, last: int, step: timedelta, **more: Any) -> dict[str, Any]:
    return {
        "target": "Tank.level",
        "branch": LIVE_BRANCH,
        "start": history.id(f"v{first}"),
        "end": history.id(f"v{last}"),
        "step": step,
        "features": FEATURES,
        **more,
    }


def _grid(versions: list[VersionRef], step: timedelta, ahead: int) -> list:
    t0, t1 = versions[0].t, versions[-1].t
    count = (t1 - t0) // step + 1
    return [t0 + index * step for index in range(count + ahead)]


def _expected(plain: Plain, uid: str, moment, feature: dict[str, Any]) -> float | None:
    at = moment - feature.get("lag", timedelta(0))
    type_name, name = feature["property"].split(".")
    if "via" not in feature:
        return plain.value(uid, type_name, name, at)
    return plain.aggregate(
        uid, feature["via"], feature.get("direction", "both"), type_name, name, feature["agg"], at
    )


def test_the_generated_history_is_large_enough_and_has_every_case(generated):
    store = generated.twin.store
    assert len(TANKS) >= 20 and GENERATED_VERSIONS >= 80
    versions = _versions(generated, 0, GENERATED_VERSIONS - 1)
    times = [version.t for version in versions]
    assert len(set(times)) < len(times), "no two versions share a time"
    tanks = store.changes("Tank", f"{versions[0].id}..{versions[-1].id}").to_pylist()
    assert sum(row["_change"] == "removed" for row in tanks) >= 3
    assert sum(row["_change"] == "added" for row in tanks) > 24
    assert any(row["_change"] == "modified" and row["level"] is None for row in tanks)
    pipes = store.changes("Pipe", f"{versions[0].id}..{versions[-1].id}").to_pylist()
    assert sum(row["_change"] == "removed" for row in pipes) >= 5
    assert sum(row["_change"] == "modified" for row in pipes) >= 5, "no pipe changed its ends"


@pytest.mark.parametrize(("first", "last", "step", "ahead"), RANGES)
def test_every_cell_is_what_plain_reads_give_at_the_version_in_force(
    generated, first, last, step, ahead
):
    twin = generated.twin
    versions = _versions(generated, first, last)
    plain = Plain(twin, versions)
    table = frame(
        twin.store, twin.ontology, FrameRef(**_ref(generated, first, last, step, ahead=ahead))
    )
    grid = _grid(versions, step, ahead)
    assert grid[0] == versions[0].t and grid[-1 - ahead] <= versions[-1].t < grid[-1 - ahead] + step
    rows = table.to_pylist()
    assert [(row["unique_id"], row["ds"]) for row in rows] == sorted(
        (node(key), moment)
        for key in TANKS
        for moment in grid
        if plain.exists(node(key), "Tank", moment)
    )
    seen = {"null": 0, "value": 0, "ahead": 0, "no_neighbour": 0, "neighbours": 0}
    for row in rows:
        uid, moment = row["unique_id"], row["ds"]
        assert row["y"] == plain.value(uid, "Tank", "level", moment), (uid, moment)
        seen["ahead" if moment > versions[-1].t else "null" if row["y"] is None else "value"] += 1
        for feature in FEATURES:
            expected = _expected(plain, uid, moment, feature)
            assert row[feature["name"]] == pytest.approx(expected, abs=1e-9), (
                uid,
                moment,
                feature["name"],
            )
        seen["no_neighbour" if row["near"] is None else "neighbours"] += 1
    assert seen["null"] and seen["value"] and seen["no_neighbour"] and seen["neighbours"], seen
    assert seen["ahead"] or not ahead
    # The neighbours of a series change over the range: its count is not one number.
    counts: dict[str, set] = {}
    for row in rows:
        counts.setdefault(row["unique_id"], set()).add(row["out_before"])
    assert sum(len(found - {None}) > 1 for found in counts.values()) >= 5
    # The same cells by reads of one object at one version.
    rng = random.Random(first)
    known = [row for row in rows if row["ds"] <= versions[-1].t]
    for row in rng.sample(known, 60):
        version = plain.version_at(row["ds"])
        found = twin.store.get(row["unique_id"], at=version, ontology=twin.ontology)
        assert found is not None and found.values["level"] == row["y"]
        before = plain.version_at(row["ds"] - STEP) if plain.known(row["ds"] - STEP) else None
        earlier = None if before is None else twin.store.get(row["unique_id"], at=before)
        assert row["before"] == (None if earlier is None else earlier.values["level"])


@pytest.mark.parametrize(("first", "last", "step"), [(3, 78, STEP), (0, 60, HALF)])
def test_a_label_is_the_target_read_at_the_version_in_force_a_horizon_later(
    generated, first, last, step
):
    twin = generated.twin
    versions = _versions(generated, first, last)
    plain = Plain(twin, versions)
    horizon = 2 * STEP
    request = TrainingSetRequest(**_ref(generated, first, last, step), horizon=horizon)
    found = training_set(twin.store, twin.ontology, request)
    plainly = frame(twin.store, twin.ontology, FrameRef(**_ref(generated, first, last, step)))
    kept = plainly.filter(
        pc.less_equal(
            plainly.column("ds"),
            pa.scalar(versions[-1].t - horizon, plainly.schema.field("ds").type),
        )
    )
    # The frame's own columns are the frame's, for the rows whose label time is known.
    assert found.table.drop_columns(["label", "label_ds"]).equals(kept)
    assert found.table.num_rows and found.metadata["rows"] == found.table.num_rows
    labels = 0
    for row in found.table.to_pylist():
        assert row["label_ds"] == row["ds"] + horizon <= versions[-1].t
        assert row["label"] == plain.value(row["unique_id"], "Tank", "level", row["label_ds"])
        labels += row["label"] is not None
    assert 0 < labels < found.table.num_rows


def test_what_is_stored_later_never_changes_an_earlier_row(generated):
    twin = generated.twin
    for first, last, later, step in [(3, 39, 78, STEP), (0, 21, 83, HALF), (10, 41, 42, STEP)]:
        end = generated.versions[f"v{last}"]
        # A later version at the same time as the last would be in force at that time.
        assert generated.versions[f"v{last + 1}"].t > end.t
        short = frame(twin.store, twin.ontology, FrameRef(**_ref(generated, first, last, step)))
        long = frame(twin.store, twin.ontology, FrameRef(**_ref(generated, first, later, step)))
        assert long.num_rows > short.num_rows
        until = pa.scalar(end.t, long.schema.field("ds").type)
        assert long.filter(pc.less_equal(long.column("ds"), until)).equals(short)


def test_the_links_a_neighbour_feature_follows_count_towards_the_largest_frame(
    generated, monkeypatch
):
    twin = generated.twin
    first, last = 0, GENERATED_VERSIONS - 1
    versions = _versions(generated, first, last)
    changes = twin.store.changes("Pipe", f"{versions[0].id}..{versions[-1].id}").to_pylist()
    touching: dict[str, set[str]] = {}
    for row in changes:
        for end in (row["start_node"], row["end_node"]):
            if end is not None:
                touching.setdefault(end, set()).add(row["uid"])
    uid, links = max(
        ((node(key), touching.get(node(key), set())) for key in TANKS),
        key=lambda item: len(item[1]),
    )
    points = len(_grid(versions, STEP, 0))
    columns = 4
    assert len(links) > columns, "no tank is touched by enough pipes over the history"
    ref = FrameRef(
        **{
            **_ref(generated, first, last, STEP),
            "features": [{**NEAR, "agg": "mean"}],
            "uids": [uid],
        }
    )
    monkeypatch.setattr(frames, "MAX_CELLS", len(links) * points - 1)
    with pytest.raises(DtaError) as caught:
        frame(twin.store, twin.ontology, ref)
    assert caught.value.code == "training.too_large"
    assert caught.value.detail == {"links": len(links), "points": points}
    assert "uids" in caught.value.message
    monkeypatch.setattr(frames, "MAX_CELLS", len(links) * points)
    assert frame(twin.store, twin.ontology, ref).num_rows


def test_a_generated_junction_is_never_a_series(generated):
    twin = generated.twin
    table = frame(twin.store, twin.ontology, FrameRef(**_ref(generated, 0, 20, STEP)))
    assert not set(table.column("unique_id").to_pylist()) & {node(key) for key in JUNCTIONS}
