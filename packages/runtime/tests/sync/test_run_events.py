"""Events over runs that repeat: found again by their identity, corrected, never moved."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dta_runtime.clock import FixedClock
from dta_runtime.sync import Replay, ReplayUnsupported, SyncError
from dta_runtime.sync import mapper as mapper_module
from dta_runtime.twin import Twin
from live_testkit import (
    LIVE,
    META_TYPES,
    READINGS,
    TANKS,
    Connecting,
    Folder,
    at,
    domain_changes,
    levels_source,
    readings_source,
    registry_source,
    stored,
)
from sync_testkit import changes_of, objects

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]


def lab(tmp_path: Path, rows: list[tuple[Any, ...]], cursor: str | None = None):
    """A registry and a lab that reports readings as events."""
    registry, folder = Folder(tmp_path / "registry"), Folder(tmp_path / "lab")
    sources = {
        "registry": registry_source(registry),
        "lab": readings_source(folder, rows, cursor=cursor),
    }
    return registry, folder, sources


def readings(store: Any, at_version: Any = "head") -> dict[str, float]:
    return {uid: row["value"] for uid, row in objects(store, "Reading", at=at_version).items()}


def test_events_read_whole_are_found_again_by_their_identity(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, folder, sources = lab(
        tmp_path,
        [
            ("R1", "S7", at(5), 3.0, 0),
            ("R2", "S7", at(12), 3.5, 0),
            # Happened in the interval that is still open at the first run.
            ("R3", "S7", at(16.5), 4.0, 0),
        ],
    )
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))

    first = tick(twin, clock)

    (written,) = store.versions(run=first.run)
    assert (first.status, written.t) == ("succeeded", at(15))
    assert readings(store) == {"reading:R1": 3.0, "reading:R2": 3.5}
    # An event keeps the time it happened at, not the end of its interval.
    assert store.get("reading:R2").provenance_of("value").t_valid == at(12)
    assert store.get("reading:R2").values["observed_at"] == at(12)
    assert first.sources["lab"].watermark == at(12)
    # Without a column to read on from, nothing is kept of how far the dataset was read.
    dataset = first.sources["lab"].datasets["readings"]
    assert (dataset.cursor_column, dataset.cursor) == (None, None)

    folder.write(
        "readings",
        READINGS,
        [
            ("R1", "S7", at(5), 3.0, 0),
            # The lab corrected a reading the twin holds.
            ("R2", "S7", at(12), 3.75, 1),
            ("R3", "S7", at(16.5), 4.0, 0),
            ("R4", "S7", at(20), 4.5, 0),
            ("R5", "S7", at(40), 5.0, 0),
            # Reported late: it happened in an interval that is already written.
            ("R0", "S7", at(3), 2.0, 0),
        ],
    )
    clock.set(at(46))
    second = tick(twin, clock)

    early, late = store.versions(run=second.run)
    assert (second.status, early.t, late.t) == ("succeeded", at(30), at(45))
    # New events enter in the interval they happened in.
    assert set(readings(store, early)) == {"reading:R1", "reading:R2", "reading:R3", "reading:R4"}
    # The one whose interval is behind the twin, and the correction, are written last.
    assert readings(store, early)["reading:R2"] == 3.5
    assert readings(store) == {
        "reading:R0": 2.0,
        "reading:R1": 3.0,
        "reading:R2": 3.75,
        "reading:R3": 4.0,
        "reading:R4": 4.5,
        "reading:R5": 5.0,
    }
    assert changes_of(store, "reading:R2", "value") == [(at(15), 3.5), (at(45), 3.75)]
    # Both keep the time they happened at.
    assert store.get("reading:R2").provenance_of("value").t_valid == at(12)
    assert store.get("reading:R0").provenance_of("value").t_valid == at(3)
    assert store.get("reading:R5").provenance_of("value").t_valid == at(40)
    # The version written by the first run still says what it said.
    assert readings(store, written) == {"reading:R1": 3.0, "reading:R2": 3.5}
    assert changes_of(store, "reading:R1", "value") == [(at(15), 3.0)]
    assert second.rows_written == 5

    # The same rows again: nothing of the domain is written.
    before = store.head()
    third = tick(twin, clock)

    assert (third.versions_written, third.rows_written) == (1, 0)
    assert set(store.diff(before, "head")) <= META_TYPES
    assert third.sources["lab"].rows_read == 6


def test_events_with_a_cursor_are_read_on_from_where_the_last_run_stopped(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, folder, sources = lab(
        tmp_path, [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(12), 3.5, 2)], cursor="rev"
    )
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))

    first = tick(twin, clock, connect=connect)

    assert readings(store) == {"reading:R1": 3.0, "reading:R2": 3.5}
    dataset = first.sources["lab"].datasets["readings"]
    assert (dataset.cursor_column, dataset.cursor, dataset.rows_read) == ("rev", "2", 2)
    kept = stored(store, "TwinDataset", "twin_dataset:lab/readings")
    assert (kept["cursor_column"], kept["cursor"]) == ("rev", "2")

    folder.append(
        "readings",
        [
            # A revision of a reading the twin holds.
            ("R2", "S7", at(12), 3.75, 3),
            ("R3", "S7", at(20), 4.0, 4),
            # Reported late.
            ("R0", "S7", at(3), 2.0, 5),
            # Ahead of the twin's clock: it waits, and holds the cursor back.
            ("R9", "S7", at(50), 9.0, 6),
            ("R6", "S7", at(25), 6.0, 7),
        ],
    )
    clock.set(at(46))
    second = tick(twin, clock, connect=connect)

    asked = [read for read in connect.asked if read["dataset"] == "readings"]
    assert [(read["cursor_column"], read["since"]) for read in asked] == [(None, None), ("rev", 2)]
    assert second.sources["lab"].rows_read == 5
    early, late = store.versions(run=second.run)
    assert (early.t, late.t) == (at(30), at(45))
    assert set(readings(store, early)) == {
        "reading:R1",
        "reading:R2",
        "reading:R3",
        "reading:R6",
    }
    assert readings(store, early)["reading:R2"] == 3.5
    assert readings(store) == {
        "reading:R0": 2.0,
        "reading:R1": 3.0,
        "reading:R2": 3.75,
        "reading:R3": 4.0,
        "reading:R6": 6.0,
    }
    assert changes_of(store, "reading:R2", "value") == [(at(15), 3.5), (at(45), 3.75)]
    assert store.get("reading:R2").provenance_of("value").t_valid == at(12)
    assert store.get("reading:R0").provenance_of("value").t_valid == at(3)
    # The cursor stops before the row that waits, so that row is read again.
    assert second.sources["lab"].datasets["readings"].cursor == "5"
    assert second.sources["lab"].watermark == at(25)

    clock.set(at(61))
    third = tick(twin, clock, connect=connect)

    asked = [read for read in connect.asked if read["dataset"] == "readings"]
    assert asked[-1]["since"] == 5
    assert third.sources["lab"].rows_read == 2
    # The row that waited is written; the one read a second time is equal and is not.
    (version,) = store.versions(run=third.run)
    assert (version.t, third.rows_written) == (at(60), 1)
    assert domain_changes(store, late) == {"Reading"}
    assert readings(store)["reading:R9"] == 9.0
    assert changes_of(store, "reading:R6", "value") == [(at(30), 6.0)]
    assert third.sources["lab"].datasets["readings"].cursor == "7"

    # Nothing new: the source is asked for rows after the cursor and has none.
    fourth = tick(twin, clock, connect=connect)
    assert (fourth.sources["lab"].rows_read, fourth.rows_written) == (0, 0)
    assert len(objects(store, "Reading")) == 6


def test_of_several_revisions_of_one_event_the_latest_stands(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, _, sources = lab(
        tmp_path,
        [
            ("R1", "S7", at(5), 3.0, 1),
            ("R1", "S7", at(5), 3.25, 3),
            ("R1", "S7", at(5), 3.125, 2),
        ],
        cursor="rev",
    )
    twin = sync_twin(sources, sync=LIVE)

    report = tick(twin, FixedClock(at(16)))

    assert (report.status, report.rows_rejected) == ("succeeded", 0)
    reading = twin.store.get("reading:R1")
    assert (reading.values["value"], reading.values["sequence"]) == (3.25, 3)
    assert report.sources["lab"].datasets["readings"].cursor == "3"


@pytest.mark.parametrize(("lookup", "held", "looked_up"), [(5000, 1, 2), (0, 3, None)])
def test_stored_events_are_looked_up_by_identity_when_a_run_brings_few(
    sync_twin: MakeTwin,
    tick: Tick,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lookup: int,
    held: int,
    looked_up: int | None,
):
    # With the limit at nothing, every stored event is read, as when a run brings many.
    monkeypatch.setattr(mapper_module, "_EVENT_LOOKUP_MAX", lookup)
    rows = [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(8), 3.5, 2), ("R3", "S7", at(12), 4.0, 3)]
    _, folder, sources = lab(tmp_path, rows, cursor="rev")
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))
    tick(twin, clock)

    folder.append("readings", [("R2", "S7", at(8), 3.75, 4), ("R4", "S7", at(20), 4.5, 5)])
    clock.set(at(31))
    caplog.set_level(logging.DEBUG, logger="dta")
    report = tick(twin, clock)

    (read,) = [
        found.data
        for found in caplog.records
        if found.getMessage() == "stored state read" and found.data["type"] == "Reading"
    ]
    # Of the three stored events, only the one the run's rows name is read.
    assert (read["objects"], read["looked_up"]) == (held, looked_up)
    # Either way the correction and the new event are told apart from the rest.
    assert (report.status, report.rows_written) == ("succeeded", 2)
    assert changes_of(store, "reading:R2", "value") == [(at(15), 3.5), (at(30), 3.75)]
    assert changes_of(store, "reading:R4", "value") == [(at(30), 4.5)]
    assert changes_of(store, "reading:R1", "value") == [(at(15), 3.0)]
    assert len(objects(store, "Reading")) == 4


def test_a_replay_of_an_event_dataset_writes_only_what_differs(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, folder, sources = lab(
        tmp_path,
        [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(12), 3.5, 2), ("R3", "S7", at(14), 4.0, 3)],
        cursor="rev",
    )
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    # The lab repairs a value in place: its revision does not move, so a run
    # that reads on from the cursor does not see it.
    folder.write(
        "readings",
        READINGS,
        [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(12), 3.625, 2), ("R3", "S7", at(14), 4.0, 3)],
    )
    clock.set(at(31))
    unseen = tick(twin, clock, connect=connect)
    assert (unseen.sources["lab"].rows_read, unseen.rows_written) == (0, 0)
    assert readings(store)["reading:R2"] == 3.5

    before = store.head()
    asked = len(connect.asked)
    report = tick(twin, clock, connect=connect, replay=Replay("lab", "readings"))

    assert (report.kind, report.status) == ("replay", "succeeded")
    # The dataset was read whole, and only the mappings that read it were run.
    assert [(read["dataset"], read["since"]) for read in connect.asked[asked:]] == [
        ("readings", None)
    ]
    assert set(report.sources) == {"lab"}
    assert (report.sources["lab"].rows_read, report.rows_written) == (3, 1)
    assert domain_changes(store, before) == {"Reading"}
    (version,) = store.versions(run=report.run)
    assert version.t == at(30)
    assert changes_of(store, "reading:R2", "value") == [(at(15), 3.5), (at(30), 3.625)]
    assert store.get("reading:R2").provenance_of("value").t_valid == at(12)
    assert changes_of(store, "reading:R1", "value") == [(at(15), 3.0)]
    # A replay does not take the cursor back.
    assert report.sources["lab"].datasets["readings"].cursor == "3"
    assert stored(store, "TwinSyncRun", report.run)["kind"] == "incremental"

    # Replayed again, everything is equal.
    again = tick(twin, clock, connect=connect, replay=Replay("lab", "readings"))
    assert (again.kind, again.versions_written, again.rows_written) == ("replay", 1, 0)


def test_a_replay_of_a_static_dataset_writes_only_what_differs(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    registry.write(
        "tanks",
        TANKS,
        [("T1", "Hill tank", 40.0, 0.5, 6.0), ("T2", "Low tank", 21.5, 0.5, 6.0)],
    )
    scada.append("levels", [("T1", at(20), 2.0)])
    clock.set(at(31))
    before = store.head()
    asked = len(connect.asked)
    report = tick(twin, clock, connect=connect, replay=Replay("registry", "tanks"))

    assert (report.kind, report.status, report.rows_written) == ("replay", "succeeded", 1)
    assert [read["dataset"] for read in connect.asked[asked:]] == ["tanks"]
    assert domain_changes(store, before) == {"Tank"}
    assert changes_of(store, "node:T2", "elevation") == [(at(15), 20.0), (at(30), 21.5)]
    assert changes_of(store, "node:T1", "elevation") == [(at(15), 40.0)]
    # The series was not touched: the next run takes its interval as usual.
    assert objects(store, "Tank")["node:T1"]["level"] == 1.0
    following = tick(twin, clock, connect=connect)
    assert (following.kind, objects(store, "Tank")["node:T1"]["level"]) == ("incremental", 2.0)


def test_a_replay_of_a_state_series_is_refused(sync_twin: MakeTwin, tick: Tick, tmp_path: Path):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    clock = FixedClock(at(16))
    tick(twin, clock)
    head = twin.store.head()
    connect = Connecting()

    with pytest.raises(ReplayUnsupported) as caught:
        tick(twin, clock, connect=connect, replay=Replay("scada", "levels"))

    assert caught.value.code == "sync.replay_unsupported"
    assert caught.value.detail["mappings"] == ["scada.levels"]
    assert (connect.opened, twin.store.head()) == ([], head)

    with pytest.raises(SyncError) as caught:
        tick(twin, clock, connect=connect, replay=Replay("scada", "pressures"))

    assert caught.value.code == "sync.replay_unknown"
    assert (connect.opened, twin.store.head()) == ([], head)


def test_a_run_of_some_sources_leaves_the_others_as_they_were(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))

    first = tick(twin, clock, connect=connect, sources=["registry"])

    assert (first.status, set(first.sources), connect.opened) == (
        "succeeded",
        {"registry"},
        ["registry"],
    )
    assert objects(store, "Tank")["node:T1"]["level"] is None

    second = tick(twin, clock, connect=connect, sources=["scada"])

    assert set(second.sources) == {"scada"}
    assert objects(store, "Tank")["node:T1"]["level"] == 1.0

    with pytest.raises(SyncError) as caught:
        tick(twin, clock, connect=connect, sources=["plant"])

    assert caught.value.code == "sync.unknown_source"
