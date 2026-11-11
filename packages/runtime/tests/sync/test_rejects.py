"""Rows that break a rule are set aside and named in a file; the rest is written."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from dta_runtime.sync import AllRejected
from dta_runtime.twin import Twin
from sync_testkit import (
    MINUTE,
    T0,
    TIME,
    Sources,
    changes_of,
    columns,
    objects,
    source,
    sync_versions,
    table,
)
from water_scenario import JUNCTIONS, TANKS, network_sources, network_tables

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]

BACKFILL = {"backfill_interval": 900}
LINE_FIELDS = {"run", "source", "mapping", "type", "uid", "property", "rule", "message", "t"}


def rejects_of(tmp_path: Path, location: str) -> tuple[str, list[dict[str, Any]]]:
    text = (tmp_path / "artifacts" / location).read_text(encoding="utf-8")
    return text, [json.loads(line) for line in text.splitlines()]


def registry() -> dict[str, Any]:
    return source(
        {
            "junctions": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation"),
            },
            "tanks": {
                "dataset": "tanks",
                "object_type": "Tank",
                "identity": ["id"],
                "properties": columns("name", "elevation", "min_level", "max_level"),
            },
            "pipes": {
                "dataset": "pipes",
                "object_type": "Pipe",
                "identity": ["id"],
                "properties": columns(
                    "status",
                    start_node="from_node",
                    end_node="to_node",
                    length="length_m",
                    diameter="diameter_mm",
                ),
            },
        }
    )


def pipe(uid: str, start: str, end: str, status: str = "open") -> dict[str, Any]:
    return {
        "id": uid,
        "from_node": start,
        "to_node": end,
        "length_m": 100.0,
        "diameter_mm": 150.0,
        "status": status,
    }


def tank(uid: str, name: str, low: float, high: float) -> dict[str, Any]:
    return {"id": uid, "name": name, "elevation": 40.0, "min_level": low, "max_level": high}


def messy_tables() -> dict[str, pa.Table]:
    junctions = [
        {"id": "J1", "name": "North", "elevation": "12.5"},
        {"id": "J2", "name": "Mid", "elevation": "10.0"},
        {"id": "J3", "name": "South", "elevation": "8.25"},
        # Not a number.
        {"id": "J4", "name": "Far", "elevation": "high"},
        # No identity.
        {"id": None, "name": "Nameless", "elevation": "3.5"},
        # Two rows that differ claim one identity.
        {"id": "J6", "name": "Twin", "elevation": "77.125"},
        {"id": "J6", "name": "Twin", "elevation": "78.125"},
    ]
    tanks = [
        tank("T1", "Hill tank", 0.5, 6.0),
        # Breaks the ontology's check that the bounds are ordered.
        tank("T2", "Upside down", 5.0, 1.0),
        # Below the property's minimum.
        tank("T3", "Sunken", -7.75, 6.0),
    ]
    pipes = [
        pipe("P1", "J1", "J2"),
        # Ends at a node nobody knows.
        pipe("P2", "J2", "J9"),
        # Starts at the junction set aside while mapping.
        pipe("P3", "J4", "J1"),
        # Ends at the tank the store refuses: sound until that tank is taken out.
        pipe("P4", "J1", "T2"),
        # Not a value of the enum.
        pipe("P5", "J2", "J3", status="ajar"),
    ]
    return {"junctions": table(junctions), "tanks": table(tanks), "pipes": table(pipes)}


def test_rows_that_break_a_rule_are_set_aside_and_the_rest_is_written(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin({"registry": registry()})
    report = run(twin, Sources(registry=messy_tables()))
    store = twin.store

    # Everything that holds is in the twin, in one version.
    assert report.status == "succeeded"
    assert report.versions_written == 1
    assert set(objects(store, "Junction")) == {"node:J1", "node:J2", "node:J3"}
    assert set(objects(store, "Tank")) == {"node:T1"}
    assert set(objects(store, "Pipe")) == {"link:P1"}
    assert objects(store, "Junction")["node:J1"]["elevation"] == 12.5

    counts = {
        label: (count.rows_read, count.rows_written, count.rows_rejected)
        for label, count in report.mappings.items()
    }
    assert counts == {
        "registry.junctions": (7, 3, 4),
        "registry.tanks": (3, 1, 2),
        "registry.pipes": (5, 1, 4),
    }
    assert (report.rows_read, report.rows_written, report.rows_rejected) == (15, 5, 10)
    assert report.sources["registry"].rows_rejected == 10

    assert report.rejects_location == f"rejects/{report.run.split(':', 1)[1]}.jsonl"
    text, lines = rejects_of(tmp_path, report.rejects_location)
    found = {(line["type"], line["uid"], line["rule"], line["property"]) for line in lines}
    assert found == {
        ("Junction", "node:J4", "type", "elevation"),
        ("Junction", None, "uid", None),
        ("Junction", "node:J6", "duplicate", None),
        ("Tank", "node:T2", "check", None),
        ("Tank", "node:T3", "range", "min_level"),
        ("Pipe", "link:P2", "reference", "end_node"),
        ("Pipe", "link:P3", "reference", "start_node"),
        ("Pipe", "link:P4", "reference", "end_node"),
        ("Pipe", "link:P5", "enum", "status"),
    }
    assert len(lines) == 9
    for line in lines:
        assert set(line) - {"also"} == LINE_FIELDS
        assert (line["run"], line["source"]) == (report.run, "registry")
        assert line["mapping"] == f"registry.{line['type'].lower()}s"
    by_uid = {line["uid"]: line for line in lines}
    assert by_uid["node:T2"]["message"] == "the ontology's check bounds_ordered does not hold"
    assert by_uid["link:P2"]["message"] == "refers to an object that does not exist"
    assert by_uid["node:J6"]["message"] == "2 rows that differ claim this identity"
    # A row refused by the store carries the time of the version it was meant for.
    assert by_uid["link:P2"]["t"] == "2026-01-05T00:00:00Z"
    assert by_uid["node:J4"]["t"] is None

    # The file says which row and which rule, and nothing of what the row held.
    for value in ("high", "ajar", "J9", "7.75", "77.125", "Nameless", "Upside", "Sunken", "Far"):
        assert value not in text, value

    stored = objects(store, "TwinSyncRun")[report.run]
    assert (stored["status"], stored["rows_rejected"], stored["rows_written"]) == (
        "succeeded",
        10,
        5,
    )
    assert stored["rejects_location"] == report.rejects_location


def test_a_run_without_rejects_leaves_no_file(sync_twin: MakeTwin, run: Run, tmp_path: Path):
    twin = sync_twin({"registry": registry()})
    tables = {
        "junctions": table(JUNCTIONS),
        "tanks": table(TANKS),
        "pipes": table([pipe("P1", "J1", "J2")]),
    }
    report = run(twin, Sources(registry=tables))

    assert (report.rows_rejected, report.rejects_location) == (0, None)
    assert not (tmp_path / "artifacts" / "rejects").exists()
    assert objects(twin.store, "TwinSyncRun")[report.run]["rejects_location"] is None


def test_a_row_that_stays_wrong_is_set_aside_again_without_a_new_version(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin({"registry": registry()})
    sources = Sources(registry=messy_tables())
    first = run(twin, sources)
    head = twin.store.head()

    second = run(twin, sources)

    # Nothing new could be written, so the twin is as it was; the rows are reported again.
    assert twin.store.head() == head
    assert (second.versions_written, second.rows_written) == (0, 0)
    assert second.rows_rejected == first.rows_rejected
    _, lines = rejects_of(tmp_path, second.rejects_location)
    assert len(lines) == 9


def test_a_reading_that_breaks_a_check_is_set_aside_and_its_series_goes_on(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    rows = [
        ("T1.level", T0 + 10 * MINUTE, 1.0),
        # Above the tank's highest level.
        ("T1.level", T0 + 20 * MINUTE, 9.5),
        # Back to what the twin still holds: nothing to write.
        ("T1.level", T0 + 40 * MINUTE, 1.0),
        ("T1.level", T0 + 55 * MINUTE, 2.0),
        ("J1.demand", T0 + 10 * MINUTE, 5.0),
        ("J1.demand", T0 + 20 * MINUTE, 6.0),
    ]
    twin = sync_twin(network_sources(), sync=BACKFILL)
    report = run(twin, Sources(**network_tables(rows)))
    store = twin.store

    assert report.status == "succeeded"
    assert changes_of(store, "node:T1", "level") == [
        (T0 + 15 * MINUTE, 1.0),
        (T0 + 60 * MINUTE, 2.0),
    ]
    # The rest of the refused version went in.
    assert changes_of(store, "node:J1", "demand") == [
        (T0 + 15 * MINUTE, 5.0),
        (T0 + 30 * MINUTE, 6.0),
    ]
    assert [version.t for version in sync_versions(store)] == [
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    levels = report.mappings["scada.levels"]
    assert (levels.rows_read, levels.rows_written, levels.rows_rejected) == (4, 2, 1)

    text, lines = rejects_of(tmp_path, report.rejects_location)
    assert len(lines) == 1
    line = lines[0]
    assert (line["type"], line["uid"], line["rule"]) == ("Tank", "node:T1", "check")
    assert line["message"] == "the ontology's check level_within_bounds does not hold"
    assert (line["mapping"], line["t"]) == ("scada.levels", "2026-01-05T00:30:00Z")
    assert "9.5" not in text


def pump_sources() -> dict[str, Any]:
    return {
        "registry": source(
            {
                "junctions": {
                    "dataset": "junctions",
                    "object_type": "Junction",
                    "identity": ["id"],
                    "properties": columns("name", "elevation"),
                },
                "pumps": {
                    "dataset": "pumps",
                    "object_type": "Pump",
                    "identity": ["id"],
                    "properties": columns(start_node="from_node", end_node="to_node"),
                },
            }
        ),
        "plant": source(
            {
                "pump_state": {
                    "dataset": "pump_state",
                    "object_type": "Pump",
                    "identity": ["pump"],
                    "valid_time": "ts",
                    "properties": columns("status", "speed"),
                }
            }
        ),
    }


def pump_state(rows: list[tuple[datetime, str | None, float | None]]) -> pa.Table:
    return pa.table(
        {
            "pump": pa.array(["PU1"] * len(rows)),
            "ts": pa.array([row[0] for row in rows], TIME),
            "status": pa.array([row[1] for row in rows], pa.string()),
            "speed": pa.array([row[2] for row in rows], pa.float64()),
        }
    )


def test_a_value_lost_with_a_refused_row_is_written_when_it_is_next_reported(
    sync_twin: MakeTwin, run: Run
):
    states = [
        (T0 + 10 * MINUTE, "closed", 0.0),
        # The speed is beyond the pump's range, so the whole row is set aside,
        # and with it the news that the pump was opened.
        (T0 + 20 * MINUTE, "open", 5.0),
        # The status is reported again, unchanged from the row before.
        (T0 + 40 * MINUTE, "open", None),
        (T0 + 55 * MINUTE, None, 1.0),
    ]
    tables = {
        "registry": {
            "junctions": table(JUNCTIONS),
            "pumps": table([{"id": "PU1", "from_node": "J1", "to_node": "J2"}]),
        },
        "plant": {"pump_state": pump_state(states)},
    }
    twin = sync_twin(pump_sources(), sync=BACKFILL)
    report = run(twin, Sources(**tables))
    store = twin.store

    assert report.status == "succeeded"
    assert report.mappings["plant.pump_state"].rows_rejected == 1
    # The twin never held the refused row, so "open" is news when it comes again,
    # in an interval that had nothing else to write.
    assert changes_of(store, "link:PU1", "status") == [
        (T0 + 15 * MINUTE, "closed"),
        (T0 + 45 * MINUTE, "open"),
    ]
    assert changes_of(store, "link:PU1", "speed") == [
        (T0 + 15 * MINUTE, 0.0),
        (T0 + 60 * MINUTE, 1.0),
    ]
    assert [version.t for version in sync_versions(store)] == [
        T0 + 15 * MINUTE,
        T0 + 45 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    status = store.get("link:PU1").provenance_of("status")
    assert (status.source, status.t_valid) == ("plant", T0 + 40 * MINUTE)

    # Nothing is left over for the next run.
    head = store.head()
    assert run(twin, Sources(**tables)).versions_written == 0
    assert store.head() == head


def test_a_bad_first_reading_does_not_keep_its_object_out_of_the_twin(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    rows = [
        # The tank's first reading is beyond its bounds, in the very interval in
        # which the registry describes the tank. The check that fails names no
        # property, so it cannot be told from it which of the two is wrong.
        ("T1.level", T0 + 10 * MINUTE, 9.5),
        ("T1.level", T0 + 20 * MINUTE, 1.0),
        ("J1.demand", T0 + 10 * MINUTE, 5.0),
    ]
    twin = sync_twin(network_sources(), sync=BACKFILL)
    report = run(twin, Sources(**network_tables(rows)))
    store = twin.store

    assert report.status == "succeeded"
    # The reading is set aside; the tank itself, and the pipes that end at it, go in.
    tank = objects(store, "Tank")["node:T1"]
    assert (tank["name"], tank["max_level"], tank["level"]) == ("Hill tank", 6.0, 1.0)
    assert set(objects(store, "Pipe")) == {"link:P1", "link:P2", "link:P3"}
    assert changes_of(store, "node:T1", "level") == [
        (T0 + 15 * MINUTE, None),
        (T0 + 30 * MINUTE, 1.0),
    ]
    counts = {
        label: (count.rows_written, count.rows_rejected) for label, count in report.mappings.items()
    }
    assert counts["registry.tanks"] == (1, 0)
    assert counts["scada.levels"] == (1, 1)
    assert report.rows_rejected == 1
    _, lines = rejects_of(tmp_path, report.rejects_location)
    assert [(line["mapping"], line["uid"], line["rule"], line["t"]) for line in lines] == [
        ("scada.levels", "node:T1", "check", "2026-01-05T00:15:00Z")
    ]


def test_a_broken_rule_that_names_a_property_is_laid_on_the_mapping_that_wrote_it(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    states = [
        # Beyond the pump's range, in the interval in which the registry describes the pump.
        (T0 + 10 * MINUTE, "open", 5.0),
        (T0 + 20 * MINUTE, "open", 1.0),
    ]
    tables = {
        "registry": {
            "junctions": table(JUNCTIONS),
            "pumps": table([{"id": "PU1", "from_node": "J1", "to_node": "J2"}]),
        },
        "plant": {"pump_state": pump_state(states)},
    }
    twin = sync_twin(pump_sources(), sync=BACKFILL)
    report = run(twin, Sources(**tables))
    store = twin.store

    pump = objects(store, "Pump")["link:PU1"]
    assert (pump["start_node"], pump["status"], pump["speed"]) == ("node:J1", "open", 1.0)
    # The first row of the feed went as a whole: its status too is first seen later.
    assert changes_of(store, "link:PU1", "status") == [
        (T0 + 15 * MINUTE, None),
        (T0 + 30 * MINUTE, "open"),
    ]
    assert report.mappings["registry.pumps"].rows_rejected == 0
    assert report.mappings["plant.pump_state"].rows_rejected == 1
    _, lines = rejects_of(tmp_path, report.rejects_location)
    assert [(line["mapping"], line["property"], line["rule"]) for line in lines] == [
        ("plant.pump_state", "speed", "range")
    ]


def test_a_reading_of_an_object_the_twin_does_not_have_is_set_aside(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    rows = [
        ("T1.level", T0 + 10 * MINUTE, 1.0),
        ("T9.level", T0 + 10 * MINUTE, 3.0),
        ("T9.level", T0 + 25 * MINUTE, 3.5),
        ("T1.level", T0 + 25 * MINUTE, 1.5),
    ]
    twin = sync_twin(network_sources(), sync=BACKFILL)
    tables = network_tables(rows)
    signals = tables["registry"]["signals"].to_pylist()
    signals.append({"signal": "T9.level", "node": "T9", "quantity": "level"})
    tables["registry"]["signals"] = table(signals)
    report = run(twin, Sources(**tables))

    assert report.status == "succeeded"
    assert set(objects(twin.store, "Tank")) == {"node:T1"}
    assert changes_of(twin.store, "node:T1", "level") == [
        (T0 + 15 * MINUTE, 1.0),
        (T0 + 30 * MINUTE, 1.5),
    ]
    levels = report.mappings["scada.levels"]
    assert (levels.rows_read, levels.rows_written, levels.rows_rejected) == (4, 2, 2)
    _, lines = rejects_of(tmp_path, report.rejects_location)
    assert {(line["uid"], line["rule"]) for line in lines} == {("node:T9", "required")}
    assert sorted(line["t"] for line in lines) == ["2026-01-05T00:15:00Z", "2026-01-05T00:30:00Z"]


def test_a_reading_without_a_time_is_set_aside(sync_twin: MakeTwin, run: Run, tmp_path: Path):
    feeds = {
        "registry": source({"tanks": registry()["mappings"]["tanks"]}),
        "levels": source(
            {
                "levels": {
                    "dataset": "tank_levels",
                    "object_type": "Tank",
                    "identity": ["tank"],
                    "valid_time": "at",
                    "properties": columns("level"),
                }
            }
        ),
    }
    levels = [
        {"tank": "T1", "at": "2026-01-05 00:10:00", "level": 1.0},
        {"tank": "T1", "at": "the day before yesterday", "level": 1.5},
        {"tank": "T1", "at": None, "level": 2.5},
        {"tank": "T1", "at": "2026-01-05T00:25:00+00:00", "level": 2.0},
    ]
    tables = {
        "registry": {"tanks": table([tank("T1", "Hill tank", 0.5, 6.0)])},
        "levels": {"tank_levels": table(levels)},
    }
    twin = sync_twin(feeds, sync=BACKFILL)
    report = run(twin, Sources(**tables))

    assert changes_of(twin.store, "node:T1", "level") == [
        (T0 + 15 * MINUTE, 1.0),
        (T0 + 30 * MINUTE, 2.0),
    ]
    counts = report.mappings["levels.levels"]
    assert (counts.rows_read, counts.rows_written, counts.rows_rejected) == (4, 2, 2)
    assert report.watermark == T0 + 25 * MINUTE
    text, lines = rejects_of(tmp_path, report.rejects_location)
    assert [(line["uid"], line["rule"], line["property"], line["t"]) for line in lines] == [
        ("node:T1", "valid_time", None, None),
        ("node:T1", "valid_time", None, None),
    ]
    assert "yesterday" not in text


def test_a_mapping_whose_every_row_is_wrong_fails_the_run_before_anything_is_written(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin({"registry": registry()})
    broken = [{**row, "elevation": "unknown"} for row in JUNCTIONS]
    tables = {
        "junctions": table(broken),
        "tanks": table(TANKS),
        "pipes": table([pipe("P1", "J1", "J2")]),
    }
    sources = Sources(registry=tables)
    head = twin.store.head()

    with pytest.raises(AllRejected) as caught:
        run(twin, sources)

    error = caught.value
    assert error.code == "sync.all_rejected"
    assert error.detail["mappings"] == ["registry.junctions"]
    report = error.detail["report"]
    assert (report["status"], report["rows_written"], report["rows_rejected"]) == ("failed", 0, 3)
    store = twin.store

    # Nothing of the data went in, not even the mappings that were sound.
    assert objects(store, "Junction") == {}
    assert objects(store, "Tank") == {}
    # One version records the failure, at the time the twin had reached.
    versions = sync_versions(store)
    assert [(version.seq, version.t, version.extra) for version in versions] == [
        (head.seq + 1, head.t, {})
    ]
    assert set(store.diff(head, "head")) == {"TwinSyncRun", "TwinSource"}
    recorded = objects(store, "TwinSyncRun")[report["run"]]
    assert recorded["status"] == "failed"
    assert recorded["error"] == "sync.all_rejected: every row of registry.junctions was set aside"
    assert (recorded["rows_rejected"], recorded["versions_written"]) == (3, 1)
    assert recorded["rejects_location"] == report["rejects_location"]
    feed = objects(store, "TwinSource")["twin_source:registry"]
    assert feed["status"] == "failing"
    assert feed["last_error"].startswith("sync.all_rejected: ")
    text, lines = rejects_of(tmp_path, report["rejects_location"])
    assert {(line["uid"], line["rule"]) for line in lines} == {
        ("node:J1", "type"),
        ("node:J2", "type"),
        ("node:J3", "type"),
    }
    assert "unknown" not in text

    # Once the source is put right the next run loads it and says so.
    sources.put("registry", "junctions", table(JUNCTIONS))
    mended = run(twin, sources)
    assert (mended.status, mended.rows_written, mended.rows_rejected) == ("succeeded", 5, 0)
    feed = objects(store, "TwinSource")["twin_source:registry"]
    assert (feed["status"], feed["last_error"]) == ("ok", None)
    assert objects(store, "TwinSyncRun")[mended.run]["status"] == "succeeded"


def test_a_mapping_whose_every_row_the_store_refuses_fails_the_run_it_was_written_in(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin({"registry": registry()})
    tables = {
        "junctions": table(JUNCTIONS),
        "tanks": table(TANKS),
        # Every pipe ends somewhere unknown; that is known only once the store says so.
        "pipes": table([pipe("P1", "J1", "J8"), pipe("P2", "J9", "J2")]),
    }

    with pytest.raises(AllRejected) as caught:
        run(twin, Sources(registry=tables))

    report = caught.value.detail["report"]
    assert caught.value.detail["mappings"] == ["registry.pipes"]
    assert (report["status"], report["versions_written"]) == ("failed", 1)
    assert (report["rows_written"], report["rows_rejected"]) == (4, 2)
    store = twin.store
    # What held was written, in the one version that also records the failure.
    assert set(objects(store, "Junction")) == {"node:J1", "node:J2", "node:J3"}
    assert objects(store, "Pipe") == {}
    assert len(sync_versions(store)) == 1
    recorded = objects(store, "TwinSyncRun")[report["run"]]
    assert recorded["status"] == "failed"
    assert recorded["error"].startswith("sync.all_rejected: ")
    assert (recorded["rows_written"], recorded["rows_rejected"]) == (4, 2)
    feed = objects(store, "TwinSource")["twin_source:registry"]
    assert feed["status"] == "failing"
    _, lines = rejects_of(tmp_path, report["rejects_location"])
    assert {(line["uid"], line["rule"]) for line in lines} == {
        ("link:P1", "reference"),
        ("link:P2", "reference"),
    }
