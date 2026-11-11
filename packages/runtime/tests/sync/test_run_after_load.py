"""A twin loaded from history goes on with runs, and both write the same history."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from dta_core.store import Store
from dta_runtime.clock import FixedClock
from dta_runtime.twin import Twin
from live_testkit import (
    LEVELS,
    READINGS,
    Connecting,
    Folder,
    at,
    levels_source,
    readings_source,
    registry_source,
    stored,
    viewed_levels_source,
)
from sync_testkit import changes_of, objects, sync_versions

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]

# History and what follows it are cut into the same intervals.
SYNC = {"interval": 900, "backfill_interval": 900, "aggregation": "mean"}
DOMAIN = ("Tank", "Junction", "Pump", "Sensor", "Reading")

# What the sources gain in each quarter of an hour: (levels, readings).
ARRIVING: list[tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]] = [
    (
        [("T1", at(5), 1.0), ("T1", at(10), 2.0), ("T2", at(7), 3.0)],
        [("R1", "S7", at(5), 3.0, 1)],
    ),
    # A reading exactly at the end of an interval belongs to it.
    ([("T1", at(20), 2.5), ("T2", at(30), 3.5)], []),
    # T1 stays at its mean: only T2 changes.
    ([("T1", at(35), 2.5), ("T2", at(40), 4.0)], [("R2", "S7", at(40), 3.5, 2)]),
    ([("T1", at(50), 1.0), ("T1", at(55), 2.0)], [("R3", "S7", at(60), 4.0, 3)]),
]
LATER: list[tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]] = [
    # A quarter of an hour in which nothing arrives.
    ([], []),
    ([("T1", at(80), 3.0), ("T2", at(85), 5.0)], [("R4", "S7", at(88), 4.5, 4)]),
]


def plant(folder: Path) -> tuple[Folder, Folder, dict[str, Any]]:
    scada, lab = Folder(folder / "scada"), Folder(folder / "lab")
    sources = {
        "registry": registry_source(Folder(folder / "registry")),
        "scada": levels_source(scada, []),
        "lab": readings_source(lab, [], cursor="rev"),
    }
    return scada, lab, sources


def history(store: Store) -> list[tuple[datetime, dict[str, Any]]]:
    """What the twin held of the domain at each time a sync changed it."""
    found: list[tuple[datetime, dict[str, Any]]] = []
    before = None
    for version in sync_versions(store):
        held = {name: objects(store, name, at=version) for name in DOMAIN}
        if held != before:
            found.append((version.t, held))
            before = held
    return found


def valid_times(store: Store) -> dict[tuple[str, str], datetime]:
    found = {}
    for uid, prop in (("node:T1", "level"), ("node:T2", "level"), ("node:T1", "name")):
        found[(uid, prop)] = store.get(uid).provenance_of(prop).t_valid
    for uid in objects(store, "Reading"):
        found[(uid, "value")] = store.get(uid).provenance_of("value").t_valid
    return found


def test_a_loaded_history_equals_the_same_data_arriving_run_by_run(
    sync_twin: MakeTwin, run: Callable[..., Any], tick: Tick, tmp_path: Path
):
    # One twin finds all of it there and loads it.
    scada, lab, sources = plant(tmp_path / "loaded")
    for levels, readings in ARRIVING:
        scada.append("levels", levels)
        lab.append("readings", readings)
    loaded = sync_twin(sources, sync=SYNC)
    report = run(loaded, Connecting())
    assert (report.status, report.kind, report.versions_written) == ("succeeded", "backfill", 4)

    # The other is running while it arrives.
    live_scada, live_lab, live_sources = plant(tmp_path / "live")
    live = sync_twin(live_sources, sync=SYNC)
    clock = FixedClock(at(1))
    for k, (levels, readings) in enumerate(ARRIVING, start=1):
        live_scada.append("levels", levels)
        live_lab.append("readings", readings)
        clock.set(at(15 * k + 1))
        assert tick(live, clock).status == "succeeded"

    # Same versions, same values, same times the values were true at.
    written = history(loaded.store)
    assert [t for t, _ in written] == [at(15), at(30), at(45), at(60)]
    assert history(live.store) == written
    assert valid_times(live.store) == valid_times(loaded.store)
    assert changes_of(loaded.store, "node:T1", "level") == [
        (at(15), 1.5),
        (at(30), 2.5),
        (at(60), 1.5),
    ]
    # Only a load of history marks its versions as such.
    assert {str(v.extra) for v in sync_versions(loaded.store)} == {"{'backfill': True}"}
    assert {str(v.extra) for v in sync_versions(live.store)} == {"{}"}

    # The loaded twin then goes on with runs, and still holds what the live one holds.
    for levels, readings in LATER:
        for folder_scada, folder_lab in ((scada, lab), (live_scada, live_lab)):
            folder_scada.append("levels", levels)
            folder_lab.append("readings", readings)
    connect = Connecting()
    following = tick(loaded, FixedClock(at(91)), connect=connect)
    for minutes in (76, 91):
        clock.set(at(minutes))
        tick(live, clock)

    assert (following.status, following.kind) == ("succeeded", "incremental")
    # It read on from where the load had stopped: for the series, from the
    # start of the interval the load's last reading lay in.
    asked = {read["dataset"]: read["since"] for read in connect.asked}
    assert (asked["levels"], asked["readings"]) == (at(45), 3)
    # The rows of that interval were read again, found equal, and counted once.
    assert following.mappings["scada.levels"].rows_read == 4
    assert stored(loaded.store, "TwinDataset", "twin_dataset:scada/levels")["rows_read"] == 11
    (version,) = loaded.store.versions(run=following.run)
    assert (version.t, version.extra) == (at(90), {})
    written = history(loaded.store)
    assert [t for t, _ in written] == [at(15), at(30), at(45), at(60), at(90)]
    assert history(live.store) == written
    assert valid_times(live.store) == valid_times(loaded.store)
    assert stored(loaded.store, "TwinSyncRun", following.run)["kind"] == "incremental"
    assert following.sources["scada"].datasets["levels"].cursor == "2026-01-05T01:30:00Z"


@pytest.mark.parametrize(
    ("aggregation", "at_load", "wanted", "valid"),
    [
        ("last", 2.0, 3.0, at(55)),
        ("mean", 2.0, 2.5, at(60)),
        ("max", 2.0, 3.0, at(60)),
        ("min", 2.0, 2.0, at(60)),
    ],
)
def test_an_interval_a_load_found_open_is_completed_by_the_run_that_follows(
    sync_twin: MakeTwin,
    run: Callable[..., Any],
    tick: Tick,
    tmp_path: Path,
    aggregation: str,
    at_load: float,
    wanted: float,
    valid: datetime,
):
    scada, _, sources = plant(tmp_path)
    scada.write("levels", LEVELS, [("T1", at(5), 1.0), ("T1", at(50), 2.0)])
    sync = {"interval": 900, "backfill_interval": 900, "aggregation": aggregation}
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    run(twin, Connecting())
    head = store.head()
    # A load has no clock: its last version is the end of the interval its
    # latest reading lies in, whether or not that interval has ended.
    assert (head.t, objects(store, "Tank")["node:T1"]["level"]) == (at(60), at_load)

    # The clock says that interval is still open.
    clock = FixedClock(at(52))
    early = tick(twin, clock)

    # The run's record cannot be earlier than the version it follows.
    (version,) = store.versions(run=early.run)
    assert (early.status, version.t, early.rows_written) == ("succeeded", at(60), 0)
    assert early.sources["scada"].datasets["levels"].cursor == "2026-01-05T00:50:00Z"

    # The rest of the interval arrives and the interval ends.
    scada.append("levels", [("T1", at(55), 3.0)])
    clock.set(at(61))
    connect = Connecting()
    report = tick(twin, clock, connect=connect)

    # The interval is read again from its start, so its value comes from all
    # of its rows and not only from those that arrived after the load.
    asked = {read["dataset"]: read["since"] for read in connect.asked}
    assert asked["levels"] == at(45)
    assert [v.t for v in store.versions(run=report.run)] == [at(60)]
    assert objects(store, "Tank")["node:T1"]["level"] == wanted
    assert store.get("node:T1").provenance_of("level").t_valid == valid
    assert report.sources["scada"].datasets["levels"].cursor == "2026-01-05T01:00:00Z"
    # Each row is counted once, though one of them was read twice.
    assert stored(store, "TwinDataset", "twin_dataset:scada/levels")["rows_read"] == 3
    assert report.rows_late == 0


def test_a_late_row_is_still_told_from_the_rows_of_an_interval_read_again(
    sync_twin: MakeTwin, run: Callable[..., Any], tick: Tick, tmp_path: Path
):
    scada = Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(Folder(tmp_path / "registry")),
        # Mapped through a view: the dataset is read whole every time.
        "scada": viewed_levels_source(scada, [("T1", at(5), 1.0), ("T1", at(50), 2.0)]),
    }
    twin = sync_twin(sources, sync={"interval": 900, "backfill_interval": 900})
    store = twin.store
    run(twin, Connecting())

    scada.append("levels", [("T1", at(7), 9.0), ("T1", at(55), 3.0)])
    report = tick(twin, FixedClock(at(61)))

    # The reading of 00:50 is read again with its interval; the one of 00:07 is late.
    assert (report.status, report.rows_late) == ("succeeded", 1)
    assert changes_of(store, "node:T1", "level") == [(at(15), 1.0), (at(60), 2.0), (at(60), 3.0)]
    assert stored(store, "TwinDataset", "twin_dataset:scada/by_tank")["rows_read"] == 4

    again = tick(twin, FixedClock(at(76)))
    assert (again.rows_late, again.rows_written) == (0, 0)


def test_a_load_still_reads_events_and_reports_as_before(
    sync_twin: MakeTwin, run: Callable[..., Any], tmp_path: Path
):
    _, lab, sources = plant(tmp_path)
    lab.write("readings", READINGS, [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(20), 3.5, 2)])
    twin = sync_twin(sources, sync={"interval": 900, "backfill_interval": 900})

    report = run(twin, Connecting()).as_dict()

    assert (report["status"], report["kind"], report["rows_late"]) == ("succeeded", "backfill", 0)
    assert [v.t for v in sync_versions(twin.store)] == [at(15), at(30)]
    assert set(objects(twin.store, "Reading", at=sync_versions(twin.store)[0])) == {"reading:R1"}
    dataset = stored(twin.store, "TwinDataset", "twin_dataset:lab/readings")
    assert (dataset["cursor_column"], dataset["cursor"]) == ("rev", "2")
