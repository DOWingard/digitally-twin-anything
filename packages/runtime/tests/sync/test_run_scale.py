"""How long a run takes: one interval, a day to catch up on, and nothing new."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dta_runtime.clock import FixedClock
from dta_runtime.twin import Twin
from live_testkit import LIVE, META_TYPES, TANKS, Folder, at, levels_source, registry_source
from sync_testkit import objects

pytestmark = pytest.mark.scale

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]

# Limits, fixed before anything was measured.
INTERVAL_LIMIT_S = 2.0
CATCH_UP_LIMIT_S = 20.0
IDLE_LIMIT_S = 1.0

SERIES = 36
# Readings of one series in one interval.
PER_INTERVAL = 3
DAY = 96


def level(tank: int, interval: int) -> float:
    """A level within a tank's bounds that differs from one interval to the next."""
    return 1.0 + ((interval * 7 + tank) % 40) * 0.1


def readings(first: int, last: int) -> list[tuple[Any, ...]]:
    """Readings of every tank in the intervals `first`..`last`, counted from one."""
    rows = []
    for interval in range(first, last + 1):
        for step in range(PER_INTERVAL):
            minutes = 15 * (interval - 1) + 5 * (step + 1)
            for tank in range(SERIES):
                value = level(tank, interval) + 0.01 * (PER_INTERVAL - 1 - step)
                rows.append((f"T{tank:02d}", at(minutes), round(value, 3)))
    return rows


def plant(tmp_path: Path) -> tuple[Folder, dict[str, Any]]:
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, readings(1, 1)),
    }
    registry.write(
        "tanks", TANKS, [(f"T{tank:02d}", f"Tank {tank:02d}", 40.0, 0.5, 6.0) for tank in range(36)]
    )
    return scada, sources


def timed(tick: Tick, twin: Twin, clock: FixedClock) -> tuple[Any, float]:
    started = time.perf_counter()
    report = tick(twin, clock)
    return report, time.perf_counter() - started


def test_one_interval_of_36_series_and_a_run_with_nothing_new(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    scada, sources = plant(tmp_path)
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))
    assert tick(twin, clock).status == "succeeded"

    scada.append("levels", readings(2, 2))
    clock.set(at(31))
    report, elapsed = timed(tick, twin, clock)

    print(f"\none interval of {SERIES} series: {elapsed:.3f} s (limit {INTERVAL_LIMIT_S} s)")
    assert (report.status, report.versions_written, report.rows_written) == (
        "succeeded",
        1,
        SERIES,
    )
    assert report.sources["scada"].rows_read == SERIES * PER_INTERVAL
    assert objects(store, "Tank")["node:T07"]["level"] == pytest.approx(level(7, 2))
    assert elapsed <= INTERVAL_LIMIT_S

    before = store.head()
    report, elapsed = timed(tick, twin, clock)

    print(f"nothing new: {elapsed:.3f} s (limit {IDLE_LIMIT_S} s)")
    assert (report.versions_written, report.rows_written) == (1, 0)
    assert set(store.diff(before, "head")) <= META_TYPES
    assert elapsed <= IDLE_LIMIT_S


def test_a_day_of_intervals_is_caught_up_in_one_run(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    scada, sources = plant(tmp_path)
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))
    assert tick(twin, clock).status == "succeeded"

    scada.append("levels", readings(2, DAY + 1))
    clock.set(at(15 * (DAY + 1) + 1))
    report, elapsed = timed(tick, twin, clock)

    print(f"\ncatch-up of {DAY} intervals: {elapsed:.3f} s (limit {CATCH_UP_LIMIT_S} s)")
    assert (report.status, report.versions_written, report.rows_written) == (
        "succeeded",
        DAY,
        DAY * SERIES,
    )
    versions = store.versions(run=report.run)
    assert [version.t for version in versions] == [at(15 * k) for k in range(2, DAY + 2)]
    assert objects(store, "Tank")["node:T07"]["level"] == pytest.approx(level(7, DAY + 1))
    assert elapsed <= CATCH_UP_LIMIT_S

    # With a day behind it, a run that finds nothing new is still short.
    report, elapsed = timed(tick, twin, clock)

    print(f"nothing new after a day: {elapsed:.3f} s (limit {IDLE_LIMIT_S} s)")
    assert (report.versions_written, report.rows_written) == (1, 0)
    assert elapsed <= IDLE_LIMIT_S
