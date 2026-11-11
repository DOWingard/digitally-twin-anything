"""Loading objects that carry no time of their own: registries, topology, settings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pyarrow as pa
import pytest
from dta_runtime.sync import MappingInvalid
from dta_runtime.twin import Twin
from sync_testkit import T0, Sources, columns, objects, source, sync_versions, table

JUNCTIONS = [
    {"id": "J1", "name": "North", "elevation": 12.5, "x": 0.0, "y": 0.0, "base_demand": 1.5},
    {"id": "J2", "name": "Mid", "elevation": 10.0, "x": 100.0, "y": 0.0, "base_demand": 0.0},
    {"id": "J3", "name": "South", "elevation": 8.25, "x": 200.0, "y": 50.0, "base_demand": None},
]
TANKS = [
    {
        "id": "T1",
        "name": "Hill tank",
        "elevation": 40.0,
        "min_level": 0.5,
        "max_level": 6.0,
        "volume_curve": [0.0, 120.5, 250.0],
    },
]
PIPES = [
    {"id": "P1", "from_node": "J1", "to_node": "J2", "length_m": 100.0, "diameter_mm": 150.0},
    {"id": "P2", "from_node": "J2", "to_node": "T1", "length_m": 250.0, "diameter_mm": 200.0},
    {"id": "P3", "from_node": "T1", "to_node": "J3", "length_m": 80.5, "diameter_mm": 100.0},
]
PIPE_DETAILS = {"P1": ("open", True), "P2": ("closed", False), "P3": ("open", None)}
SENSORS = [
    {
        "sensor_id": 7,
        "node": "T1",
        "quantity": "level",
        "serial": "SN-000007",
        "installed": date(2024, 3, 9),
        "active": True,
        "interval_s": 900,
        "tags": ["scada", "critical"],
    },
    {
        "sensor_id": 8,
        "node": "J2",
        "quantity": "pressure",
        "serial": None,
        "installed": None,
        "active": False,
        "interval_s": None,
        "tags": None,
    },
]


def registry_tables() -> dict[str, pa.Table]:
    pipes = [
        {**pipe, "status": PIPE_DETAILS[pipe["id"]][0], "lined": PIPE_DETAILS[pipe["id"]][1]}
        for pipe in PIPES
    ]
    return {
        "junctions": table(JUNCTIONS),
        "tanks": table(TANKS),
        "pipes": table(pipes),
        "sensors": table(SENSORS, tags=pa.list_(pa.string()), installed=pa.date32()),
    }


def registry_source() -> dict[str, Any]:
    return source(
        {
            "junctions": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation", "x", "y", "base_demand"),
            },
            "tanks": {
                "dataset": "tanks",
                "object_type": "Tank",
                "identity": ["id"],
                "properties": columns(
                    "name", "elevation", "min_level", "max_level", "volume_curve"
                ),
            },
            "pipes": {
                "dataset": "pipes",
                "object_type": "Pipe",
                "identity": ["id"],
                "properties": columns(
                    "status",
                    "lined",
                    start_node="from_node",
                    end_node="to_node",
                    length="length_m",
                    diameter="diameter_mm",
                ),
            },
            "sensors": {
                "dataset": "sensors",
                "object_type": "Sensor",
                "identity": ["sensor_id"],
                "kind": "assumed",
                "properties": columns(
                    "quantity",
                    "serial",
                    "active",
                    "tags",
                    measures="node",
                    installed_on="installed",
                    sample_interval="interval_s",
                ),
            },
        }
    )


def test_objects_of_several_types_are_loaded_as_one_version(
    sync_twin: Callable[..., Twin], run: Callable[..., Any]
):
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

    store = twin.store
    version = sync_versions(store)[-1]
    assert (version.t, version.actor, version.run) == (T0, "sync", report.run)

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


def test_provenance_names_the_source_the_kind_and_the_version_time(
    sync_twin: Callable[..., Twin], run: Callable[..., Any]
):
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


def test_abstract_type_and_unknown_property_are_refused_before_reading(
    sync_twin: Callable[..., Twin], run: Callable[..., Any]
):
    broken = registry_source()
    broken["mappings"]["junctions"]["object_type"] = "Node"
    broken["mappings"]["tanks"]["properties"]["colour"] = {"column": "name"}
    twin = sync_twin({"registry": broken})
    sources = Sources(registry=registry_tables())
    head = twin.store.head()

    with pytest.raises(MappingInvalid) as caught:
        run(twin, sources)

    assert caught.value.code == "sync.mapping_invalid"
    found = {(p["where"], p["message"]) for p in caught.value.detail["problems"]}
    assert (
        "sources.registry.mappings.junctions.object_type",
        "Node is abstract; map onto one of Junction, Reservoir, Tank",
    ) in found
    assert (
        "sources.registry.mappings.tanks.properties.colour",
        "Tank has no property colour",
    ) in found
    assert sources.opened == []
    assert twin.store.head() == head
