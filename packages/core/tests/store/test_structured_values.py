"""Multivalued scalars and structured values: written, read back as Arrow, followed
through history and compared between versions."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import pyarrow as pa
from dta_core.store import Store
from dta_store_testkit import at, changes, meta

PLUS_THREE = timezone(timedelta(hours=3))
FIRST = {
    "uid": "node:t1",
    "name": "T1",
    "capacity": 80.0,
    "level": 2.0,
    "status": "out of service",
    "curve": [0, 1.5, 4.25],
    "tags": ["north", 'it\'s "quoted"', "ünïcode"],
    "inspections": [datetime(2024, 3, 1, 12, 30, tzinfo=PLUS_THREE), "2024-04-01T00:00:00Z"],
    "geometry": {"shape": "cylinder", "radius": 2.5, "ports": [{"id": 1}, {"id": 2}]},
    "commissioned": date(2019, 6, 30),
    "sampled_at": datetime(2024, 5, 1, 9, 0, 0, 123456, tzinfo=PLUS_THREE),
    "sealed": True,
    "fill_count": 7,
    "notes": "long text " * 100,
    "code": "TK-17",
}


def _load(store: Store) -> None:
    store.commit(changes({"Tank": [FIRST]}), meta(t=at(1)))
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "curve": [0, 2, 5], "tags": []}]}, kind="inferred"),
        meta("derived", at(2)),
    )
    store.commit(
        changes(
            {"Tank": [{"uid": "node:t1", "geometry": {"shape": "sphere"}, "inspections": None}]},
            kind="assumed",
        ),
        meta("manual", at(3)),
    )


def test_arrow_types_come_from_the_descriptors(store: Store):
    _load(store)
    table = store.table("Tank", at="#1")
    assert table.schema.field("uid").type == pa.string()
    assert table.schema.field("level").type == pa.float64()
    assert table.schema.field("fill_count").type == pa.int64()
    assert table.schema.field("sealed").type == pa.bool_()
    assert table.schema.field("status").type == pa.string()
    assert table.schema.field("commissioned").type == pa.date32()
    assert table.schema.field("sampled_at").type == pa.timestamp("us", tz="UTC")
    assert table.schema.field("curve").type == pa.list_(pa.float64())
    assert table.schema.field("tags").type == pa.list_(pa.string())
    assert table.schema.field("inspections").type == pa.list_(pa.timestamp("us", tz="UTC"))
    assert table.schema.field("geometry").type == pa.string()
    assert "_prov" not in table.schema.names
    assert store.table("Tank", prov=True).schema.field("_prov").type == pa.string()


def test_values_read_back_as_written_with_times_in_utc(store: Store):
    _load(store)
    row = store.table("Tank", at="#1").to_pylist()[0]
    assert row["curve"] == [0.0, 1.5, 4.25]
    assert row["tags"] == ["north", 'it\'s "quoted"', "ünïcode"]
    assert row["inspections"] == [
        datetime(2024, 3, 1, 9, 30, tzinfo=UTC),
        datetime(2024, 4, 1, tzinfo=UTC),
    ]
    assert json.loads(row["geometry"]) == FIRST["geometry"]
    assert row["commissioned"] == date(2019, 6, 30)
    assert row["sampled_at"] == datetime(2024, 5, 1, 6, 0, 0, 123456, tzinfo=UTC)
    assert row["sampled_at"].utcoffset() == timedelta(0)
    assert row["sealed"] is True and row["fill_count"] == 7 and row["status"] == "out of service"
    assert row["notes"] == FIRST["notes"]

    found = store.get("node:t1", at="#1")
    assert found.values["geometry"] == FIRST["geometry"]
    assert found.values["curve"] == [0.0, 1.5, 4.25]
    assert found.values["inspections"][0] == datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
    assert found.values["sampled_at"].tzinfo is not None
    assert found.values["sealed"] is True


def test_each_version_keeps_its_own_structured_values(store: Store):
    _load(store)
    curves = [store.get("node:t1", at=f"#{seq}").values["curve"] for seq in (1, 2, 3)]
    assert curves == [[0.0, 1.5, 4.25], [0.0, 2.0, 5.0], [0.0, 2.0, 5.0]]
    shapes = [store.get("node:t1", at=f"#{seq}").values["geometry"]["shape"] for seq in (1, 2, 3)]
    assert shapes == ["cylinder", "cylinder", "sphere"]
    assert store.get("node:t1").values["tags"] == []
    assert store.get("node:t1").values["inspections"] is None


def test_history_follows_structured_values_across_versions(store: Store):
    _load(store)
    history = store.history("node:t1", ["curve", "geometry"]).to_pylist()
    assert [(row["_seq"], row["_change"]) for row in history] == [
        (1, "added"),
        (2, "modified"),
        (3, "modified"),
    ]
    assert [row["curve"] for row in history] == [[0.0, 1.5, 4.25], [0.0, 2.0, 5.0], [0.0, 2.0, 5.0]]
    assert [json.loads(row["geometry"])["shape"] for row in history] == [
        "cylinder",
        "cylinder",
        "sphere",
    ]
    # Asking for one property leaves out the versions in which only others changed.
    only_curve = store.history("node:t1", ["curve"])
    assert only_curve.column("_seq").to_pylist() == [1, 2]
    assert only_curve.schema.names == ["_version", "_seq", "_t", "_change", "uid", "curve"]
    only_tags = store.history("node:t1", ["tags"], range="#2..head")
    assert only_tags.column("_seq").to_pylist() == [2]
    assert only_tags.column("tags").to_pylist() == [[]]


def test_diff_shows_structured_values_before_and_after(store: Store):
    _load(store)
    difference = store.diff("#1", "#3", types=["Tank"])
    assert list(difference) == ["Tank"]
    row = difference["Tank"].to_pylist()[0]
    assert row["_change"] == "modified" and row["uid"] == "node:t1"
    assert row["curve"] == [0.0, 2.0, 5.0] and row["_before"]["curve"] == [0.0, 1.5, 4.25]
    assert json.loads(row["geometry"]) == {"shape": "sphere"}
    assert json.loads(row["_before"]["geometry"]) == FIRST["geometry"]
    assert row["inspections"] is None and len(row["_before"]["inspections"]) == 2
    assert row["tags"] == [] and row["_before"]["tags"] == FIRST["tags"]
    assert store.diff("#3", "#3") == {}

    added = store.diff("#0", "#1")["Tank"].to_pylist()[0]
    assert added["_change"] == "added" and added["_before"] is None
    removed_view = store.diff("#1", "#0")["Tank"].to_pylist()[0]
    assert removed_view["_change"] == "removed" and removed_view["curve"] is None
    assert removed_view["_before"]["curve"] == [0.0, 1.5, 4.25]


def test_changes_lists_every_version_of_the_structured_row(store: Store):
    _load(store)
    listed = store.changes("Tank", columns=["curve", "tags"]).to_pylist()
    assert [(row["_seq"], row["_change"], row["tags"]) for row in listed] == [
        (1, "added", FIRST["tags"]),
        (2, "modified", []),
        (3, "modified", []),
    ]


def test_meta_types_hold_multivalued_values_too(store: Store):
    store.commit(
        changes(
            {
                "TwinSyncRun": [
                    {"uid": "twin_sync_run:r1", "status": "succeeded", "sources": ["scada", "gis"]}
                ]
            },
            kind="inferred",
        ),
        meta("sync", at(1)),
    )
    row = store.table("TwinSyncRun").to_pylist()[0]
    assert row["sources"] == ["scada", "gis"] and row["status"] == "succeeded"
