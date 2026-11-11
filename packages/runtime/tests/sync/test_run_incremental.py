"""A dataset beneath views that the manifest declares as read on: each run asks the source
only for the rows after what its views have taken, and the twin holds what it would
hold had the dataset been read whole every time."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import parse_manifest
from dta_core.ontology import parse_ontology
from dta_core.sources import SourceUnavailable
from dta_runtime.clock import FixedClock
from dta_runtime.sync import MappingInvalid, Replay, ReplayUnsupported
from dta_runtime.sync.plan import build_plan
from dta_runtime.twin import Twin
from live_testkit import (
    JUNCTION_STATE_MAPPING,
    LIVE,
    TANK_LEVEL_MAPPING,
    TANK_LEVELS,
    Connecting,
    Folder,
    asked_of,
    at,
    csv_source,
    levels_source,
    readings_source,
    register_signals,
    registry_source,
    stored,
    telemetry_source,
)
from sync_testkit import changes_of, objects, sync_versions

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]

# Reduced from several rows: an interval taken in two parts would show.
SYNC = {"interval": 900, "backfill_interval": 900, "aggregation": "mean"}
DOMAIN = ("Tank", "Junction", "Pump", "Sensor")
SERIES = [
    ("node:T1", "level"),
    ("node:T2", "level"),
    ("node:J1", "pressure"),
    ("node:J1", "demand"),
    ("node:J2", "pressure"),
    ("node:J2", "demand"),
]
BASE = {"L1": 1.0, "L2": 2.0, "P1": 40.0, "D1": 1.0, "P2": 45.0, "D2": 2.0}
# When every instrument reports within a quarter of an hour: just after it has
# begun, twice inside it, and exactly at its end.
MINUTES = (0.5, 4.0, 9.0, 15.0)
RELATIONS = ("tank_levels", "junction_state")


def arrived(after: float, until: float) -> list[tuple[Any, ...]]:
    """The readings of all instruments made later than `after` and no later than `until`,
    both in minutes."""
    rows = []
    quarter = 0
    while 15 * quarter < until:
        for minute in MINUTES:
            moment = 15 * quarter + minute
            if after < moment <= until:
                for position, (signal, base) in enumerate(BASE.items()):
                    value = base + ((quarter * 3 + position + int(minute)) % 7) * 0.25
                    rows.append((signal, at(moment), value))
        quarter += 1
    return rows


def mean(signal: str, until: float) -> float:
    """What the readings of one instrument in the quarter that ends at `until` reduce to."""
    values = [value for name, _, value in arrived(until - 15, until) if name == signal]
    return sum(values) / len(values)


@dataclass
class World:
    """One twin, the folder its telemetry comes from, and what it asked of its sources."""

    twin: Twin
    telemetry: Folder
    connect: Connecting
    clock: FixedClock
    # Minutes up to which the telemetry has arrived.
    reached: float = -1.0

    def arrive(self, until: float) -> None:
        self.telemetry.append("readings", arrived(self.reached, until))
        self.reached = until
        self.clock.set(at(until))


def world(
    sync_twin: MakeTwin, folder: Path, *, declared: bool, sync: dict[str, Any] | None = None
) -> World:
    registry = Folder(folder / "registry")
    telemetry = Folder(folder / "telemetry")
    sources = {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(telemetry, declared=declared),
    }
    return World(sync_twin(sources, sync=sync or SYNC), telemetry, Connecting(), FixedClock(at(0)))


def counts(report: Any) -> dict[str, Any]:
    """Everything a report counts, but the rows received from the sources."""
    return {
        "status": report.status,
        "error": report.error,
        "versions_written": report.versions_written,
        "rows_written": report.rows_written,
        "rows_rejected": report.rows_rejected,
        "rows_late": report.rows_late,
        "watermark": report.watermark,
        "mappings": {
            label: (found.rows_read, found.rows_written, found.rows_rejected)
            for label, found in report.mappings.items()
        },
        "sources": {
            name: (
                found.status,
                found.error,
                found.rows_written,
                found.rows_rejected,
                found.rows_late,
                found.watermark,
            )
            for name, found in report.sources.items()
        },
    }


def held(twin: Twin) -> dict[str, Any]:
    """What a twin holds of the domain, when each value changed, and its versions' times."""
    store = twin.store
    relations = {
        name: stored(store, "TwinDataset", f"twin_dataset:telemetry/{name}")
        for name in RELATIONS
        if f"twin_dataset:telemetry/{name}" in objects(store, "TwinDataset")
    }
    return {
        "objects": {name: objects(store, name) for name in DOMAIN},
        "changes": {series: changes_of(store, *series) for series in SERIES},
        "valid": {
            series: store.get(series[0]).provenance_of(series[1]).t_valid
            for series in SERIES
            if store.get(series[0]).values.get(series[1]) is not None
        },
        "versions": [version.t for version in sync_versions(store)],
        # What each view has had taken from it, and how far.
        "taken": {
            name: (row["cursor_column"], row["cursor"], row["rows_read"])
            for name, row in relations.items()
        },
    }


