"""What the twin records about a run, and that none of it depends on when or where it ran."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dta_core.store import StoreSettings
from dta_runtime.twin import Twin
from sync_testkit import MINUTE, T0, TRACE, Sources, objects, sync_versions, table
from telemetry import BACKFILL, READINGS
from water_scenario import JUNCTIONS, network_sources, network_tables

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]

WORKER = Path(__file__).with_name("zone_worker.py")
# Generous: the machine is shared, and each worker installs a twin of its own.
WORKER_TIMEOUT_S = 900
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def test_a_run_is_recorded_with_its_datasets_and_sources(sync_twin: MakeTwin, run: Run):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    tables = network_tables(READINGS)
    tables["registry"]["junctions"] = table([{**row, "surveyed_by": "crew 4"} for row in JUNCTIONS])
    report = run(twin, Sources(**tables))
    store = twin.store
    end = T0 + 60 * MINUTE

    recorded = objects(store, "TwinSyncRun")
    assert set(recorded) == {report.run}
    stored = recorded[report.run]
    duration = stored.pop("duration_ms")
    assert isinstance(duration, int) and duration >= 0
    assert stored == {
        "kind": "backfill",
        "status": "succeeded",
        # A run begins and ends at the times of its first and last version.
        "started_at": T0 + 15 * MINUTE,
        "finished_at": end,
        "versions_written": 4,
        "rejects_location": None,
        "unmapped_columns": ["registry.junctions.surveyed_by"],
        "watermark": T0 + 50 * MINUTE,
        "sources": ["registry", "scada"],
        "rows_read": 23,
        "rows_written": 12,
        "rows_rejected": 0,
        "rows_late": 0,
        # A load of existing history is not handed a clock.
        "clock_at": None,
        "error": None,
        "trace_id": TRACE,
    }
    assert report.run == "twin_sync_run:20260105T001500Z-" + report.run.rsplit("-", 1)[1]
    assert (report.rows_read, report.rows_written, report.versions_written) == (23, 12, 4)
    assert report.unmapped_columns == ["registry.junctions.surveyed_by"]
    assert report.as_dict()["watermark"] == "2026-01-05T00:50:00Z"

    datasets = objects(store, "TwinDataset")
    read = {name: 0 for name in ("junctions", "tanks", "pipes", "sensors", "signals")}
    read.update(junctions=3, tanks=1, pipes=3, sensors=2, signals=3)
    wanted = {
        f"twin_dataset:registry/{name}": {
            "source": "twin_source:registry",
            "name": name,
            "cursor_column": None,
            "cursor": None,
            "rows_read": rows,
            "last_read_at": end,
        }
        for name, rows in read.items()
    }
    wanted["twin_dataset:scada/readings"] = {
        "source": "twin_source:scada",
        "name": "readings",
        "cursor_column": None,
        "cursor": None,
        "rows_read": 11,
        "last_read_at": end,
    }
    # The view the time series are read from carries how far they were read.
    wanted["twin_dataset:scada/by_node"] = {
        "source": "twin_source:scada",
        "name": "by_node",
        "cursor_column": "ts",
        "cursor": "2026-01-05T00:50:00Z",
        "rows_read": 11,
        "last_read_at": end,
    }
    assert datasets == wanted

    feeds = objects(store, "TwinSource")
    assert feeds == {
        "twin_source:registry": {
            "connector": "custom",
            "description": None,
            "location": "memory://test",
            "watermark": None,
            "status": "ok",
            "last_success_at": end,
            "last_error": None,
        },
        "twin_source:scada": {
            "connector": "custom",
            "description": None,
            "location": "memory://test",
            "watermark": T0 + 50 * MINUTE,
            "status": "ok",
            "last_success_at": end,
            "last_error": None,
        },
    }

    # The pipeline's own records say that they are its own.
    for uid in (report.run, "twin_dataset:scada/by_node", "twin_source:scada"):
        origin = store.get(uid).provenance_of("status" if "source" in uid else "name")
        assert (origin.source, origin.kind) == ("sync", "inferred"), uid
    assert store.get(report.run).provenance_of("status").t_valid == end

    # How far the series was read is known after every version, not only the last.
    cursors = [
        objects(store, "TwinDataset", at=version)["twin_dataset:scada/by_node"]["cursor"]
        for version in sync_versions(store)
    ]
    assert cursors == [
        "2026-01-05T00:15:00Z",
        "2026-01-05T00:30:00Z",
        "2026-01-05T00:45:00Z",
        "2026-01-05T00:50:00Z",
    ]
    # The rest is recorded once, with the last version.
    assert objects(store, "TwinSyncRun", at=sync_versions(store)[-2]) == {}


def test_what_is_read_adds_up_over_runs(sync_twin: MakeTwin, run: Run):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    early = [row for row in READINGS if row[1] <= T0 + 30 * MINUTE]
    run(twin, Sources(**network_tables(early)))
    report = run(twin, Sources(**network_tables(READINGS)))

    datasets = objects(twin.store, "TwinDataset")
    # The registry and the telemetry under the view were read whole both times.
    assert datasets["twin_dataset:registry/pipes"]["rows_read"] == 6
    assert datasets["twin_dataset:scada/readings"]["rows_read"] == 6 + 11
    assert datasets["twin_dataset:scada/by_node"]["last_read_at"] == T0 + 60 * MINUTE
    runs = objects(twin.store, "TwinSyncRun")
    assert len(runs) == 2
    assert runs[report.run]["started_at"] == T0 + 45 * MINUTE
    assert runs[report.run]["versions_written"] == 2


def everything(twin: Twin) -> dict[str, Any]:
    """Every version of a twin and every object at every version, without wall-clock values."""
    store = twin.store
    versions = store.versions()
    found: dict[str, Any] = {
        "versions": [
            (v.seq, v.t, str(v.cause), v.actor, v.run, dict(v.extra), v.note) for v in versions
        ]
    }
    for version in versions:
        for object_type in twin.ontology.types:
            rows = store.table(object_type.name, at=version, prov=True).to_pylist()
            for row in rows:
                # The one stored value that is measured on the wall clock.
                row.pop("duration_ms", None)
            found[f"{version.seq}/{object_type.name}"] = sorted(rows, key=lambda row: row["uid"])
    return found


def times_in(value: Any) -> list[datetime]:
    """Every point in time found anywhere in a stored value, texts included."""
    if isinstance(value, datetime):
        return [value]
    if isinstance(value, str):
        return [datetime.fromisoformat(text) for text in STAMP.findall(value)]
    if isinstance(value, dict):
        return [found for item in value.values() for found in times_in(item)]
    if isinstance(value, list | tuple):
        return [found for item in value for found in times_in(item)]
    return []


def test_nothing_stored_comes_from_the_wall_clock_but_the_duration(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    rows = [
        *READINGS,
        # Refused by the store, so the run also leaves a rejects file.
        ("T1.level", T0 + 52 * MINUTE, 9.5),
    ]
    held = []
    rejects = []
    for _ in range(2):
        twin = sync_twin(network_sources(), sync=BACKFILL)
        tables = network_tables(rows)
        # A static row that changes between two runs, written at the time the twin reached.
        first = run(twin, Sources(**tables))
        changed = [dict(row) for row in JUNCTIONS]
        changed[0]["elevation"] = 13.0
        tables["registry"]["junctions"] = table(changed)
        second = run(twin, Sources(**tables))
        held.append(everything(twin))
        # The refused reading lies behind the cursor by then: only the first run names it.
        assert second.rejects_location is None
        file = tmp_path / "artifacts" / first.rejects_location
        rejects.append((first.rejects_location, file.read_text(encoding="utf-8")))
        # The second twin's run has the same name, so it writes the same file.
        file.unlink()

    one, other = held
    assert one["versions"] == other["versions"]
    assert set(one) == set(other)
    for key in one:
        assert one[key] == other[key], key
    assert rejects[0] == rejects[1]
    assert len(one["versions"]) == 2 + 4 + 1

    # And every time that is stored lies in the data, far from today.
    latest = T0 + timedelta(hours=1)
    stamps = times_in(one)
    assert len(stamps) > 100
    assert max(stamps) == latest
    assert min(stamps) >= T0


def worker(zone: str, settings: StoreSettings, directory: Path) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, str(WORKER), settings.database, str(directory)],
        env={**os.environ, "TZ": zone},
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_what_is_stored_does_not_depend_on_the_time_zone_of_the_process(
    new_settings: Callable[[], StoreSettings], tmp_path: Path
):
    west = worker("America/Los_Angeles", new_settings(), tmp_path / "west")
    utc = worker("UTC", new_settings(), tmp_path / "utc")

    # The two processes really did live in different zones.
    assert (west.pop("default_zone"), utc.pop("default_zone")) == ("America/Los_Angeles", "UTC")
    assert west.pop("local_midnight") == "2026-01-06T00:00:00-08:00"
    assert utc.pop("local_midnight") == "2026-01-06T00:00:00+00:00"

    assert west == utc
    # Keys, days and hours derived from a timestamp are those of UTC.
    readings = {row["uid"]: row for row in utc["held"]["Reading"]}
    assert sorted(readings) == [
        "reading:S7-20260105-234000",
        "reading:S7-20260105-235959",
        "reading:S7-20260106-000000",
        "reading:S7-20260106-000001",
        "reading:S7-20260106-003000",
    ]
    assert [readings[uid]["sequence"] for uid in sorted(readings)] == [23, 23, 0, 0, 0]
    assert readings["reading:S7-20260105-234000"]["observed_at"] == "2026-01-05T23:40:00+00:00"
    assert readings["reading:S7-20260105-234000"]["sensor"] == "sensor:S7"
    (sensor,) = utc["held"]["Sensor"]
    assert sensor["installed_on"] == "2026-01-06"
    # Midnight itself closes the interval that ends there; a second later opens the next.
    assert [version[1] for version in utc["versions"]] == [
        "2026-01-06T00:00:00+00:00",
        "2026-01-06T01:00:00+00:00",
    ]
    assert utc["run"] == "twin_sync_run:20260106T000000Z-" + utc["run"].rsplit("-", 1)[1]
    # A time written without a zone is read as UTC wherever the twin runs.
    (tank,) = utc["held"]["Tank"]
    assert tank["level"] == 2.0
    datasets = {row["uid"]: row for row in utc["held"]["TwinDataset"]}
    assert datasets["twin_dataset:scada/levels"]["cursor"] == "2026-01-06T00:10:00Z"
    assert datasets["twin_dataset:scada/by_day"]["cursor"] == "2026-01-06T00:30:00Z"
