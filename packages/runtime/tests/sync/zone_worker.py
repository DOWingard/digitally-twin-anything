"""Loads a short history around midnight UTC into a twin of its own and prints what the
twin then holds, as one line of JSON.

Run as a process of its own, so that the time zone of the process can be chosen:

    python zone_worker.py <database> <directory>

The store is the one named by the `DTA_STORE_*` variables; only the database differs.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
from dta_core.log import trace  # noqa: E402
from dta_core.store import StoreSettings  # noqa: E402
from dta_runtime.sync import load  # noqa: E402
from dta_runtime.twin import Bundle, install_twin  # noqa: E402
from sync_testkit import (  # noqa: E402
    T0,
    TIME,
    TRACE,
    WATER_ONTOLOGY,
    Sources,
    columns,
    runtime_settings,
    source,
    table,
    water_manifest,
)

HOUR = {"backfill_interval": 3600}
TYPES = ("Junction", "Tank", "Sensor", "Reading", "TwinDataset", "TwinSource", "TwinSyncRun")

# Readings on both sides of midnight UTC, which is the afternoon before in America.
READINGS = [
    (datetime(2026, 1, 5, 23, 40, tzinfo=UTC), 4.0),
    (datetime(2026, 1, 5, 23, 59, 59, tzinfo=UTC), 4.5),
    # Exactly midnight: the last instant of the interval that ends there.
    (datetime(2026, 1, 6, 0, 0, tzinfo=UTC), 5.0),
    (datetime(2026, 1, 6, 0, 0, 1, tzinfo=UTC), 5.5),
    (datetime(2026, 1, 6, 0, 30, tzinfo=UTC), 6.0),
]
# Times written without a zone, as many sources hold them.
LEVELS = [
    {"tank": "T1", "at": "2026-01-05 23:50:00", "level": 1.0},
    {"tank": "T1", "at": "2026-01-06 00:10:00", "level": 2.0},
]
# Everything in it depends on the zone a timestamp is read in: the key, the day, the hour.
BY_DAY = (
    "SELECT 'S7-' || strftime(ts, '%Y%m%d-%H%M%S') AS reading, 'S7' AS sensor, ts, value, "
    "CAST(ts AS DATE) AS day, hour(ts) AS hour_of_day "
    'FROM "scada"."readings"'
)


def sources() -> dict[str, Any]:
    registry = source(
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
            "sensors": {
                "dataset": "sensors",
                "object_type": "Sensor",
                "identity": ["id"],
                "properties": columns("quantity", "active", measures="node"),
            },
        }
    )
    scada = source(
        {
            "readings": {
                "dataset": "by_day",
                "object_type": "Reading",
                "identity": ["reading"],
                "valid_time": "ts",
                "properties": columns("sensor", "value", observed_at="ts", sequence="hour_of_day"),
            },
            "seen": {
                "dataset": "by_day",
                "object_type": "Sensor",
                "identity": ["sensor"],
                "valid_time": "ts",
                "properties": columns(installed_on="day"),
            },
            "levels": {
                "dataset": "levels",
                "object_type": "Tank",
                "identity": ["tank"],
                "valid_time": "at",
                "properties": columns("level"),
            },
        },
        views={"by_day": BY_DAY},
    )
    return {"registry": registry, "scada": scada}


def tables() -> dict[str, dict[str, pa.Table]]:
    readings = pa.table(
        {
            "ts": pa.array([row[0] for row in READINGS], TIME),
            "value": pa.array([row[1] for row in READINGS], pa.float64()),
        }
    )
    registry = {
        "junctions": table([{"id": "J1", "name": "North", "elevation": 12.5}]),
        "tanks": table(
            [{"id": "T1", "name": "Hill", "elevation": 40.0, "min_level": 0.5, "max_level": 6.0}]
        ),
        "sensors": table([{"id": "S7", "node": "J1", "quantity": "flow", "active": True}]),
    }
    return {"registry": registry, "scada": {"readings": readings, "levels": table(LEVELS)}}


def plain(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def main(database: str, directory: Path) -> None:
    text = WATER_ONTOLOGY.read_text(encoding="utf-8")
    settings = dataclasses.replace(StoreSettings.from_env(), database=database)
    manifest = water_manifest(text, sources=sources(), sync=HOUR)
    twin = install_twin(settings, Bundle(manifest=manifest, ontology_text=text), t=T0).twin
    try:
        with trace(TRACE):
            report = load(
                twin, runtime_settings(settings, directory), {}, connect=Sources(**tables())
            )
        store = twin.store
        held: dict[str, Any] = {}
        for name in TYPES:
            rows = store.table(name, prov=True).to_pylist()
            for row in rows:
                # The one stored value that is measured on the wall clock.
                row.pop("duration_ms", None)
            held[name] = sorted(
                ({key: plain(value) for key, value in row.items()} for row in rows),
                key=lambda row: row["uid"],
            )
        found = {
            # The zone DuckDB would have worked in had the pipeline not chosen one.
            "default_zone": duckdb.connect()
            .execute("SELECT current_setting('TimeZone')")
            .fetchall()[0][0],
            "local_midnight": datetime(2026, 1, 6).astimezone().isoformat(),
            "run": report.run,
            "versions": [
                [version.seq, version.t.isoformat(), version.run, dict(version.extra)]
                for version in store.versions(cause="sync")
            ],
            "held": held,
        }
    finally:
        twin.close()
    print(json.dumps(found, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
