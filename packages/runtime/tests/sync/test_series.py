"""Time series: history replayed as one version per interval, in data time."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from dta_core.store import EPOCH
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
from water_scenario import (
    JUNCTIONS,
    junction_mapping,
    network_sources,
    network_tables,
    readings,
    registry_tables,
)

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]
Reading = tuple[str, datetime, float | None]

QUARTER = timedelta(minutes=15)
BACKFILL = {"backfill_interval": 900}
SECOND = timedelta(seconds=1)

READINGS: list[Reading] = [
    # Exactly on a multiple of the interval: the last instant of the interval ending there.
    ("T1.level", T0 + 15 * MINUTE, 1.0),
    # Just after it: the first instant of the next.
    ("T1.level", T0 + 15 * MINUTE + SECOND, 1.1),
    ("T1.level", T0 + 20 * MINUTE, 1.2),
    ("T1.level", T0 + 30 * MINUTE, 1.3),
    # Unchanged over a whole interval.
    ("T1.level", T0 + 31 * MINUTE, 1.3),
    ("T1.level", T0 + 45 * MINUTE, 1.3),
    ("T1.level", T0 + 50 * MINUTE, 2.0),
    ("J1.demand", T0 + 15 * MINUTE, 5.0),
    ("J1.demand", T0 + 40 * MINUTE, 6.0),
    ("J2.demand", T0 + 15 * MINUTE, 7.0),
    ("J2.demand", T0 + 44 * MINUTE, 7.0),
]
OBJECT_OF = {
    "T1.level": ("node:T1", "level"),
    "J1.demand": ("node:J1", "demand"),
    "J2.demand": ("node:J2", "demand"),
}
REDUCE: dict[str, Callable[[list[tuple[datetime, float]]], float]] = {
    "last": lambda found: max(found)[1],
    "mean": lambda found: sum(value for _, value in found) / len(found),
    "min": lambda found: min(value for _, value in found),
    "max": lambda found: max(value for _, value in found),
}


def interval_end(moment: datetime, width: timedelta = QUARTER) -> datetime:
    """End of the interval `(end - width, end]` that holds a time; worked out independently."""
    whole, rest = divmod(moment - EPOCH, width)
    return EPOCH + (whole + (1 if rest else 0)) * width


def expected_changes(
    rows: list[Reading], aggregation: str = "last"
) -> dict[str, list[tuple[datetime, float]]]:
    """Per signal, the (version time, value) changes a backfill of `rows` must produce."""
    grouped: dict[str, dict[datetime, list[tuple[datetime, float]]]] = {}
    for signal, moment, value in rows:
        if value is not None:
            by_end = grouped.setdefault(signal, {})
            by_end.setdefault(interval_end(moment), []).append((moment, value))
    result: dict[str, list[tuple[datetime, float]]] = {}
    for signal, by_end in grouped.items():
        changes: list[tuple[datetime, float]] = []
        for end in sorted(by_end):
            value = REDUCE[aggregation](by_end[end])
            if not changes or changes[-1][1] != value:
                changes.append((end, value))
        result[signal] = changes
    return result


def loaded(sync_twin: MakeTwin, run: Run, rows: list[Reading], **sync: Any) -> tuple[Twin, Any]:
    twin = sync_twin(network_sources(), sync={**BACKFILL, **sync})
    report = run(twin, Sources(**network_tables(rows)))
    return twin, report


def test_history_is_replayed_as_one_version_per_interval(sync_twin: MakeTwin, run: Run):
    twin, report = loaded(sync_twin, run, READINGS)
    store = twin.store

    assert (report.status, report.kind) == ("succeeded", "backfill")
    versions = sync_versions(store)
    # One version per interval that has something to write, at the interval's end.
    assert [version.t for version in versions] == [
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
        T0 + 45 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    assert all(version.extra == {"backfill": True} for version in versions)
    assert all((v.cause, v.actor, v.run) == ("sync", "sync", report.run) for v in versions)
    assert report.versions_written == 4
    assert (report.first_seq, report.last_seq) == (versions[0].seq, versions[-1].seq)
    assert report.watermark == T0 + 50 * MINUTE

    # What changed, and when, is exactly what the input says.
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(store, uid, prop) == wanted[signal], signal
    assert wanted["T1.level"] == [
        (T0 + 15 * MINUTE, 1.0),
        (T0 + 30 * MINUTE, 1.3),
        (T0 + 60 * MINUTE, 2.0),
    ]
    assert wanted["J2.demand"] == [(T0 + 15 * MINUTE, 7.0)]

    # Static objects go into the run's first version, at its time.
    first = versions[0]
    assert set(objects(store, "Pipe", at=first)) == {"link:P1", "link:P2", "link:P3"}
    assert objects(store, "Tank", at=first)["node:T1"]["level"] == 1.0
    assert store.get("link:P1").provenance_of("length").t_valid == T0 + 15 * MINUTE

    # The state as of an old version is the state at that time.
    assert objects(store, "Tank", at=versions[1])["node:T1"]["level"] == 1.3
    assert objects(store, "Junction", at=versions[1])["node:J1"]["demand"] == 5.0
    assert objects(store, "Junction", at="head")["node:J1"]["demand"] == 6.0

    counts = report.mappings
    assert (counts["scada.levels"].rows_read, counts["scada.levels"].rows_written) == (7, 3)
    assert (counts["scada.demands"].rows_read, counts["scada.demands"].rows_written) == (4, 3)
    assert report.rows_rejected == 0


def test_a_value_keeps_the_valid_time_of_its_row(sync_twin: MakeTwin, run: Run):
    twin, _ = loaded(sync_twin, run, READINGS)
    store = twin.store
    versions = sync_versions(store)

    at_second = store.get("node:T1", at=versions[1])
    level = at_second.provenance_of("level")
    assert (level.source, level.kind, level.t_valid) == ("scada", "observed", T0 + 30 * MINUTE)
    # The rest of the object still says where it came from.
    name = at_second.provenance_of("name")
    assert (name.source, name.t_valid) == ("registry", T0 + 15 * MINUTE)
    # At the head the value of 00:50 is in force, written by the version of 01:00.
    head_level = store.get("node:T1").provenance_of("level")
    assert head_level.t_valid == T0 + 50 * MINUTE
    assert store.get("node:J1").provenance_of("demand").t_valid == T0 + 40 * MINUTE


@pytest.mark.parametrize("aggregation", ["mean", "min", "max"])
def test_an_interval_is_reduced_by_the_aggregation(sync_twin: MakeTwin, run: Run, aggregation: str):
    twin, report = loaded(sync_twin, run, READINGS, aggregation=aggregation)
    store = twin.store

    wanted = expected_changes(READINGS, aggregation)
    for signal, (uid, prop) in OBJECT_OF.items():
        found = changes_of(store, uid, prop)
        assert [t for t, _ in found] == [t for t, _ in wanted[signal]], signal
        assert [v for _, v in found] == pytest.approx([v for _, v in wanted[signal]]), signal
    # Three readings fall into the interval ending 00:30.
    second = {"mean": 1.2, "min": 1.1, "max": 1.3}[aggregation]
    assert dict(changes_of(store, "node:T1", "level"))[T0 + 30 * MINUTE] == pytest.approx(second)
    # A reduced value was true over the interval: its valid time is the interval's end.
    version = next(v for v in sync_versions(store) if v.t == T0 + 30 * MINUTE)
    assert store.get("node:T1", at=version).provenance_of("level").t_valid == T0 + 30 * MINUTE
    assert report.rows_rejected == 0


def test_version_times_never_decrease_and_follow_the_data(sync_twin: MakeTwin, run: Run):
    rows = [
        ("T1.level", T0 + QUARTER * step + offset, 1.0 + 0.01 * step)
        for step in range(1, 40)
        for offset in (timedelta(0), 7 * MINUTE)
    ]
    twin, report = loaded(sync_twin, run, rows)

    times = [version.t for version in sync_versions(twin.store)]
    assert times == sorted(times)
    assert times == sorted({interval_end(moment) for _, moment, _ in rows})
    assert report.versions_written == len(times) == 40
    assert changes_of(twin.store, "node:T1", "level") == expected_changes(rows)["T1.level"]


def test_two_rows_of_one_time_do_not_depend_on_their_order(sync_twin: MakeTwin, run: Run):
    moment = T0 + 20 * MINUTE
    one = [("T1.level", moment, 1.5), ("T1.level", moment, 2.5)]
    first, _ = loaded(sync_twin, run, one)
    second, _ = loaded(sync_twin, run, list(reversed(one)))

    assert objects(first.store, "Tank")["node:T1"]["level"] == 2.5
    assert objects(second.store, "Tank")["node:T1"]["level"] == 2.5


def test_without_an_interval_everything_is_one_version_at_the_latest_time(
    sync_twin: MakeTwin, run: Run
):
    twin = sync_twin(network_sources())
    report = run(twin, Sources(**network_tables(READINGS)))
    store = twin.store

    versions = sync_versions(store)
    assert [version.t for version in versions] == [T0 + 50 * MINUTE]
    assert versions[0].extra == {}
    assert (report.kind, report.versions_written) == ("incremental", 1)
    assert objects(store, "Tank")["node:T1"]["level"] == 2.0
    assert objects(store, "Junction")["node:J1"]["demand"] == 6.0
    assert store.get("node:T1").provenance_of("level").t_valid == T0 + 50 * MINUTE
    assert store.get("node:J1").provenance_of("demand").t_valid == T0 + 40 * MINUTE
    assert store.get("node:J1").provenance_of("name").t_valid == T0 + 50 * MINUTE


def lab_source() -> dict[str, Any]:
    """A second feed that writes another property of the junctions, as estimates."""
    return source(
        {
            "pressures": {
                "dataset": "pressures",
                "object_type": "Junction",
                "identity": ["node"],
                "valid_time": "at",
                "kind": "inferred",
                "properties": columns(pressure="head_m"),
            }
        }
    )


def pressures(rows: list[tuple[str, datetime, float]]) -> pa.Table:
    return pa.table(
        {
            "node": pa.array([row[0] for row in rows]),
            "at": pa.array([row[1] for row in rows], TIME),
            "head_m": pa.array([row[2] for row in rows], pa.float64()),
        }
    )


def test_two_sources_feeding_one_object_in_one_interval_keep_their_own_provenance(
    sync_twin: MakeTwin, run: Run
):
    twin = sync_twin({**network_sources(), "lab": lab_source()}, sync=BACKFILL)
    tables = network_tables([("J1.demand", T0 + 20 * MINUTE, 5.0)])
    tables["lab"] = {"pressures": pressures([("J1", T0 + 25 * MINUTE, 31.5)])}
    report = run(twin, Sources(**tables))

    assert report.versions_written == 1
    junction = twin.store.get("node:J1")
    assert (junction.values["demand"], junction.values["pressure"]) == (5.0, 31.5)
    demand, pressure = junction.provenance_of("demand"), junction.provenance_of("pressure")
    assert (demand.source, demand.kind, demand.t_valid) == (
        "scada",
        "observed",
        T0 + 20 * MINUTE,
    )
    assert (pressure.source, pressure.kind, pressure.t_valid) == (
        "lab",
        "inferred",
        T0 + 25 * MINUTE,
    )
    # One row was written for the object, with the registry's values in it.
    whole = junction.provenance_of("elevation")
    assert (whole.source, whole.t_valid) == ("registry", T0 + 30 * MINUTE)
    assert set(junction.provenance) == {"*", "demand", "pressure"}
    # Three mappings wrote that one row; the run wrote one row per object.
    written = {label: count.rows_written for label, count in report.mappings.items()}
    assert (written["registry.junctions"], written["scada.demands"], written["lab.pressures"]) == (
        3,
        1,
        1,
    )
    assert report.rows_written == 9


def test_a_second_run_writes_only_what_the_source_gained(sync_twin: MakeTwin, run: Run):
    early = [row for row in READINGS if row[1] <= T0 + 30 * MINUTE]
    twin, first = loaded(sync_twin, run, early)
    store = twin.store
    head = store.head()
    assert head.t == T0 + 30 * MINUTE

    # Nothing new: nothing written.
    again = run(twin, Sources(**network_tables(early)))
    assert again.versions_written == 0
    assert store.head() == head

    second = run(twin, Sources(**network_tables(READINGS)))

    assert second.versions_written == 2
    assert [v.t for v in store.versions(run=second.run)] == [T0 + 45 * MINUTE, T0 + 60 * MINUTE]
    # The history is the same as if everything had been loaded at once.
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(store, uid, prop) == wanted[signal], signal
    assert second.mappings["scada.levels"].rows_read == 3
    assert second.mappings["registry.pipes"].rows_written == 0
    assert second.watermark == T0 + 50 * MINUTE
    dataset = objects(store, "TwinDataset")["twin_dataset:scada/by_node"]
    assert (dataset["cursor_column"], dataset["cursor"]) == ("ts", "2026-01-05T00:50:00Z")
    assert first.run != second.run


def direct_sources() -> dict[str, Any]:
    """Telemetry already in one row per tank and time: mapped without a view."""
    return {
        "registry": source({"tanks": registry_tank_mapping()}),
        "levels": source(
            {
                "levels": {
                    "dataset": "tank_levels",
                    "object_type": "Tank",
                    "identity": ["tank"],
                    "valid_time": "ts",
                    "cursor": "ts",
                    "properties": columns("level"),
                }
            }
        ),
    }


def registry_tank_mapping() -> dict[str, Any]:
    return {
        "dataset": "tanks",
        "object_type": "Tank",
        "identity": ["id"],
        "properties": columns("name", "elevation", "min_level", "max_level"),
    }


def tank_levels(rows: list[tuple[datetime, float | None]]) -> pa.Table:
    return pa.table(
        {
            "tank": pa.array(["T1"] * len(rows)),
            "ts": pa.array([row[0] for row in rows], TIME),
            "level": pa.array([row[1] for row in rows], pa.float64()),
        }
    )


def test_a_dataset_read_as_a_time_series_is_asked_only_for_newer_rows(
    sync_twin: MakeTwin, run: Run
):
    twin = sync_twin(direct_sources(), sync=BACKFILL)
    rows = [(T0 + 10 * MINUTE, 1.0), (T0 + 25 * MINUTE, 1.5)]
    sources = Sources(
        registry={"tanks": registry_tables()["tanks"]}, levels={"tank_levels": tank_levels(rows)}
    )
    run(twin, sources)
    assert sources["levels"].reads[-1]["since"] is None

    rows.append((T0 + 40 * MINUTE, 2.5))
    sources.put("levels", "tank_levels", tank_levels(rows))
    report = run(twin, sources)

    asked = sources["levels"].reads[-1]
    assert (asked["cursor_column"], asked["since"]) == ("ts", T0 + 25 * MINUTE)
    assert report.sources["levels"].datasets == {"tank_levels": 1}
    assert report.rows_written == 1
    assert changes_of(twin.store, "node:T1", "level") == [
        (T0 + 15 * MINUTE, 1.0),
        (T0 + 30 * MINUTE, 1.5),
        (T0 + 45 * MINUTE, 2.5),
    ]
    # The registry has no time: it is read whole every time.
    assert all(read["since"] is None for read in sources["registry"].reads)
    dataset = objects(twin.store, "TwinDataset")["twin_dataset:levels/tank_levels"]
    assert dataset["cursor"] == "2026-01-05T00:40:00Z"
    assert dataset["rows_read"] == 3


def test_an_unchanged_reading_writes_no_data_but_moves_the_cursor(sync_twin: MakeTwin, run: Run):
    twin = sync_twin(direct_sources(), sync=BACKFILL)
    rows = [(T0 + 10 * MINUTE, 1.0)]
    sources = Sources(
        registry={"tanks": registry_tables()["tanks"]}, levels={"tank_levels": tank_levels(rows)}
    )
    run(twin, sources)
    before = twin.store.head()

    rows.append((T0 + 70 * MINUTE, 1.0))
    sources.put("levels", "tank_levels", tank_levels(rows))
    report = run(twin, sources)

    # One version, holding only the pipeline's own records.
    assert (report.versions_written, report.rows_written) == (1, 0)
    difference = twin.store.diff(before, "head")
    assert set(difference) == {"TwinSyncRun", "TwinDataset", "TwinSource"}
    assert twin.store.head().t == T0 + 75 * MINUTE
    assert changes_of(twin.store, "node:T1", "level") == [(T0 + 15 * MINUTE, 1.0)]
    dataset = objects(twin.store, "TwinDataset")["twin_dataset:levels/tank_levels"]
    assert dataset["cursor"] == "2026-01-05T01:10:00Z"
    feed = objects(twin.store, "TwinSource")["twin_source:levels"]
    assert (feed["watermark"], feed["status"]) == (T0 + 70 * MINUTE, "ok")
    assert feed["last_success_at"] == T0 + 75 * MINUTE
    assert report.watermark == T0 + 70 * MINUTE

    # And the run after that has nothing to do at all.
    head = twin.store.head()
    assert run(twin, sources).versions_written == 0
    assert twin.store.head() == head


def test_a_changed_static_row_is_written_at_the_time_the_twin_has_reached(
    sync_twin: MakeTwin, run: Run
):
    twin, _ = loaded(sync_twin, run, READINGS)
    head = twin.store.head()
    assert head.t == T0 + 60 * MINUTE

    tables = network_tables(READINGS)
    changed = [dict(row) for row in JUNCTIONS]
    changed[2]["elevation"] = 9.0
    tables["registry"]["junctions"] = table(changed)
    report = run(twin, Sources(**tables))

    assert (report.versions_written, report.rows_written) == (1, 1)
    version = twin.store.head()
    assert (version.seq, version.t) == (head.seq + 1, head.t)
    junction = twin.store.get("node:J3")
    assert junction.values["elevation"] == 9.0
    assert junction.provenance_of("elevation").t_valid == head.t
    assert objects(twin.store, "Junction", at=head)["node:J3"]["elevation"] == 8.25


def test_data_older_than_the_head_is_recorded_at_the_head_with_its_own_valid_time(
    sync_twin: MakeTwin, run: Run
):
    twin = sync_twin({**network_sources(), "lab": lab_source()}, sync=BACKFILL)
    tables = network_tables(READINGS)
    tables["lab"] = {"pressures": pressures([])}
    sources = Sources(**tables)
    run(twin, sources)
    head = twin.store.head()

    # The lab reports late: its estimates are of a time the twin has long passed.
    sources.put(
        "lab",
        "pressures",
        pressures([("J1", T0 + 10 * MINUTE, 30.0), ("J1", T0 + 22 * MINUTE, 31.0)]),
    )
    report = run(twin, sources)

    versions = twin.store.versions(run=report.run)
    assert [version.t for version in versions] == [head.t, head.t]
    assert changes_of(twin.store, "node:J1", "pressure") == [
        (T0 + 15 * MINUTE, None),  # the junction as the registry first described it
        (head.t, 30.0),
        (head.t, 31.0),
    ]
    late = twin.store.get("node:J1").provenance_of("pressure")
    assert (late.source, late.t_valid) == ("lab", T0 + 22 * MINUTE)


def signals_with_pressure() -> list[dict[str, str]]:
    return [
        {"signal": "J1.demand", "node": "J1", "quantity": "demand"},
        {"signal": "J1.pressure", "node": "J1", "quantity": "pressure"},
        {"signal": "J2.demand", "node": "J2", "quantity": "demand"},
        {"signal": "J2.chlorine", "node": "J2", "quantity": "chlorine"},
    ]


def pivot_sources() -> dict[str, Any]:
    pivot = (
        "SELECT s.node, r.ts, "
        "max(r.value) FILTER (WHERE s.quantity = 'demand') AS demand, "
        "max(r.value) FILTER (WHERE s.quantity = 'pressure') AS pressure "
        'FROM "scada"."readings" r JOIN "registry"."signals" s ON s.signal = r.signal '
        "GROUP BY s.node, r.ts"
    )
    return {
        "registry": source({"junctions": junction_mapping()}),
        "scada": source(
            {
                "junctions": {
                    "dataset": "by_node",
                    "object_type": "Junction",
                    "identity": ["node"],
                    "valid_time": "ts",
                    "properties": columns("demand", "pressure"),
                }
            },
            views={"by_node": pivot},
        ),
    }


def test_a_missing_reading_is_not_a_value(sync_twin: MakeTwin, run: Run):
    rows: list[Reading] = [
        ("J1.demand", T0 + 10 * MINUTE, 5.0),
        ("J1.pressure", T0 + 10 * MINUTE, 30.0),
        # In the next interval only the pressure reports: the pivot gives a null demand.
        ("J1.pressure", T0 + 20 * MINUTE, 32.0),
        ("J1.pressure", T0 + 25 * MINUTE, 34.0),
        ("J1.demand", T0 + 40 * MINUTE, 5.0),
        # A signal nothing maps: its rows have nothing but nulls.
        ("J2.chlorine", T0 + 55 * MINUTE, 0.4),
        ("J2.demand", T0 + 10 * MINUTE, 7.0),
    ]
    twin = sync_twin(pivot_sources(), sync={**BACKFILL, "aggregation": "mean"})
    tables = {
        "registry": {"junctions": table(JUNCTIONS), "signals": table(signals_with_pressure())},
        "scada": {"readings": readings(rows)},
    }
    report = run(twin, Sources(**tables))
    store = twin.store

    assert report.rows_rejected == 0
    # The demand was never overwritten with null, nor written again unchanged.
    assert changes_of(store, "node:J1", "demand") == [(T0 + 15 * MINUTE, 5.0)]
    # A null takes no part in the reduction of its interval.
    assert changes_of(store, "node:J1", "pressure") == [
        (T0 + 15 * MINUTE, 30.0),
        (T0 + 30 * MINUTE, 33.0),
    ]
    # The value that was not reported keeps the provenance it had.
    junction = store.get("node:J1")
    assert junction.provenance_of("demand").t_valid == T0 + 15 * MINUTE
    assert junction.provenance_of("pressure").t_valid == T0 + 30 * MINUTE
    # Rows with nothing but nulls write nothing: two versions hold data, a third
    # only records how far the feed was read.
    versions = sync_versions(store)
    assert [version.t for version in versions] == [
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    assert set(store.diff(versions[1], versions[2])) == {"TwinSyncRun", "TwinDataset", "TwinSource"}
    assert objects(store, "Junction")["node:J2"]["demand"] == 7.0
    assert report.watermark == T0 + 55 * MINUTE