def said_of(caplog: pytest.LogCaptureFixture, dataset: str) -> dict[str, Any]:
    """What the one line a run logs about reading a dataset says."""
    (found,) = [
        record.data
        for record in caplog.records
        if record.getMessage() == "dataset read"
        and record.context == "sync.extract"
        and record.data["dataset"] == dataset
    ]
    return found


def same(on: World, whole: World, reports: tuple[Any, Any]) -> None:
    assert counts(reports[0]) == counts(reports[1])
    assert held(on.twin) == held(whole.twin)


def test_a_dataset_read_on_gives_the_twin_what_reading_it_whole_gives(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    on = world(sync_twin, tmp_path / "on", declared=True)
    whole = world(sync_twin, tmp_path / "whole", declared=False)
    both = (on, whole)

    def run(until: float) -> tuple[Any, Any]:
        for found in both:
            found.arrive(until)
        reports = (
            tick(on.twin, on.clock, connect=on.connect),
            tick(whole.twin, whole.clock, connect=whole.connect),
        )
        same(on, whole, reports)
        return reports

    def received(report: Any) -> int:
        return report.sources["telemetry"].rows_read

    # The first run: nothing has been taken yet, so everything is asked for.
    first = run(16)
    assert first[0].status == "succeeded"
    assert asked_of(on.connect, "readings") == [None]
    assert received(first[0]) == received(first[1]) == len(arrived(-1, 16))

    # One interval at a time: only what came after the last one written.
    second = run(31)
    assert asked_of(on.connect, "readings")[-1] == at(15)
    # The reading made exactly at 00:15 was taken by the first run and is not sent again;
    # the one of 00:15:30, which that run had to leave, is.
    assert received(second[0]) == len(arrived(15, 31)) == 30
    assert received(second[1]) == len(arrived(-1, 31)) == 54
    third = run(46)
    assert asked_of(on.connect, "readings")[-1] == at(30)
    assert (received(third[0]), received(third[1])) == (30, 78)

    # Four intervals without a run, then one run for all of them.
    caught_up = run(106)
    assert asked_of(on.connect, "readings")[-1] == at(45)
    assert caught_up[0].versions_written == 4
    assert (received(caught_up[0]), received(caught_up[1])) == (len(arrived(45, 106)), 174)

    # The telemetry cannot be reached for two runs.
    for found in both:
        found.connect.break_("telemetry", SourceUnavailable("the plant does not answer"))
    before = asked_of(on.connect, "readings")
    kept = {
        name: stored(on.twin.store, "TwinDataset", f"twin_dataset:telemetry/{name}")
        for name in ("readings", *RELATIONS)
    }
    for until in (121, 136):
        failing = run(until)
        assert failing[0].status == "partial"
        assert failing[0].sources["telemetry"].rows_read == 0
        assert failing[0].sources["telemetry"].datasets["readings"].cursor == "2026-01-05T01:45:00Z"
    # Nothing was read, and nothing the twin knows about the source's datasets has moved.
    assert asked_of(on.connect, "readings") == before
    for name, record in kept.items():
        assert stored(on.twin.store, "TwinDataset", f"twin_dataset:telemetry/{name}") == record
    assert (kept["readings"]["cursor_column"], kept["readings"]["last_read_at"]) == (
        "ts",
        at(105),
    )
    assert kept["tank_levels"]["cursor"] == "2026-01-05T01:45:00Z"

    # It answers again: asked from where it had been left, every interval missed is
    # there, reduced from all of its rows, at the time the twin had reached meanwhile.
    for found in both:
        found.connect.mend()
    recovered = run(151)
    assert recovered[0].status == "succeeded"
    assert asked_of(on.connect, "readings")[-1] == at(105)
    assert (received(recovered[0]), received(recovered[1])) == (len(arrived(105, 151)), 246)
    assert changes_of(on.twin.store, "node:J1", "pressure")[-3:] == [
        (at(135), pytest.approx(mean("P1", 120))),
        (at(135), pytest.approx(mean("P1", 135))),
        (at(150), pytest.approx(mean("P1", 150))),
    ]

    # Nothing new: the source is asked for what came after the last interval, and has
    # only the reading the last run had to leave.
    idle = run(151)
    assert asked_of(on.connect, "readings")[-1] == at(150)
    assert (received(idle[0]), idle[0].rows_written) == (6, 0)

    # The dataset's own record says that it is read on, and by which column. A cursor
    # there would be that of a series reading the dataset itself: how far it has been
    # read is on the records of the views, and each run's report gives their least.
    record = stored(on.twin.store, "TwinDataset", "twin_dataset:telemetry/readings")
    assert (record["cursor_column"], record["cursor"]) == ("ts", None)
    assert record["last_read_at"] == at(150)
    # It counts the rows each run received: those left for a later run are counted again.
    assert record["rows_read"] == sum(
        len(arrived(since, until))
        for since, until in ((-1, 16), (15, 31), (30, 46), (45, 106), (105, 151), (150, 151))
    )
    listed = idle[0].sources["telemetry"].datasets["readings"]
    assert (listed.cursor_column, listed.cursor) == ("ts", "2026-01-05T02:30:00Z")
    # The one read whole keeps no cursor, and counts every row every time.
    record = stored(whole.twin.store, "TwinDataset", "twin_dataset:telemetry/readings")
    assert (record["cursor_column"], record["cursor"]) == (None, None)
    # Through each view every row was taken once, whichever way the dataset was read.
    for name, signals in (("tank_levels", 2), ("junction_state", 2)):
        taken = stored(on.twin.store, "TwinDataset", f"twin_dataset:telemetry/{name}")
        assert taken["rows_read"] == signals * 10 * len(MINUTES)
        assert (taken["cursor_column"], taken["cursor"]) == ("ts", "2026-01-05T02:30:00Z")
    # Never asked for anything but everything, the other twin.
    assert set(asked_of(whole.connect, "readings")) == {None}


def test_a_load_and_the_runs_after_it_read_on_as_well(
    sync_twin: MakeTwin, run: Callable[..., Any], tick: Tick, tmp_path: Path
):
    on = world(sync_twin, tmp_path / "on", declared=True)
    whole = world(sync_twin, tmp_path / "whole", declared=False)
    # History that ends inside an interval: the last readings are of 00:49.
    for found in (on, whole):
        found.arrive(50)
    loaded = (run(on.twin, on.connect), run(whole.twin, whole.connect))
    assert (loaded[0].status, loaded[0].kind) == ("succeeded", "backfill")
    assert loaded[0].versions_written == loaded[1].versions_written == 4
    assert held(on.twin) == held(whole.twin)
    assert asked_of(on.connect, "readings") == [None]
    record = stored(on.twin.store, "TwinDataset", "twin_dataset:telemetry/readings")
    assert (record["cursor_column"], record["rows_read"]) == ("ts", len(arrived(-1, 50)))
    taken = stored(on.twin.store, "TwinDataset", "twin_dataset:telemetry/tank_levels")
    assert taken["cursor"] == "2026-01-05T00:49:00Z"

    def go(until: float) -> tuple[Any, Any]:
        for found in (on, whole):
            found.arrive(until)
        reports = (
            tick(on.twin, on.clock, connect=on.connect),
            tick(whole.twin, whole.clock, connect=whole.connect),
        )
        same(on, whole, reports)
        return reports

    # The interval the load found open is read again from its start, so that its
    # value is reduced from all of its rows.
    first = go(61)
    assert asked_of(on.connect, "readings")[-1] == at(45)
    assert first[0].sources["telemetry"].rows_read == len(arrived(45, 61)) == 30
    assert first[0].rows_late == 0
    level = [value for t, value in changes_of(on.twin.store, "node:T1", "level") if t == at(60)]
    assert level[-1] == pytest.approx(mean("L1", 60))
    # Counted once, though the readings of 00:45:30 and 00:49 were read twice.
    taken = stored(on.twin.store, "TwinDataset", "twin_dataset:telemetry/tank_levels")
    assert taken["rows_read"] == 2 * 4 * len(MINUTES)

    go(76)
    assert asked_of(on.connect, "readings")[-1] == at(60)

    # A load made later reads on too.
    for found in (on, whole):
        found.arrive(88)
    again = (run(on.twin, on.connect), run(whole.twin, whole.connect))
    assert again[0].status == again[1].status == "succeeded"
    assert asked_of(on.connect, "readings")[-1] == at(75)
    assert held(on.twin) == held(whole.twin)
    assert set(asked_of(whole.connect, "readings")) == {None}


def test_a_mapping_added_later_has_the_dataset_read_whole_once(
    sync_twin: MakeTwin,
    revise: Callable[..., None],
    tick: Tick,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    registry, telemetry = Folder(tmp_path / "registry"), Folder(tmp_path / "telemetry")
    sources = {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(
            telemetry, arrived(-1, 16), declared=True, mappings={"levels": TANK_LEVEL_MAPPING}
        ),
    }
    twin = sync_twin(sources, sync=SYNC)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)
    telemetry.append("readings", arrived(16, 31))
    clock.set(at(31))
    tick(twin, clock, connect=connect)
    assert asked_of(connect, "readings") == [None, at(15)]
    assert objects(store, "Junction")["node:J1"]["pressure"] is None

    # The manifest gains a mapping that reads the dataset through another view.
    sources["telemetry"]["mappings"] = {
        "levels": TANK_LEVEL_MAPPING,
        "junctions": JUNCTION_STATE_MAPPING,
    }
    revise(twin, sources, SYNC)
    telemetry.append("readings", arrived(31, 46))
    clock.set(at(46))
    caplog.set_level(logging.INFO, logger="dta")
    report = tick(twin, clock, connect=connect)

    # Nothing has been taken through the new view, so it needs every row there is.
    assert asked_of(connect, "readings")[-1] is None
    read = said_of(caplog, "readings")
    assert (read["read"], read["since"], read["rows"]) == ("whole", None, len(arrived(-1, 46)))
    assert read["whole_because"] == "nothing has been taken through telemetry.junction_state yet"
    assert (report.status, report.rows_late) == ("succeeded", 0)
    # The junctions get their whole history, the intervals behind the twin at the time
    # it had reached; the tanks only the interval that is new.
    assert changes_of(store, "node:J1", "pressure")[1:] == [
        (at(30), pytest.approx(mean("P1", 15))),
        (at(30), pytest.approx(mean("P1", 30))),
        (at(45), pytest.approx(mean("P1", 45))),
    ]
    assert changes_of(store, "node:T1", "level") == [
        (at(15), pytest.approx(mean("L1", 15))),
        (at(30), pytest.approx(mean("L1", 30))),
        (at(45), pytest.approx(mean("L1", 45))),
    ]
    taken = stored(store, "TwinDataset", "twin_dataset:telemetry/tank_levels")
    assert taken["rows_read"] == 2 * 3 * len(MINUTES)
    listed = report.sources["telemetry"].datasets["readings"]
    assert (listed.cursor_column, listed.cursor) == ("ts", "2026-01-05T00:45:00Z")

    # From then on it is read on.
    telemetry.append("readings", arrived(46, 61))
    clock.set(at(61))
    caplog.clear()
    report = tick(twin, clock, connect=connect)

    assert asked_of(connect, "readings")[-1] == at(45)
    read = said_of(caplog, "readings")
    assert (read["read"], read["since"], read["by"]) == ("on", "2026-01-05T00:45:00Z", "ts")
    assert (read["rows"], read["whole_because"]) == (len(arrived(45, 61)), None)
    assert changes_of(store, "node:J1", "pressure")[-1] == (
        at(60),
        pytest.approx(mean("P1", 60)),
    )


def test_a_changed_valid_time_column_has_the_dataset_read_whole_once(
    sync_twin: MakeTwin,
    revise: Callable[..., None],
    tick: Tick,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    registry, telemetry = Folder(tmp_path / "registry"), Folder(tmp_path / "telemetry")
    sources = {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(
            telemetry, arrived(-1, 16), declared=True, mappings={"levels": TANK_LEVEL_MAPPING}
        ),
    }
    twin = sync_twin(sources, sync=SYNC)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    # The view gives the time of its rows under another name from now on.
    sources["telemetry"]["views"]["tank_levels"] = TANK_LEVELS.replace(
        "r.ts,", "r.ts AS measured_at,"
    )
    sources["telemetry"]["mappings"] = {
        "levels": {**TANK_LEVEL_MAPPING, "valid_time": "measured_at"}
    }
    revise(twin, sources, SYNC)
    telemetry.append("readings", arrived(16, 31))
    clock.set(at(31))
    caplog.set_level(logging.INFO, logger="dta")
    report = tick(twin, clock, connect=connect)

    # How far the view was taken is known by a column it no longer has.
    assert asked_of(connect, "readings") == [None, None]
    read = said_of(caplog, "readings")
    assert (read["read"], read["rows"]) == ("whole", len(arrived(-1, 31)))
    assert read["whole_because"] == "the valid-time column of telemetry.tank_levels changed"
    # The rows taken before are not taken again, nor are they late.
    assert (report.status, report.rows_written, report.rows_late) == ("succeeded", 2, 0)
    assert changes_of(store, "node:T1", "level") == [
        (at(15), pytest.approx(mean("L1", 15))),
        (at(30), pytest.approx(mean("L1", 30))),
    ]
    taken = stored(store, "TwinDataset", "twin_dataset:telemetry/tank_levels")
    assert (taken["cursor_column"], taken["cursor"], taken["rows_read"]) == (
        "measured_at",
        "2026-01-05T00:30:00Z",
        2 * 2 * len(MINUTES),
    )

    telemetry.append("readings", arrived(31, 46))
    clock.set(at(46))
    tick(twin, clock, connect=connect)

    assert asked_of(connect, "readings")[-1] == at(30)


def test_a_view_nothing_has_come_through_keeps_the_dataset_read_whole(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    def levels(after: float, until: float) -> list[tuple[Any, ...]]:
        return [row for row in arrived(after, until) if row[0].startswith("L")]

    registry, telemetry = Folder(tmp_path / "registry"), Folder(tmp_path / "telemetry")
    sources = {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(telemetry, levels(-1, 16), declared=True),
    }
    twin = sync_twin(sources, sync=SYNC)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    # Only the tanks' instruments report: the view of the junctions has no row yet,
    # so there is no time after which it is known to need nothing.
    telemetry.append("readings", levels(16, 31))
    clock.set(at(31))
    caplog.set_level(logging.INFO, logger="dta")
    report = tick(twin, clock, connect=connect)

    assert asked_of(connect, "readings") == [None, None]
    read = said_of(caplog, "readings")
    assert (read["read"], read["rows"]) == ("whole", len(levels(-1, 31)))
    assert read["whole_because"] == "nothing has been taken through telemetry.junction_state yet"
    assert (report.status, report.rows_written, report.rows_late) == ("succeeded", 2, 0)
    taken = stored(store, "TwinDataset", "twin_dataset:telemetry/tank_levels")
    assert (taken["cursor"], taken["rows_read"]) == ("2026-01-05T00:30:00Z", 2 * 2 * len(MINUTES))
    # Its record says by which column it is to be read on, and that it is not yet.
    record = stored(store, "TwinDataset", "twin_dataset:telemetry/readings")
    assert (record["cursor_column"], record["cursor"]) == ("ts", None)
    listed = report.sources["telemetry"].datasets["readings"]
    assert (listed.cursor_column, listed.cursor) == ("ts", None)

    # The junctions' instruments report: read whole once more, then on.
    telemetry.append("readings", arrived(31, 46))
    clock.set(at(46))
    report = tick(twin, clock, connect=connect)
    listed = report.sources["telemetry"].datasets["readings"]
    assert (listed.cursor_column, listed.cursor) == ("ts", "2026-01-05T00:45:00Z")
    telemetry.append("readings", arrived(46, 61))
    clock.set(at(61))
    tick(twin, clock, connect=connect)

    assert asked_of(connect, "readings")[2:] == [None, at(45)]
    # The first reading of that quarter of an hour was made before they began to report.
    came = [value for name, ts, value in arrived(31, 45) if name == "P1"]
    assert changes_of(store, "node:J1", "pressure")[1:] == [
        (at(45), pytest.approx(sum(came) / len(came))),
        (at(60), pytest.approx(mean("P1", 60))),
    ]


def test_a_dataset_no_longer_declared_is_read_whole_again(
    sync_twin: MakeTwin,
    revise: Callable[..., None],
    tick: Tick,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    registry, telemetry = Folder(tmp_path / "registry"), Folder(tmp_path / "telemetry")
    sources = {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(telemetry, arrived(-1, 16), declared=True),
    }
    twin = sync_twin(sources, sync=SYNC)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)
    telemetry.append("readings", arrived(16, 31))
    clock.set(at(31))
    tick(twin, clock, connect=connect)
    assert asked_of(connect, "readings") == [None, at(15)]

    del sources["telemetry"]["incremental"]
    revise(twin, sources, SYNC)
    telemetry.append("readings", arrived(31, 46))
    clock.set(at(46))
    caplog.set_level(logging.INFO, logger="dta")
    report = tick(twin, clock, connect=connect)

    assert asked_of(connect, "readings")[-1] is None
    read = said_of(caplog, "readings")
    assert (read["read"], read["rows"], read["whole_because"]) == (
        "whole",
        len(arrived(-1, 46)),
        None,
    )
    # Its record no longer names a column it would be read on by.
    record = stored(store, "TwinDataset", "twin_dataset:telemetry/readings")
    assert (record["cursor_column"], record["cursor"]) == (None, None)
    listed = report.sources["telemetry"].datasets["readings"]
    assert (listed.cursor_column, listed.cursor) == (None, None)
    # The views go on from where they were: nothing twice, nothing late.
    assert (report.status, report.rows_written, report.rows_late) == ("succeeded", 4, 0)
    for name in RELATIONS:
        taken = stored(store, "TwinDataset", f"twin_dataset:telemetry/{name}")
        assert (taken["cursor"], taken["rows_read"]) == (
            "2026-01-05T00:45:00Z",
            2 * 3 * len(MINUTES),
        )


def lagging(tmp_path: Path, declared: bool = True) -> tuple[Folder, dict[str, Any]]:
    """A dataset read by a series of its own and, from another source, through a view."""
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    feed = levels_source(scada, [("T1", at(5), 1.0)])
    if declared:
        feed["incremental"] = {"levels": "ts"}
    ops = csv_source(
        Folder(tmp_path / "ops"),
        {},
        {
            "heads": {
                "dataset": "heads",
                "object_type": "Junction",
                "identity": ["junction"],
                "valid_time": "ts",
                "properties": {"pressure": {"column": "pressure"}},
            }
        },
        views={
            "heads": "SELECT 'J1' AS junction, ts, level * 10 AS pressure "
            'FROM "scada"."levels" WHERE tank = \'T1\''
        },
    )
    return scada, {"registry": registry_source(registry), "scada": feed, "ops": ops}


def test_only_what_a_run_takes_decides_from_where_it_reads(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    scada, sources = lagging(tmp_path)
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)
    assert asked_of(connect, "levels") == [None]

    # Two runs of the feed alone: the view of the other source is not taken, and
    # does not hold the reading back.
    for minutes, level in ((20, 2.0), (35, 3.0)):
        scada.append("levels", [("T1", at(minutes), level)])
        clock.set(at(minutes + 11))
        report = tick(twin, clock, connect=connect, sources=["scada"])
        assert report.status == "succeeded"
    assert asked_of(connect, "levels") == [None, at(15), at(30)]
    assert changes_of(store, "node:J1", "pressure") == [(at(15), 10.0)]
    # The record is that of the dataset's own series: every row once, as far as it has taken.
    record = stored(store, "TwinDataset", "twin_dataset:scada/levels")
    assert (record["cursor_column"], record["cursor"], record["rows_read"]) == (
        "ts",
        "2026-01-05T00:45:00Z",
        3,
    )

    # A run of everything reads from where the view was left.
    scada.append("levels", [("T1", at(50), 4.0)])
    clock.set(at(61))
    report = tick(twin, clock, connect=connect)

    assert asked_of(connect, "levels")[-1] == at(15)
    assert report.sources["scada"].rows_read == 3
    # The two readings the dataset's own series had taken are behind its cursor,
    # and are not late: they are told from late ones by their number.
    assert (report.status, report.rows_late) == ("succeeded", 0)
    assert report.mappings["scada.levels"].rows_read == 1
    assert changes_of(store, "node:T1", "level") == [
        (at(15), 1.0),
        (at(30), 2.0),
        (at(45), 3.0),
        (at(60), 4.0),
    ]
    # The view catches up: an interval each, in order, at the time the twin had reached.
    assert changes_of(store, "node:J1", "pressure") == [
        (at(15), 10.0),
        (at(45), 20.0),
        (at(45), 30.0),
        (at(60), 40.0),
    ]
    record = stored(store, "TwinDataset", "twin_dataset:scada/levels")
    assert (record["cursor"], record["rows_read"]) == ("2026-01-05T01:00:00Z", 4)

    scada.append("levels", [("T1", at(65), 5.0)])
    clock.set(at(76))
    tick(twin, clock, connect=connect)
    assert asked_of(connect, "levels")[-1] == at(60)


def test_a_series_added_later_on_the_dataset_itself_is_given_every_row(
    sync_twin: MakeTwin, revise: Callable[..., None], tick: Tick, tmp_path: Path
):
    asked: dict[bool, list[Any]] = {}
    holds: dict[bool, Any] = {}
    for declared in (True, False):
        scada, sources = lagging(tmp_path / ("on" if declared else "whole"), declared)
        # At first the dataset is taken through the view of the other source only.
        series = sources["scada"]["mappings"]
        sources["scada"]["mappings"] = {}
        twin = sync_twin(sources, sync=LIVE)
        store = twin.store
        connect = Connecting()
        clock = FixedClock(at(16))
        tick(twin, clock, connect=connect)
        scada.append("levels", [("T1", at(20), 2.0)])
        clock.set(at(31))
        tick(twin, clock, connect=connect)
        # No cursor is kept on the dataset's own record for the series to go on from.
        record = stored(store, "TwinDataset", "twin_dataset:scada/levels")
        assert (record["cursor_column"], record["cursor"]) == ("ts" if declared else None, None)

        sources["scada"]["mappings"] = series
        revise(twin, sources, LIVE)
        scada.append("levels", [("T1", at(35), 3.0)])
        clock.set(at(46))
        report = tick(twin, clock, connect=connect)
        assert (report.status, report.rows_late) == ("succeeded", 0)
        scada.append("levels", [("T1", at(50), 4.0)])
        clock.set(at(61))
        tick(twin, clock, connect=connect)

        asked[declared] = asked_of(connect, "levels")
        holds[declared] = (
            changes_of(store, "node:T1", "level"),
            changes_of(store, "node:J1", "pressure"),
        )
        record = stored(store, "TwinDataset", "twin_dataset:scada/levels")
        assert (record["cursor_column"], record["cursor"]) == ("ts", "2026-01-05T01:00:00Z")

    # Nothing had been taken by the new series: every row once, then on.
    assert asked == {True: [None, at(15), None, at(45)], False: [None] * 4}
    assert holds[True] == holds[False]
    levels, pressures = holds[True]
    assert levels[1:] == [(at(30), 1.0), (at(30), 2.0), (at(45), 3.0), (at(60), 4.0)]
    assert pressures == [(at(15), 10.0), (at(30), 20.0), (at(45), 30.0), (at(60), 40.0)]


def test_a_dataset_read_on_cannot_be_replayed(sync_twin: MakeTwin, tick: Tick, tmp_path: Path):
    on = world(sync_twin, tmp_path, declared=True)
    on.arrive(16)
    tick(on.twin, on.clock, connect=on.connect)
    head = on.twin.store.head()
    asked = len(on.connect.asked)

    with pytest.raises(ReplayUnsupported) as caught:
        tick(on.twin, on.clock, connect=on.connect, replay=Replay("telemetry", "readings"))

    assert caught.value.detail["mappings"] == ["telemetry.junctions", "telemetry.levels"]
    assert (len(on.connect.asked), on.twin.store.head()) == (asked, head)


def refused(sync_twin: MakeTwin, tick: Tick, sources: dict[str, Any]) -> list[tuple[str, str]]:
    """Where and why a manifest with these sources cannot be planned."""
    twin = sync_twin(sources, sync=LIVE)
    head = twin.store.head()
    connect = Connecting()
    with pytest.raises(MappingInvalid) as caught:
        tick(twin, FixedClock(at(16)), connect=connect)
    assert (connect.opened, twin.store.head()) == ([], head)
    return [(found["where"], found["message"]) for found in caught.value.detail["problems"]]


def plant(tmp_path: Path) -> dict[str, Any]:
    registry, telemetry = Folder(tmp_path / "registry"), Folder(tmp_path / "telemetry")
    return {
        "registry": register_signals(registry_source(registry), registry),
        "telemetry": telemetry_source(telemetry, arrived(-1, 16), declared=True),
    }


def test_a_view_cannot_be_declared_as_read_on(sync_twin: MakeTwin, tick: Tick, tmp_path: Path):
    sources = plant(tmp_path)
    sources["telemetry"]["incremental"] = {"tank_levels": "ts"}

    assert refused(sync_twin, tick, sources) == [
        (
            "sources.telemetry.incremental.tank_levels",
            "tank_levels is a view; only a dataset the source holds can be read on. "
            "Declare the datasets the view reads instead",
        )
    ]


def test_a_dataset_nothing_reads_cannot_be_declared_as_read_on(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    sources = plant(tmp_path)
    sources["telemetry"]["incremental"] = {"readings": "ts", "alarms": "raised_at"}

    assert refused(sync_twin, tick, sources) == [
        (
            "sources.telemetry.incremental.alarms",
            "no mapping reads alarms, directly or through a view; map it or remove the declaration",
        )
    ]


def test_a_dataset_is_declared_as_read_on_by_a_column(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    sources = plant(tmp_path)
    sources["telemetry"]["incremental"] = {"readings": ""}

    assert refused(sync_twin, tick, sources) == [
        (
            "sources.telemetry.incremental.readings",
            "names no column holding the time of the dataset's rows",
        )
    ]


def test_a_static_mapping_cannot_read_a_dataset_read_on(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    sources = plant(tmp_path)
    # The latest reading of each junction, kept as if it had no time of its own.
    sources["telemetry"]["views"]["latest"] = (
        "SELECT junction, arg_max(pressure, ts) AS pressure "
        'FROM "telemetry"."junction_state" GROUP BY junction'
    )
    sources["telemetry"]["mappings"]["latest"] = {
        "dataset": "latest",
        "object_type": "Junction",
        "identity": ["junction"],
        "properties": {"pressure": {"column": "pressure"}},
    }
    # The register is mapped as it is, and declared although it holds no readings.
    sources["registry"]["incremental"] = {"tanks": "id"}

    assert refused(sync_twin, tick, sources) == [
        (
            "sources.registry.incremental.tanks",
            "registry.tanks is a static mapping that reads tanks: it compares everything the "
            "source holds with what is stored, and would take the rows that were not read for "
            "removed. Map it from a dataset that is read whole, or remove the declaration",
        ),
        (
            "sources.telemetry.incremental.readings",
            "telemetry.latest is a static mapping that reads readings through the view latest: "
            "it compares everything the source holds with what is stored, and would take the "
            "rows that were not read for removed. Map it from a dataset that is read whole, or "
            "remove the declaration",
        ),
    ]


def test_events_cannot_come_from_a_dataset_read_on(sync_twin: MakeTwin, tick: Tick, tmp_path: Path):
    registry, lab = Folder(tmp_path / "registry"), Folder(tmp_path / "lab")
    feed = readings_source(lab, [("R1", "S7", at(5), 3.0, 1)], cursor="rev")
    feed["incremental"] = {"readings": "ts"}

    assert refused(sync_twin, tick, {"registry": registry_source(registry), "lab": feed}) == [
        (
            "sources.lab.incremental.readings",
            "lab.readings maps events from readings: an event is found again by its identity "
            "among all rows of its dataset, so the dataset cannot be read on by time. Give the "
            "events a dataset of their own, read on by the mapping's cursor, or remove the "
            "declaration",
        )
    ]


def test_a_series_reads_its_dataset_by_the_column_it_is_declared_with(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    feed = levels_source(scada, [("T1", at(5), 1.0)])
    feed["incremental"] = {"levels": "received_at"}

    assert refused(sync_twin, tick, {"registry": registry_source(registry), "scada": feed}) == [
        (
            "sources.scada.incremental.levels",
            "scada.levels reads levels by its valid_time ts, but the dataset is declared as "
            "read on by received_at; name the same column in both",
        )
    ]


def test_a_series_that_reads_its_dataset_itself_is_read_on_as_before(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    feed = levels_source(scada, [("T1", at(5), 1.0), ("T1", at(10), 1.2)])
    feed["incremental"] = {"levels": "ts"}
    twin = sync_twin({"registry": registry_source(registry), "scada": feed}, sync=LIVE)
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    scada.append("levels", [("T1", at(20), 1.5)])
    clock.set(at(31))
    report = tick(twin, clock, connect=connect)

    assert asked_of(connect, "levels") == [None, at(15)]
    assert (report.sources["scada"].rows_read, report.rows_late) == (1, 0)
    assert changes_of(twin.store, "node:T1", "level") == [(at(15), 1.2), (at(30), 1.5)]
    record = stored(twin.store, "TwinDataset", "twin_dataset:scada/levels")
    assert (record["cursor_column"], record["cursor"], record["rows_read"]) == (
        "ts",
        "2026-01-05T00:30:00Z",
        3,
    )


REFERENCE = Path(__file__).resolve().parents[4] / "testbed"


def test_the_reference_twin_of_the_plant_can_declare_its_telemetry(tmp_path: Path):
    manifest_file = REFERENCE / "twin-live" / "manifest.yaml"
    ontology_file = REFERENCE / "twin" / "ontology" / "water_utility.yaml"
    if not (manifest_file.is_file() and ontology_file.is_file()):
        pytest.skip("the reference twin is not in this tree")
    document = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    document["sources"]["telemetry"]["incremental"] = {"readings": "timestamp"}
    manifest = parse_manifest(document, origin="the reference twin")
    ontology = parse_ontology(ontology_file.read_text(encoding="utf-8")).ontology

    plan = build_plan(manifest, ontology, recurring=True)

    (readings,) = [dataset for dataset in plan.datasets if dataset.name == "readings"]
    assert (readings.source_id, readings.cursor_column) == ("telemetry", "timestamp")
    # Its rows are taken through the two views; both must have got as far before
    # the dataset is asked for less.
    assert readings.decided_by == (
        ("telemetry", "asset_readings"),
        ("telemetry", "pump_readings"),
    )
    # The register the views join it with is still read whole.
    (sensor,) = [dataset for dataset in plan.datasets if dataset.name == "sensor"]
    assert (sensor.cursor_column, sensor.decided_by) == (None, ())
