"""The HTTP connector against a real plant's interface: its datasets, a paged read
of readings with `since`, and its clock. Skipped unless `DTA_PLANT_URL` (the base
address) and `DTA_PLANT_KEY` (the integration key) are set."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import timedelta

import pyarrow as pa
import pytest
from dta_connect_testkit import CapturedLogs
from dta_core.connect import open_connector, probe
from dta_core.manifest.models import Source
from dta_core.sources import Connector, TimeSource

PLANT_ENV = ("DTA_PLANT_URL", "DTA_PLANT_KEY")
# Small pages and a window of a few scans, so that the read takes several pages.
PAGE_ROWS = 25
WINDOW = timedelta(hours=2)

SOURCE = Source(
    connector="http",
    location="${DTA_PLANT_URL}",
    options={
        "headers": {"X-API-Key": "${DTA_PLANT_KEY}"},
        "clock": {"path": "/api/v1/time", "field": "time"},
        "datasets": {
            "readings": {
                "path": "/api/v1/telemetry",
                "params": {"limit": PAGE_ROWS},
                "records": "data",
                "paging": {
                    "type": "cursor",
                    "cursor_path": "next_cursor",
                    "cursor_param": "cursor",
                },
                "since": {"param": "start", "format": "iso"},
                "columns": {
                    "sensor_id": "string",
                    "timestamp": "timestamp[us, tz=UTC]",
                    "value": "double",
                    "quality": "string",
                },
            },
            "sensors": {"path": "/api/v1/sensors", "records": "data"},
            "controls": {
                "path": "/api/v1/controls",
                "records": "data",
                "columns": {
                    "asset_id": "string",
                    "asset_type": "string",
                    "mode": "string",
                    "bounds": "string",
                    "setpoint": "string",
                },
            },
        },
    },
)


@pytest.fixture
def plant() -> Iterator[Connector]:
    missing = [name for name in PLANT_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip("needs a real plant interface: set " + ", ".join(missing))
    env = {name: os.environ[name] for name in PLANT_ENV}
    with open_connector("plant", SOURCE, env) as connector:
        yield connector


def test_datasets_paged_readings_since_and_the_clock(
    plant: Connector, captured_logs: CapturedLogs
) -> None:
    assert plant.datasets() == ["controls", "readings", "sensors"]
    assert isinstance(plant, TimeSource)
    now = plant.now()
    assert now.utcoffset() is not None

    sensors = {row["sensor_id"] for table in plant.read("sensors") for row in table.to_pylist()}
    assert sensors

    since = now - WINDOW
    tables = list(plant.read("readings", cursor_column="timestamp", since=since))
    rows = [row for table in tables for row in table.to_pylist()]
    assert tables[0].schema.field("timestamp").type == pa.timestamp("us", tz="UTC")
    assert rows, "the plant returned no readings for the last scans"
    # The source's start is inclusive; the rows at `since` itself are not newer.
    assert min(row["timestamp"] for row in rows) > since
    assert max(row["timestamp"] for row in rows) <= now
    assert {row["sensor_id"] for row in rows} <= sensors
    unique = {id(r): r for r in captured_logs.records}.values()
    pages = [
        r
        for r in unique
        if r.getMessage() == "page read" and r.data["dataset"] == "readings"  # type: ignore[attr-defined]
    ]
    assert len(pages) >= 2, "the read did not page"

    # Controls need the key; their nested bounds and setpoints are kept as JSON text.
    controls = [row for table in plant.read("controls") for row in table.to_pylist()]
    assert controls
    assert all(isinstance(json.loads(row["bounds"]), dict) for row in controls)

    report = probe(plant)
    assert report.ok, report.as_dict()
