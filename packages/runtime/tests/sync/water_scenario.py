"""A small water network as its sources hold it: a registry and a telemetry feed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

import pyarrow as pa
from sync_testkit import TIME, columns, source, table

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
    {
        "id": "P1",
        "from_node": "J1",
        "to_node": "J2",
        "length_m": 100.0,
        "diameter_mm": 150.0,
        "status": "open",
        "lined": True,
    },
    {
        "id": "P2",
        "from_node": "J2",
        "to_node": "T1",
        "length_m": 250.0,
        "diameter_mm": 200.0,
        "status": "closed",
        "lined": False,
    },
    {
        "id": "P3",
        "from_node": "T1",
        "to_node": "J3",
        "length_m": 80.5,
        "diameter_mm": 100.0,
        "status": "open",
        "lined": None,
    },
]
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


def sensors_table(rows: Iterable[Mapping[str, Any]] = SENSORS) -> pa.Table:
    return table(list(rows), tags=pa.list_(pa.string()), installed=pa.date32())


def registry_tables() -> dict[str, pa.Table]:
    return {
        "junctions": table(JUNCTIONS),
        "tanks": table(TANKS),
        "pipes": table(PIPES),
        "sensors": sensors_table(),
    }


def junction_mapping() -> dict[str, Any]:
    return {
        "dataset": "junctions",
        "object_type": "Junction",
        "identity": ["id"],
        "properties": columns("name", "elevation", "x", "y", "base_demand"),
    }


def tank_mapping() -> dict[str, Any]:
    return {
        "dataset": "tanks",
        "object_type": "Tank",
        "identity": ["id"],
        "properties": columns("name", "elevation", "min_level", "max_level", "volume_curve"),
    }


def registry_source() -> dict[str, Any]:
    return source(
        {
            "junctions": junction_mapping(),
            "tanks": tank_mapping(),
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


def readings(rows: Iterable[tuple[str, datetime, float | None]]) -> pa.Table:
    """Telemetry in long format: one row per signal and time."""
    listed = list(rows)
    return pa.table(
        {
            "signal": pa.array([row[0] for row in listed], pa.string()),
            "ts": pa.array([row[1] for row in listed], TIME),
            "value": pa.array([row[2] for row in listed], pa.float64()),
        }
    )


# Which object and property each telemetry signal reports.
SIGNALS = [
    {"signal": "T1.level", "node": "T1", "quantity": "level"},
    {"signal": "J1.demand", "node": "J1", "quantity": "demand"},
    {"signal": "J2.demand", "node": "J2", "quantity": "demand"},
]

# Long telemetry turned into one row per node and time, one column per quantity.
PIVOT = (
    "SELECT s.node, r.ts, "
    "max(r.value) FILTER (WHERE s.quantity = 'level') AS level, "
    "max(r.value) FILTER (WHERE s.quantity = 'demand') AS demand "
    'FROM "scada"."readings" r JOIN "registry"."signals" s ON s.signal = r.signal '
    "GROUP BY s.node, r.ts"
)


def scada_source() -> dict[str, Any]:
    """Telemetry mapped through the pivot onto tank levels and junction demands."""
    return source(
        {
            "levels": {
                "dataset": "by_node",
                "object_type": "Tank",
                "identity": ["node"],
                "valid_time": "ts",
                "filter": "node LIKE 'T%'",
                "properties": columns("level"),
            },
            "demands": {
                "dataset": "by_node",
                "object_type": "Junction",
                "identity": ["node"],
                "valid_time": "ts",
                "filter": "node LIKE 'J%'",
                "properties": columns("demand"),
            },
        },
        views={"by_node": PIVOT},
    )


def network_sources() -> dict[str, Any]:
    """Registry and telemetry together: the manifest's `sources` section."""
    return {"registry": registry_source(), "scada": scada_source()}


def network_tables(rows: Iterable[tuple[str, datetime, float | None]]) -> dict[str, Any]:
    registry = registry_tables()
    registry["signals"] = table(SIGNALS)
    return {"registry": registry, "scada": {"readings": readings(rows)}}
