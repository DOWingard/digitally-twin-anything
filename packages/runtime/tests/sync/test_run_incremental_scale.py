"""A dataset beneath a view with a day of history behind it: what one more interval
asks of the source and how long the run takes, read on and read whole; and what a
run spends on its plan once the process has made one."""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import parse_manifest
from dta_runtime.clock import FixedClock
from dta_runtime.sync.plan import build_plan
from dta_runtime.twin import Bundle, Twin
from live_testkit import (
    TANK_LEVEL_MAPPING,
    TANKS,
    Connecting,
    Folder,
    asked_of,
    at,
    register_signals,
    registry_source,
    telemetry_source,
)
from sync_testkit import objects

pytestmark = pytest.mark.scale

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]

SERIES = 36
# Readings of one series in one interval.
PER_INTERVAL = 3
DAY = 96

# Limits, fixed before anything was measured.
# One more interval is asked of the source as its own rows and no others.
ROWS_LIMIT = SERIES * PER_INTERVAL
INTERVAL_LIMIT_S = 1.0
# What a run may spend on its plan once the process has made one before.
PLAN_LIMIT_S = 0.050

SYNC = {"interval": 900, "aggregation": "mean"}
REFERENCE = Path(__file__).resolve().parents[4] / "testbed"


def level(tank: int, interval: int) -> float:
    """A level within a tank's bounds that differs from one interval to the next."""
    return 1.0 + ((interval * 7 + tank) % 40) * 0.1


def readings(first: int, last: int) -> list[tuple[Any, ...]]:
    """Readings of every level instrument in the intervals `first`..`last`, counted from
    one; the last of each interval is made exactly at its end."""
    rows = []
    for interval in range(first, last + 1):
        for step in range(PER_INTERVAL):
            minutes = 15 * (interval - 1) + 5 * (step + 1)
            for tank in range(SERIES):
                value = level(tank, interval) + 0.01 * (step - 1)
                rows.append((f"L{tank:02d}", at(minutes), round(value, 3)))
    return rows


def plant(folder: Path, declared: bool) -> tuple[Folder, dict[str, Any]]:
    """Tanks whose levels are told from the telemetry by a register of its instruments."""
    registry, telemetry = Folder(folder / "registry"), Folder(folder / "telemetry")
    register = [(f"L{tank:02d}", f"T{tank:02d}", "level") for tank in range(SERIES)]
    sources = {
        "registry": register_signals(registry_source(registry), registry, register),
        "telemetry": telemetry_source(
            telemetry,
            readings(1, DAY),
            declared=declared,
            mappings={"levels": TANK_LEVEL_MAPPING},
        ),
    }
    registry.write(
        "tanks",
        TANKS,
        [(f"T{tank:02d}", f"Tank {tank:02d}", 40.0, 0.5, 6.0) for tank in range(SERIES)],
    )
    return telemetry, sources


def planning_ms(caplog: pytest.LogCaptureFixture) -> float:
    (found,) = [
        record.data["elapsed_ms"]
        for record in caplog.records
        if record.getMessage() == "plan made" and record.context == "sync"
    ]
    return found


def test_one_more_interval_after_a_day_is_asked_for_as_its_own_rows(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO, logger="dta")
    measured: dict[bool, tuple[int, float, float]] = {}
    asked: dict[bool, Any] = {}
    for declared in (True, False):
        telemetry, sources = plant(tmp_path / ("on" if declared else "whole"), declared)
        twin = sync_twin(sources, sync=SYNC)
        connect = Connecting()
        clock = FixedClock(at(15 * DAY + 1))
        history = tick(twin, clock, connect=connect)
        assert (history.status, history.versions_written) == ("succeeded", DAY)

        telemetry.append("readings", readings(DAY + 1, DAY + 1))
        clock.set(at(15 * (DAY + 1) + 1))
        caplog.clear()
        started = time.perf_counter()
        report = tick(twin, clock, connect=connect)
        elapsed = time.perf_counter() - started

        assert (report.status, report.versions_written, report.rows_written) == (
            "succeeded",
            1,
            SERIES,
        )
        assert objects(twin.store, "Tank")["node:T07"]["level"] == pytest.approx(level(7, DAY + 1))
        received = report.sources["telemetry"].datasets["readings"].rows_read
        measured[declared] = (received, elapsed, planning_ms(caplog))
        asked[declared] = asked_of(connect, "readings")[-1]

    for declared, (received, elapsed, planned) in measured.items():
        print(
            f"\none more interval of {SERIES} series after {DAY}, "
            f"{'read on' if declared else 'read whole'}: {received} rows received "
            f"(limit {ROWS_LIMIT if declared else 'none'}), {elapsed:.3f} s "
            f"(limit {INTERVAL_LIMIT_S if declared else 'none'} s), "
            f"plan {planned} ms (limit {PLAN_LIMIT_S * 1000:.0f} ms)"
        )
    assert asked == {True: at(15 * DAY), False: None}
    assert measured[True][0] <= ROWS_LIMIT
    assert measured[True][1] <= INTERVAL_LIMIT_S
    # Read whole, the same interval costs the source everything it holds.
    assert measured[False][0] == (DAY + 1) * SERIES * PER_INTERVAL
    for _, _, planned in measured.values():
        assert planned <= PLAN_LIMIT_S * 1000


def test_planning_again_in_one_process_is_short(make_twin: Callable[..., Twin]):
    manifest_file = REFERENCE / "twin-live" / "manifest.yaml"
    ontology_file = REFERENCE / "twin" / "ontology" / "water_utility.yaml"
    if not (manifest_file.is_file() and ontology_file.is_file()):
        pytest.skip("the reference twin is not in this tree")
    document = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    document["sources"]["telemetry"]["incremental"] = {"readings": "timestamp"}
    manifest = parse_manifest(document, origin="the reference twin")
    twin = make_twin(
        Bundle(manifest=manifest, ontology_text=ontology_file.read_text(encoding="utf-8"))
    )

    # What a run does for its plan: the manifest in force, read from the store, and
    # the plan made from it. Nothing of it is kept from one time to the next but the
    # parsed manifest, which the twin keeps by its hash.
    spent = []
    for _ in range(8):
        started = time.perf_counter()
        plan = build_plan(twin.manifest(), twin.ontology, recurring=True)
        spent.append(time.perf_counter() - started)

    later = spent[1:]
    print(
        f"\nplan of the reference twin ({len(plan.mappings)} mappings, {len(plan.views)} views, "
        f"{len(plan.datasets)} datasets): first {spent[0] * 1000:.1f} ms, then "
        f"{min(later) * 1000:.1f}..{max(later) * 1000:.1f} ms, median "
        f"{statistics.median(later) * 1000:.1f} ms (limit {PLAN_LIMIT_S * 1000:.0f} ms)"
    )
    assert statistics.median(later) <= PLAN_LIMIT_S
