"""Loading objects that carry no time of their own: registries, topology, settings."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pyarrow as pa
import pytest
from dta_runtime.sync import MappingInvalid
from dta_runtime.twin import Twin
from sync_testkit import T0, Sources, columns, objects, source, sync_versions, table
from water_scenario import (
    JUNCTIONS,
    SENSORS,
    TANKS,
    registry_source,
    registry_tables,
    sensors_table,
)

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]


def test_objects_of_several_types_are_loaded_as_one_version(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": registry_source()})
    sources = Sources(registry=registry_tables())
    report = run(twin, sources)

    assert report.status == "succeeded"
    assert report.kind == "incremental"
    assert (report.versions_written, report.rows_read) == (1, 9)
    assert (report.rows_written, report.rows_rejected) == (9, 0)
    assert report.rejects_location is None
    assert report.first_seq == report.last_seq == twin.store.head().seq
    assert report.mappings["registry.pipes"].as_dict() == {
        "object_type": "Pipe",
        "rows_read": 3,
        "rows_written": 3,
        "rows_rejected": 0,
        "rows_deleted": 0,
    }
    assert report.sources["registry"].datasets == {
        "junctions": 3,
        "tanks": 1,
        "pipes": 3,
        "sensors": 2,
    }
    assert sources["registry"].closed == 1
    # Each dataset is read once, whole.
    assert sorted(read["dataset"] for read in sources["registry"].reads) == [
        "junctions",
        "pipes",
        "sensors",
        "tanks",
    ]
    assert all(read["since"] is None for read in sources["registry"].reads)
    json.dumps(report.as_dict())

    store = twin.store
    version = sync_versions(store)[-1]
    assert (version.t, version.actor, version.run) == (T0, "sync", report.run)
    assert version.extra == {}

    junctions = objects(store, "Junction")
    assert set(junctions) == {"node:J1", "node:J2", "node:J3"}
    assert junctions["node:J1"] == {
        "name": "North",
        "elevation": 12.5,
        "x": 0.0,
        "y": 0.0,
        "base_demand": 1.5,
        "demand": None,
        "pressure": None,
    }
    assert junctions["node:J3"]["base_demand"] is None

    tank = objects(store, "Tank")["node:T1"]
    assert tank["volume_curve"] == [0.0, 120.5, 250.0]
    assert (tank["min_level"], tank["max_level"], tank["level"]) == (0.5, 6.0, None)

    # A pipe refers to nodes through their abstract class: a junction or a tank alike.
    pipes = objects(store, "Pipe")
    assert pipes["link:P1"]["start_node"] == "node:J1"
    assert pipes["link:P3"]["start_node"] == "node:T1"
    assert pipes["link:P2"]["end_node"] == "node:T1"
    assert [pipes[uid]["status"] for uid in sorted(pipes)] == ["open", "closed", "open"]
    assert [pipes[uid]["lined"] for uid in sorted(pipes)] == [True, False, None]
    edges = store.edges(["Pipe"]).to_pylist()
    assert {(e["src"], e["dst"], e["link_uid"]) for e in edges} == {
        ("node:J1", "node:J2", "link:P1"),
        ("node:J2", "node:T1", "link:P2"),
        ("node:T1", "node:J3", "link:P3"),
    }

    sensors = objects(store, "Sensor")
    assert sensors["sensor:7"] == {
        "measures": "node:T1",
        "quantity": "level",
        "serial": "SN-000007",
        "installed_on": date(2024, 3, 9),
        "active": True,
        "sample_interval": 900,
        "tags": ["scada", "critical"],
        "notes": None,
    }
    assert sensors["sensor:8"]["active"] is False
    assert sensors["sensor:8"]["tags"] is None


def test_provenance_names_the_source_the_kind_and_the_version_time(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": registry_source()})
    run(twin, Sources(registry=registry_tables()))

    pipe = twin.store.get("link:P1")
    assert set(pipe.provenance) == {"*"}
    found = pipe.provenance_of("length")
    assert (found.source, found.kind, found.t_valid, found.confidence) == (
        "registry",
        "observed",
        T0,
        1.0,
    )
    # The kind is the mapping's.
    assert twin.store.get("sensor:7").provenance_of("quantity").kind == "assumed"


def test_a_rerun_on_an_unchanged_source_writes_nothing(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": registry_source()})
    sources = Sources(registry=registry_tables())
    first = run(twin, sources)
    head = twin.store.head()

    again = run(twin, sources)

    assert again.status == "succeeded"
    assert (again.versions_written, again.rows_written, again.rows_rejected) == (0, 0, 0)
    assert (again.first_seq, again.last_seq) == (None, None)
    assert again.rows_read == first.rows_read
    assert twin.store.head() == head
    # A run that wrote no version leaves no object either.
    assert set(objects(twin.store, "TwinSyncRun")) == {first.run}


def test_only_a_changed_row_is_written_and_null_is_a_value(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": registry_source()})
    sources = Sources(registry=registry_tables())
    run(twin, sources)
    before = twin.store.head()

    changed = [dict(row) for row in JUNCTIONS]
    changed[1]["elevation"] = 11.0  # J2
    changed[0]["base_demand"] = None  # J1: a value that is gone is written as null
    sources.put("registry", "junctions", table(changed))
    report = run(twin, sources)

    assert (report.versions_written, report.rows_written) == (1, 2)
    assert report.mappings["registry.junctions"].rows_written == 2
    assert report.mappings["registry.pipes"].rows_written == 0
    difference = twin.store.diff(before, "head")
    # The source's own record is as it was: same status, same data time.
    assert set(difference) == {"Junction", "TwinSyncRun", "TwinDataset"}
    rows = {row["uid"]: row for row in difference["Junction"].to_pylist()}
    assert set(rows) == {"node:J1", "node:J2"}
    assert rows["node:J2"]["elevation"] == 11.0
    assert rows["node:J2"]["_before"]["elevation"] == 10.0
    assert rows["node:J1"]["base_demand"] is None
    assert rows["node:J1"]["_before"]["base_demand"] == 1.5
    # Written at the head's time: the source carries no time of its own.
    assert twin.store.head().t == before.t


def test_views_expressions_filters_and_composite_identity(sync_twin: MakeTwin, run: Run):
    stations = table(
        [
            {"region": "north", "code": "A1", "label": "alpha", "kind": "junction", "elev_ft": 100},
            {"region": "north", "code": "A2", "label": "beta", "kind": "junction", "elev_ft": 50},
            {"region": "south", "code": "A1", "label": "gamma", "kind": "junction", "elev_ft": 10},
            {"region": "south", "code": "Z9", "label": "depot", "kind": "office", "elev_ft": 0},
        ]
    )
    coordinates = table(
        [
            {"station": "north/A1", "easting": 10.0, "northing": 20.0, "surveyed": 2019},
            {"station": "north/A2", "easting": 30.0, "northing": 40.0, "surveyed": 2020},
            {"station": "south/A1", "easting": 50.0, "northing": 60.0, "surveyed": 2021},
        ]
    )
    segments = table(
        [
            {"seg": 1, "a_region": "north", "a_code": "A1", "b_region": "north", "b_code": "A2"},
            {"seg": 2, "a_region": "north", "a_code": "A2", "b_region": "south", "b_code": "A1"},
        ]
    )
    registry = source(
        {
            "stations": {
                "dataset": "located",
                "object_type": "Junction",
                "identity": ["region", "code"],
                "filter": "kind = 'junction'",
                "properties": {
                    "name": {"expression": "upper(label) || ' ' || code"},
                    "elevation": {"expression": "elev_ft * 0.3048"},
                    "x": {"column": "easting"},
                    "y": {"column": "northing"},
                },
            },
            "segments": {
                "dataset": "segments",
                "object_type": "Pipe",
                "identity": ["seg"],
                "properties": {
                    "start_node": {"expression": "a_region || '|' || a_code"},
                    "end_node": {"expression": "b_region || '|' || b_code"},
                    "length": {"expression": "100.0 * seg"},
                    "diameter": {"expression": "150"},
                },
            },
        },
        views={
            # Joins a dataset of this source with one of another source.
            "located": (
                "SELECT s.region, s.code, s.label, s.kind, s.elev_ft, c.easting, c.northing "
                'FROM "registry"."stations" s LEFT JOIN "gis"."coordinates" c '
                "ON c.station = s.region || '/' || s.code"
            ),
        },
    )
    twin = sync_twin({"registry": registry, "gis": source({})})
    sources = Sources(
        registry={"stations": stations, "segments": segments}, gis={"coordinates": coordinates}
    )

    report = run(twin, sources)

    assert sorted(sources.opened) == ["gis", "registry"]
    assert report.mappings["registry.stations"].rows_read == 3  # the office is filtered out
    junctions = objects(twin.store, "Junction")
    assert set(junctions) == {"node:north|A1", "node:north|A2", "node:south|A1"}
    assert junctions["node:north|A1"]["name"] == "ALPHA A1"
    assert junctions["node:north|A1"]["elevation"] == pytest.approx(30.48)
    assert (junctions["node:south|A1"]["x"], junctions["node:south|A1"]["y"]) == (50.0, 60.0)
    pipes = objects(twin.store, "Pipe")
    assert pipes["link:2"]["start_node"] == "node:north|A2"
    assert pipes["link:2"]["end_node"] == "node:south|A1"
    assert (pipes["link:2"]["length"], pipes["link:2"]["diameter"]) == (200.0, 150.0)
    # The one column of a dataset read that no mapping, view or expression mentions.
    assert report.unmapped_columns == ["gis.coordinates.surveyed"]
    stored = objects(twin.store, "TwinSyncRun")[report.run]
    assert stored["unmapped_columns"] == ["gis.coordinates.surveyed"]
    assert stored["sources"] == ["gis", "registry"]
    assert stored["rows_read"] == 9


def pump_source(curve_view: str) -> dict[str, Any]:
    return source(
        {
            "nodes": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation"),
            },
            "pumps": {
                "dataset": "pumps_with_curve",
                "object_type": "Pump",
                "identity": ["pump_id"],
                "properties": columns("curve", start_node="from_node", end_node="to_node"),
            },
            "tanks": {
                "dataset": "tank_volumes",
                "object_type": "Tank",
                "identity": ["tank_id"],
                "properties": columns(
                    "name", "elevation", "min_level", "max_level", "volume_curve"
                ),
            },
        },
        views={
            "pump_curves": curve_view,
            "pumps_with_curve": (
                'SELECT p.*, c.curve FROM "registry"."pumps" p '
                'LEFT JOIN "registry"."pump_curves" c USING (pump_id)'
            ),
            # A list of scalars built in a view feeds a multivalued property.
            "tank_volumes": (
                "SELECT tank_id, any_value(name) AS name, 30.0 AS elevation, 0.0 AS min_level, "
                "5.0 AS max_level, list(volume ORDER BY level_m) AS volume_curve "
                'FROM "registry"."tank_volume" GROUP BY tank_id'
            ),
        },
    )


CURVE_VIEW = (
    "SELECT pump_id, list({'flow': flow_lps, 'head': head_m} ORDER BY point_index) AS curve "
    'FROM "registry"."pump_curve_point" GROUP BY pump_id'
)


def pump_tables(second_head: float = 42.0) -> dict[str, pa.Table]:
    return {
        "junctions": table(JUNCTIONS),
        "pumps": table(
            [
                {"pump_id": "PU1", "from_node": "J1", "to_node": "J2"},
                {"pump_id": "PU2", "from_node": "J2", "to_node": "J3"},
            ]
        ),
        "pump_curve_point": table(
            [
                {"pump_id": "PU1", "point_index": 2, "flow_lps": 10.5, "head_m": second_head},
                {"pump_id": "PU1", "point_index": 1, "flow_lps": 0.0, "head_m": 50.0},
                {"pump_id": "PU1", "point_index": 3, "flow_lps": 20.0, "head_m": 30.25},
            ]
        ),
        "tank_volume": table(
            [
                {"tank_id": "T1", "name": "Hill tank", "level_m": 1.0, "volume": 120.5},
                {"tank_id": "T1", "name": "Hill tank", "level_m": 0.0, "volume": 0.0},
                {"tank_id": "T1", "name": "Hill tank", "level_m": 2.0, "volume": 250.0},
            ]
        ),
    }


def test_structured_values_built_in_a_view_reach_the_store_as_lists_and_objects(
    sync_twin: MakeTwin, run: Run
):
    twin = sync_twin({"registry": pump_source(CURVE_VIEW)})
    sources = Sources(registry=pump_tables())
    report = run(twin, sources)

    assert report.rows_rejected == 0
    first = twin.store.get("link:PU1").values
    assert first["curve"] == [
        {"flow": 0.0, "head": 50.0},
        {"flow": 10.5, "head": 42.0},
        {"flow": 20.0, "head": 30.25},
    ]
    assert twin.store.get("link:PU2").values["curve"] is None
    assert objects(twin.store, "Tank")["node:T1"]["volume_curve"] == [0.0, 120.5, 250.0]

    # Compared by value: the store renders 50.0 as 50, which is not a change.
    head = twin.store.head()
    again = run(twin, sources)
    assert again.versions_written == 0
    assert twin.store.head() == head

    # A point that moved is one.
    changed = run(twin, Sources(registry=pump_tables(second_head=41.0)))
    assert (changed.versions_written, changed.rows_written) == (1, 1)
    assert twin.store.get("link:PU1").values["curve"][1] == {"flow": 10.5, "head": 41.0}


def test_the_order_of_keys_in_a_structured_value_is_not_a_change(sync_twin: MakeTwin, run: Run):
    registry = source(
        {
            "nodes": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation"),
            },
            "pumps": {
                "dataset": "pumps",
                "object_type": "Pump",
                "identity": ["pump_id"],
                "properties": columns("curve", start_node="from_node", end_node="to_node"),
            },
        }
    )

    def tables(curve: str) -> dict[str, pa.Table]:
        pump = {"pump_id": "PU1", "from_node": "J1", "to_node": "J2", "curve": curve}
        return {"junctions": table(JUNCTIONS), "pumps": table([pump])}

    twin = sync_twin({"registry": registry})
    # The source holds the curve as JSON text.
    run(twin, Sources(registry=tables('[{"head": 50, "flow": 0}, {"head": 42.5, "flow": 10}]')))
    assert twin.store.get("link:PU1").values["curve"] == [
        {"head": 50, "flow": 0},
        {"head": 42.5, "flow": 10},
    ]
    head = twin.store.head()

    reordered = '[{"flow": 0.0, "head": 50.0},   {"flow": 10, "head": 42.5}]'
    again = run(twin, Sources(registry=tables(reordered)))
    assert again.versions_written == 0
    assert twin.store.head() == head

    moved = '[{"flow": 0, "head": 50}, {"flow": 11, "head": 42.5}]'
    report = run(twin, Sources(registry=tables(moved)))
    assert report.rows_written == 1
    assert twin.store.get("link:PU1").values["curve"][1]["flow"] == 11


def sensor_source(mode: str) -> dict[str, Any]:
    registry = registry_source()
    registry["mappings"]["sensors"]["mode"] = mode
    return registry


def test_snapshot_deletes_objects_that_left_the_dataset(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": sensor_source("snapshot")})
    sources = Sources(registry=registry_tables())
    run(twin, sources)
    assert set(objects(twin.store, "Sensor")) == {"sensor:7", "sensor:8"}
    before = twin.store.head()

    remaining = [dict(SENSORS[0]), {**SENSORS[1], "sensor_id": 9, "node": "J3"}]
    sources.put("registry", "sensors", sensors_table(remaining))
    report = run(twin, sources)

    assert set(objects(twin.store, "Sensor")) == {"sensor:7", "sensor:9"}
    count = report.mappings["registry.sensors"]
    assert (count.rows_written, count.rows_deleted, count.rows_rejected) == (1, 1, 0)
    assert report.rows_written == 2
    # The object is still there at the version before.
    assert "sensor:8" in objects(twin.store, "Sensor", at=before)
    removed = twin.store.history("sensor:8", type="Sensor").column("_change").to_pylist()
    assert removed == ["added", "removed"]


def test_upsert_keeps_objects_that_left_the_dataset(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({"registry": sensor_source("upsert")})
    sources = Sources(registry=registry_tables())
    run(twin, sources)
    head = twin.store.head()

    sources.put("registry", "sensors", sensors_table(SENSORS[:1]))
    report = run(twin, sources)

    assert report.versions_written == 0
    assert twin.store.head() == head
    assert set(objects(twin.store, "Sensor")) == {"sensor:7", "sensor:8"}


def test_a_snapshot_deletion_of_an_object_still_referred_to_is_set_aside(
    sync_twin: MakeTwin, run: Run, settings_of: Callable[..., Any]
):
    registry = registry_source()
    registry["mappings"]["tanks"]["mode"] = "snapshot"
    twin = sync_twin({"registry": registry})
    sources = Sources(registry=registry_tables())
    run(twin, sources)
    head = twin.store.head()

    # The tank leaves its dataset, but pipes P2 and P3 still end at it.
    sources.put("registry", "tanks", table(TANKS).slice(0, 0))
    report = run(twin, sources)

    assert "node:T1" in objects(twin.store, "Tank")
    count = report.mappings["registry.tanks"]
    assert (count.rows_deleted, count.rows_rejected) == (0, 1)
    assert report.versions_written == 0
    assert twin.store.head() == head
    path = settings_of(twin).artifacts_dir / report.rejects_location
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(line["uid"], line["rule"], line["mapping"]) for line in lines] == [
        ("node:T1", "referenced", "registry.tanks")
    ]


def test_snapshot_is_refused_when_two_mappings_feed_the_type(sync_twin: MakeTwin, run: Run):
    registry = sensor_source("snapshot")
    registry["mappings"]["more_sensors"] = {
        **registry["mappings"]["sensors"],
        "dataset": "more_sensors",
        "mode": "upsert",
    }
    twin = sync_twin({"registry": registry})
    sources = Sources(registry=registry_tables())
    head = twin.store.head()

    with pytest.raises(MappingInvalid) as caught:
        run(twin, sources)

    problems = caught.value.detail["problems"]
    assert [p["where"] for p in problems] == ["sources.registry.mappings.sensors.mode"]
    assert "snapshot needs to be the only mapping of Sensor" in problems[0]["message"]
    assert sources.opened == []
    assert twin.store.head() == head
