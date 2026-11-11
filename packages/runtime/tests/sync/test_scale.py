"""Full-size loads: a fortnight of telemetry replayed, and a million rows mapped."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from dta_core.log import trace
from dta_runtime.sync.pipeline import prepare_load
from dta_runtime.twin import Twin
from sync_testkit import T0, TIME, TRACE, Sources, columns, objects, source, sync_versions

pytestmark = pytest.mark.scale

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]

# Limits, fixed before anything was measured. The machine is shared, so they
# are far above what an idle one needs.
BACKFILL_LIMIT_S = 600.0
VERSION_LIMIT_S = 0.250
MAPPING_LIMIT_S = 30.0

QUARTER = timedelta(minutes=15)
JUNCTIONS, TANKS, PIPES = 200, 20, 80
PIVOT = (
    "SELECT s.node, r.ts, "
    "max(r.value) FILTER (WHERE s.quantity = 'level') AS level, "
    "max(r.value) FILTER (WHERE s.quantity = 'demand') AS demand "
    'FROM "scada"."readings" r JOIN "registry"."signals" s ON s.signal = r.signal '
    "GROUP BY s.node, r.ts"
)


def network() -> dict[str, Any]:
    registry = source(
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
                "properties": columns("name", "elevation", "min_level", "max_level"),
            },
            "pipes": {
                "dataset": "pipes",
                "object_type": "Pipe",
                "identity": ["id"],
                "properties": columns(
                    "status", start_node="from_node", end_node="to_node", length="length_m"
                )
                | {"diameter": {"column": "diameter_m", "unit": "m"}},
            },
        }
    )
    scada = source(
        {
            "levels": {
                "dataset": "by_node",
                "object_type": "Tank",
                "identity": ["node"],
                "valid_time": "ts",
                "filter": "level IS NOT NULL",
                "properties": columns("level"),
            },
            "demands": {
                "dataset": "by_node",
                "object_type": "Junction",
                "identity": ["node"],
                "valid_time": "ts",
                "filter": "demand IS NOT NULL",
                "properties": columns("demand"),
            },
        },
        views={"by_node": PIVOT},
    )
    return {"registry": registry, "scada": scada}


def registry_tables(tank_signals: int, junction_signals: int) -> dict[str, pa.Table]:
    junctions = [f"J{n:03d}" for n in range(1, JUNCTIONS + 1)]
    tanks = [f"T{n:02d}" for n in range(1, TANKS + 1)]
    signals = [(f"{node}.level", node, "level") for node in tanks[:tank_signals]]
    signals += [(f"{node}.demand", node, "demand") for node in junctions[:junction_signals]]
    return {
        "junctions": pa.table(
            {
                "id": junctions,
                "name": [f"Junction {n}" for n in range(1, JUNCTIONS + 1)],
                "elevation": [10.0 + n * 0.25 for n in range(JUNCTIONS)],
                "x": [float(n % 20) * 100.0 for n in range(JUNCTIONS)],
                "y": [float(n // 20) * 100.0 for n in range(JUNCTIONS)],
                "base_demand": [0.5 + (n % 7) * 0.125 for n in range(JUNCTIONS)],
            }
        ),
        "tanks": pa.table(
            {
                "id": tanks,
                "name": [f"Tank {n}" for n in range(1, TANKS + 1)],
                "elevation": [60.0 + n for n in range(TANKS)],
                "min_level": [0.5] * TANKS,
                "max_level": [6.0] * TANKS,
            }
        ),
        "pipes": pa.table(
            {
                "id": [f"P{n:03d}" for n in range(1, PIPES + 1)],
                "from_node": [junctions[n] for n in range(PIPES)],
                "to_node": [junctions[n + 1] for n in range(PIPES)],
                "length_m": [100.0 + n for n in range(PIPES)],
                "diameter_m": [0.15] * PIPES,
                "status": ["open"] * PIPES,
            }
        ),
        "signals": pa.table(
            {
                "signal": [row[0] for row in signals],
                "node": [row[1] for row in signals],
                "quantity": [row[2] for row in signals],
            }
        ),
    }


def telemetry(
    signals: pa.Table, steps: int, every: timedelta
) -> tuple[pa.Table, dict[str, np.ndarray]]:
    """`steps` readings of every signal, `every` apart; returns them long and per signal."""
    names = signals.column("signal").to_pylist()
    quantities = signals.column("quantity").to_pylist()
    generator = np.random.default_rng(20260105)
    series = {}
    for name, quantity in zip(names, quantities, strict=True):
        if quantity == "level":
            # Always within the tanks' bounds.
            series[name] = 1.0 + 4.0 * generator.random(steps)
        else:
            series[name] = 10.0 * generator.random(steps)
    start = int(T0.timestamp() * 1_000_000)
    step = every // timedelta(microseconds=1)
    moments = start + step * np.arange(1, steps + 1, dtype=np.int64)
    # Signal by signal, as a historian would export it: not sorted by time.
    long = pa.table(
        {
            "signal": pa.array(np.repeat(np.array(names, dtype=object), steps), pa.string()),
            "ts": pa.array(np.tile(moments, len(names)), pa.timestamp("us")).cast(TIME),
            "value": pa.array(np.concatenate([series[name] for name in names]), pa.float64()),
        }
    )
    return long, series


def load_average() -> str:
    return " ".join(f"{value:.2f}" for value in os.getloadavg())


def test_a_fortnight_of_telemetry_is_replayed_in_time(
    sync_twin: MakeTwin, run: Run, capsys: pytest.CaptureFixture[str]
):
    steps = 14 * 96
    registry = registry_tables(tank_signals=12, junction_signals=24)
    readings, series = telemetry(registry["signals"], steps, QUARTER)
    assert (readings.num_rows, len(series)) == (48_384, 36)
    twin = sync_twin(network(), sync={"backfill_interval": 900})
    before = load_average()

    started = time.perf_counter()
    report = run(twin, Sources(registry=registry, scada={"readings": readings}))
    elapsed = time.perf_counter() - started

    per_version = elapsed / report.versions_written
    with capsys.disabled():
        print(
            f"\nscale: {report.rows_read} rows read, {report.versions_written} versions and "
            f"{report.rows_written} rows written in {elapsed:.1f} s "
            f"({per_version * 1000.0:.1f} ms per version); "
            f"load average before {before}, after {load_average()}"
        )

    # What was written is what the input says.
    store = twin.store
    changes = sum(
        1 + int(np.count_nonzero(values[1:] != values[:-1])) for values in series.values()
    )
    assert report.status == "succeeded"
    assert report.versions_written == steps
    # One row per object and version; the first version also holds the other objects.
    assert report.rows_written == changes + (JUNCTIONS + TANKS + PIPES - len(series))
    assert report.rows_rejected == 0
    versions = sync_versions(store)
    assert [version.t for version in versions] == [T0 + QUARTER * k for k in range(1, steps + 1)]
    assert len(objects(store, "Junction")) == JUNCTIONS
    assert len(objects(store, "Pipe")) == PIPES
    for uid, signal, prop in (
        ("node:T07", "T07.level", "level"),
        ("node:J019", "J019.demand", "demand"),
    ):
        history = store.history(uid, properties=[prop])
        assert history.column("_t").to_pylist() == [version.t for version in versions]
        assert history.column(prop).to_pylist() == series[signal].tolist()
    tanks = objects(store, "Tank")
    assert all(
        tanks[f"node:T{n:02d}"]["level"] == series[f"T{n:02d}.level"][-1] for n in range(1, 13)
    )
    assert tanks["node:T20"]["level"] is None
    assert objects(store, "Pipe")["link:P001"]["diameter"] == pytest.approx(150.0)

    assert elapsed <= BACKFILL_LIMIT_S
    assert per_version <= VERSION_LIMIT_S

    # And a second run over the same history has nothing to write.
    head = store.head()
    again = run(twin, Sources(registry=registry, scada={"readings": readings}))
    assert again.versions_written == 0
    assert store.head() == head


def test_a_million_readings_are_mapped_and_compared(
    sync_twin: MakeTwin,
    settings_of: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
):
    steps = 25_000
    registry = registry_tables(tank_signals=16, junction_signals=24)
    readings, series = telemetry(registry["signals"], steps, timedelta(minutes=1))
    assert readings.num_rows == 1_000_000
    twin = sync_twin(network(), sync={"backfill_interval": 900})
    sources = Sources(registry=registry, scada={"readings": readings})
    head = twin.store.head()
    before = load_average()

    started = time.perf_counter()
    with trace(TRACE):
        prepared = prepare_load(twin, settings_of(twin), {}, connect=sources)
    elapsed = time.perf_counter() - started

    try:
        mapped = sum(tally.rows for tally in prepared.tallies.values())
        to_write = prepared.rows_to_write()
        intervals = len(prepared.keys)
        with capsys.disabled():
            print(
                f"\nscale: {readings.num_rows} readings planned, read, mapped and compared in "
                f"{elapsed:.1f} s ({intervals} versions and {to_write} rows to write); "
                f"load average before {before}, after {load_average()}"
            )
        # Every reading was mapped, besides the registry's rows.
        assert mapped == readings.num_rows + JUNCTIONS + TANKS + PIPES
        assert sum(tally.rejected for tally in prepared.tallies.values()) == 0
        # 25,000 minutes are 1,666 whole quarters of an hour and a part of one more.
        assert intervals == 1_667
        # Every signal changes in every interval; the first also holds the other objects.
        assert to_write == intervals * len(series) + (JUNCTIONS + TANKS + PIPES - len(series))
        # The value kept for an interval is the last one read in it.
        levels = prepared.batches["Tank"]
        slot = next(s.index for s in levels.slots if s.field.name == "level")
        offset, length = levels.spans[prepared.keys[-1]]
        last = levels.rows.slice(offset, length).to_pydict()
        assert last["uid"][:2] == ["node:T01", "node:T02"]
        assert last[f"v_{slot}"][0] == series["T01.level"][-1]
        assert last[f"tv_{slot}"][0] == T0 + timedelta(minutes=steps)
    finally:
        prepared.close()

    # Nothing was written.
    assert twin.store.head() == head
    assert elapsed <= MAPPING_LIMIT_S
