"""A run that repeats: readings arrive, intervals end, and each is written once."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from dta_runtime.clock import ClockUnavailable, FixedClock
from dta_runtime.twin import Twin
from live_testkit import (
    LIVE,
    META_TYPES,
    QUARTER,
    TANKS,
    Connecting,
    Folder,
    at,
    domain_changes,
    levels_source,
    registry_source,
    speeds_source,
    stored,
    viewed_levels_source,
)
from sync_testkit import changes_of, objects, sync_versions

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]


def plant(tmp_path: Path, levels: list[tuple[Any, ...]], sync: dict[str, Any] | None = None):
    """A registry and a feed of tank levels, as folders, and the sources that read them."""
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {"registry": registry_source(registry), "scada": levels_source(scada, levels)}
    return registry, scada, sources, {**LIVE, **(sync or {})}


def test_each_run_writes_the_interval_that_just_ended(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, scada, sources, sync = plant(
        tmp_path,
        [
            ("T1", at(5), 1.0),
            ("T1", at(10), 1.2),
            # Of the interval that is still open when the first run is made.
            ("T1", at(15.5), 1.5),
        ],
    )
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    clock = FixedClock(at(16))

    first = tick(twin, clock)

    assert (first.status, first.kind, first.error) == ("succeeded", "incremental", None)
    assert first.now == at(16)
    assert (first.versions_written, first.rows_late, first.rows_rejected) == (1, 0, 0)
    # The interval that ended is one version, at its end; `now` is not a version's time.
    (version,) = store.versions(run=first.run)
    assert (version.t, version.extra, version.cause, version.actor) == (at(15), {}, "sync", "sync")
    assert (first.head.id, first.head.seq, first.head.t) == (version.id, version.seq, at(15))
    assert first.first_seq == first.last_seq == version.seq
    tanks = objects(store, "Tank")
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (1.2, None)
    assert tanks["node:T1"]["name"] == "Hill tank"
    # The reading of the open interval was read but not taken.
    scada_run = first.sources["scada"]
    assert (scada_run.status, scada_run.error, scada_run.rows_read) == ("ok", None, 3)
    assert first.mappings["scada.levels"].rows_read == 2
    assert scada_run.datasets["levels"].as_dict() == {
        "rows_read": 3,
        "cursor_column": "ts",
        "cursor": "2026-01-05T00:15:00Z",
    }
    # A source is as fresh as the latest valid time taken from it.
    assert (scada_run.watermark, scada_run.freshness_s) == (at(10), 360.0)
    assert first.watermark == at(10)
    level = store.get("node:T1").provenance_of("level")
    assert (level.source, level.kind, level.t_valid) == ("scada", "observed", at(10))

    scada.append("levels", [("T1", at(20), 1.7), ("T1", at(31), 2.0)])
    clock.set(at(31))
    second = tick(twin, clock)

    assert (second.status, second.versions_written, second.rows_written) == ("succeeded", 1, 1)
    assert [v.t for v in store.versions(run=second.run)] == [at(30)]
    assert objects(store, "Tank")["node:T1"]["level"] == 1.7
    # Only rows after the cursor were asked of the source.
    assert second.sources["scada"].rows_read == 3
    assert second.mappings["scada.levels"].rows_read == 2

    clock.advance(QUARTER)
    third = tick(twin, clock)

    assert [v.t for v in store.versions(run=third.run)] == [at(45)]
    assert changes_of(store, "node:T1", "level") == [
        (at(15), 1.2),
        (at(30), 1.7),
        (at(45), 2.0),
    ]
    assert store.get("node:T1").provenance_of("level").t_valid == at(31)
    # What the twin keeps of the run, the dataset and the source.
    run = stored(store, "TwinSyncRun", third.run)
    assert (run["kind"], run["status"], run["error"]) == ("incremental", "succeeded", None)
    assert (run["started_at"], run["finished_at"], run["versions_written"]) == (at(45), at(45), 1)
    # The registry is read whole each time (six rows); the feed gave the one row after its cursor.
    assert (run["rows_read"], run["rows_written"], run["watermark"]) == (7, 1, at(31))
    dataset = stored(store, "TwinDataset", "twin_dataset:scada/levels")
    assert (dataset["cursor_column"], dataset["cursor"]) == ("ts", "2026-01-05T00:45:00Z")
    assert (dataset["rows_read"], dataset["last_read_at"]) == (5, at(45))
    feed = stored(store, "TwinSource", "twin_source:scada")
    assert (feed["status"], feed["last_error"]) == ("ok", None)
    assert (feed["watermark"], feed["last_success_at"]) == (at(31), at(45))
    json.dumps(third.as_dict())


def test_the_report_is_what_the_brief_of_a_run_asks_for(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, _, sources, sync = plant(tmp_path, [("T1", at(5), 1.0)])
    registry.write("tanks", (*TANKS, "surveyed_by"), [("T1", "Hill tank", 40.0, 0.5, 6.0, "crew")])
    sources["registry"]["options"]["datasets"]["tanks"]["types"]["surveyed_by"] = "string"
    twin = sync_twin(sources, sync=sync)

    report = tick(twin, FixedClock(at(16))).as_dict()

    assert set(report) >= {
        "run",
        "kind",
        "now",
        "status",
        "error",
        "versions_written",
        "first_seq",
        "last_seq",
        "head",
        "rows_read",
        "rows_written",
        "rows_rejected",
        "rows_late",
        "rejects_location",
        "watermark",
        "duration_ms",
        "unmapped_columns",
        "sources",
    }
    assert (report["now"], report["watermark"]) == ("2026-01-05T00:16:00Z", "2026-01-05T00:05:00Z")
    assert set(report["head"]) == {"id", "seq", "t"}
    assert report["head"]["t"] == "2026-01-05T00:15:00Z"
    # A column of a source that no mapping reads is named, as in a load.
    assert report["unmapped_columns"] == ["registry.tanks.surveyed_by"]
    assert set(report["sources"]) == {"registry", "scada"}
    for found in report["sources"].values():
        assert set(found) == {
            "status",
            "error",
            "watermark",
            "freshness_s",
            "rows_read",
            "rows_written",
            "rows_rejected",
            "rows_late",
            "datasets",
        }
    # A source that is only read whole is as fresh as its last successful read.
    registry_run = report["sources"]["registry"]
    assert (registry_run["watermark"], registry_run["freshness_s"]) == ("2026-01-05T00:16:00Z", 0.0)
    assert registry_run["datasets"]["tanks"] == {
        "rows_read": 1,
        "cursor_column": None,
        "cursor": None,
    }
    feed = stored(twin.store, "TwinSource", "twin_source:registry")
    assert (feed["watermark"], feed["last_success_at"]) == (at(16), at(15))


def test_a_clock_that_jumped_is_caught_up_one_interval_at_a_time(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, scada, sources, sync = plant(tmp_path, [("T1", at(5), 1.0)])
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    clock = FixedClock(at(16))
    tick(twin, clock)

    # Six hours pass without a run: a reading in each interval but the fourth,
    # and one of the interval that is open when the next run is made.
    rows = [("T1", at(15 * k + 7), 1.0 + 0.125 * k) for k in range(1, 25) if k != 4]
    scada.append("levels", [*rows, ("T1", at(15 * 25 + 2), 9.0)])
    clock.advance(timedelta(hours=6))
    report = tick(twin, clock)

    assert (report.status, report.kind) == ("succeeded", "incremental")
    versions = store.versions(run=report.run)
    # One version per interval that has changes, in time order, and one to end at.
    wanted = [at(15 * (k + 1)) for k in range(1, 25) if k != 4]
    assert [version.t for version in versions] == wanted
    assert wanted[-1] == at(375) and clock.now() == at(376)
    assert all(version.extra == {} for version in versions)
    assert (report.versions_written, report.rows_written) == (23, 23)
    assert (report.first_seq, report.last_seq) == (versions[0].seq, versions[-1].seq)
    assert changes_of(store, "node:T1", "level") == [
        (at(15), 1.0),
        *((at(15 * (k + 1)), 1.0 + 0.125 * k) for k in range(1, 25) if k != 4),
    ]
    # How far the series was read is known after every version.
    cursors = [
        objects(store, "TwinDataset", at=version)["twin_dataset:scada/levels"]["cursor"]
        for version in versions[:3]
    ]
    assert cursors == ["2026-01-05T00:30:00Z", "2026-01-05T00:45:00Z", "2026-01-05T01:00:00Z"]
    assert objects(store, "Tank")["node:T1"]["level"] == 4.0
    assert report.sources["scada"].freshness_s == 9 * 60.0


def test_a_second_run_at_the_same_time_writes_its_record_and_nothing_else(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada, sources, sync = plant(tmp_path, [("T1", at(5), 1.0), ("T1", at(20), 2.0)])
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    clock = FixedClock(at(31))
    first = tick(twin, clock)
    head = store.head()
    assert [v.t for v in store.versions(run=first.run)] == [at(15), at(30)]

    again = tick(twin, clock)

    # Exactly one version, at the end of the last interval that has ended,
    # holding the run's record and no object of the domain.
    assert (again.status, again.versions_written, again.rows_written) == ("succeeded", 1, 0)
    (version,) = store.versions(run=again.run)
    assert (version.seq, version.t) == (head.seq + 1, at(30))
    assert set(store.diff(head, "head")) <= META_TYPES
    assert again.run != first.run
    assert stored(store, "TwinSyncRun", again.run)["status"] == "succeeded"
    assert changes_of(store, "node:T1", "level") == [(at(15), 1.0), (at(30), 2.0)]
    assert again.sources["scada"].datasets["levels"].cursor == "2026-01-05T00:30:00Z"

    # The sources change while the clock stands still: a reading of the open
    # interval waits, a changed static row is written at the time reached.
    scada.append("levels", [("T1", at(30.5), 3.0)])
    registry.write("tanks", TANKS, [("T1", "Hill tank", 41.5, 0.5, 6.0)])
    before = store.head()
    third = tick(twin, clock)

    assert (third.versions_written, third.rows_written) == (1, 1)
    (version,) = store.versions(run=third.run)
    assert version.t == at(30)
    assert domain_changes(store, before) == {"Tank"}
    tank = store.get("node:T1")
    assert (tank.values["elevation"], tank.values["level"]) == (41.5, 2.0)
    assert tank.provenance_of("elevation").t_valid == at(30)


@pytest.mark.parametrize(
    ("aggregation", "wanted"), [("mean", 2.75), ("min", 1.0), ("max", 5.0), ("last", 2.0)]
)
def test_an_interval_whose_rows_arrive_across_two_runs_is_reduced_once(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, aggregation: str, wanted: float
):
    _, scada, sources, sync = plant(
        tmp_path,
        [("T1", at(5), 0.5), ("T1", at(16), 1.0), ("T1", at(20), 3.0)],
        sync={"aggregation": aggregation},
    )
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    clock = FixedClock(at(21))
    # The interval that ends at 00:30 is half there: nothing of it is taken.
    tick(twin, clock)
    assert changes_of(store, "node:T1", "level") == [(at(15), 0.5)]

    scada.append("levels", [("T1", at(25), 5.0), ("T1", at(28), 2.0)])
    clock.set(at(31))
    report = tick(twin, clock)

    # One value from all four rows, as if they had been read together.
    assert report.mappings["scada.levels"].rows_read == 4
    assert changes_of(store, "node:T1", "level") == [(at(15), 0.5), (at(30), pytest.approx(wanted))]
    # A value reduced from several holds for the interval; the last one keeps its own time.
    valid = store.get("node:T1").provenance_of("level").t_valid
    assert valid == (at(28) if aggregation == "last" else at(30))
    assert len(store.versions(run=report.run)) == 1


def test_a_row_behind_the_cursor_is_late_and_one_ahead_of_now_waits(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    # The series is mapped through a view, so its dataset is read whole every time.
    feed = viewed_levels_source(scada, [("T1", at(5), 1.0), ("T1", at(10), 1.2)])
    twin = sync_twin({"registry": registry_source(registry), "scada": feed}, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))
    first = tick(twin, clock)
    assert (first.rows_late, objects(store, "Tank")["node:T1"]["level"]) == (0, 1.2)

    scada.append(
        "levels",
        [
            # Arrives after its interval was written: it cannot go behind what the twin holds.
            ("T1", at(7), 5.5),
            ("T1", at(20), 1.5),
            # The source is ahead of the twin's clock.
            ("T1", at(40), 3.0),
        ],
    )
    clock.set(at(31))
    second = tick(twin, clock)

    assert (second.status, second.rows_late, second.rows_rejected) == ("succeeded", 1, 0)
    assert second.sources["scada"].rows_late == 1
    assert second.mappings["scada.levels"].rows_read == 1
    assert objects(store, "Tank")["node:T1"]["level"] == 1.5
    assert second.sources["scada"].datasets["by_tank"].cursor == "2026-01-05T00:30:00Z"

    # The late row is counted once; the row that waited is taken when its interval ends.
    clock.set(at(46))
    third = tick(twin, clock)

    assert (third.rows_late, third.rows_written) == (0, 1)
    assert changes_of(store, "node:T1", "level") == [(at(15), 1.2), (at(30), 1.5), (at(45), 3.0)]
    assert store.get("node:T1").provenance_of("level").t_valid == at(40)
    # Every row was counted once, when it was taken or found late.
    assert stored(store, "TwinDataset", "twin_dataset:scada/by_tank")["rows_read"] == 5


def test_a_source_asked_only_for_newer_rows_does_not_show_a_late_one(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, scada, sources, sync = plant(tmp_path, [("T1", at(5), 1.0), ("T1", at(10), 1.2)])
    twin = sync_twin(sources, sync=sync)
    connect = Connecting()
    clock = FixedClock(at(16))
    tick(twin, clock, connect=connect)

    scada.append("levels", [("T1", at(7), 5.5), ("T1", at(20), 1.5)])
    clock.set(at(31))
    report = tick(twin, clock, connect=connect)

    asked = [read for read in connect.asked if read["dataset"] == "levels"]
    assert [(read["cursor_column"], read["since"]) for read in asked] == [
        (None, None),
        ("ts", at(15)),
    ]
    # The connector filters exactly: the late row never reaches the twin.
    assert (report.sources["scada"].rows_read, report.rows_late) == (1, 0)
    assert changes_of(twin.store, "node:T1", "level") == [(at(15), 1.2), (at(30), 1.5)]


def test_a_reading_beyond_its_bounds_does_not_take_its_interval_with_it(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    speeds = [
        ("PU1", at(5), 1.0),
        # Exactly the highest speed the ontology allows.
        ("PU1", at(8), 2.0),
        # Beyond it, and the last reading of its interval.
        ("PU1", at(12), 5.25),
    ]
    feed = speeds_source(scada, speeds)
    sync = {**LIVE, "aggregation": "mean"}
    twin = sync_twin({"registry": registry_source(registry), "scada": feed}, sync=sync)
    store = twin.store

    report = tick(twin, FixedClock(at(16)))

    assert (report.status, report.rows_rejected) == ("succeeded", 1)
    # The interval's value comes from the readings that hold.
    assert changes_of(store, "link:PU1", "speed") == [(at(15), 1.5)]
    counts = report.mappings["scada.speeds"]
    assert (counts.rows_read, counts.rows_written, counts.rows_rejected) == (3, 1, 1)
    text = (tmp_path / "artifacts" / report.rejects_location).read_text(encoding="utf-8")
    (line,) = [json.loads(found) for found in text.splitlines()]
    assert (line["mapping"], line["uid"], line["property"], line["rule"]) == (
        "scada.speeds",
        "link:PU1",
        "speed",
        "range",
    )
    assert (line["t"], line["run"]) == ("2026-01-05T00:12:00Z", report.run)
    assert "5.25" not in text


def test_a_run_in_which_every_row_was_set_aside_is_recorded_once(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    feed = speeds_source(scada, [("PU1", at(5), 7.5), ("PU1", at(9), 8.5)])
    twin = sync_twin({"registry": registry_source(registry), "scada": feed}, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))

    first = tick(twin, clock, connect=connect)

    # The registry went in; the feed is marked for the batch it could not use.
    assert (first.status, first.error) == ("partial", None)
    assert (first.rows_rejected, first.rows_written) == (2, 6)
    feed_run = first.sources["scada"]
    assert (feed_run.status, feed_run.error["code"]) == ("failing", "sync.all_rejected")
    assert first.sources["registry"].status == "ok"
    assert objects(store, "Pump")["link:PU1"]["speed"] is None
    record = stored(store, "TwinSource", "twin_source:scada")
    assert record["status"] == "failing"
    assert record["last_error"] == "sync.all_rejected: every row of mapping speeds was set aside"
    run = stored(store, "TwinSyncRun", first.run)
    assert (run["status"], run["rows_rejected"]) == ("failed", 2)
    assert run["error"].startswith("sync.partial: ")
    assert run["rejects_location"] == first.rejects_location
    # The cursor moved past the rows all the same.
    assert feed_run.datasets["speeds"].cursor == "2026-01-05T00:15:00Z"

    clock.set(at(31))
    second = tick(twin, clock, connect=connect)

    # The next run asks for newer rows only: nothing is set aside a second time.
    asked = [read for read in connect.asked if read["dataset"] == "speeds"]
    assert asked[-1]["since"] == at(15)
    assert (second.status, second.rows_rejected, second.rejects_location) == ("succeeded", 0, None)
    assert second.sources["scada"].rows_read == 0
    assert second.sources["scada"].status == "ok"
    record = stored(store, "TwinSource", "twin_source:scada")
    assert (record["status"], record["last_error"]) == ("ok", None)
    statuses = sorted(row["status"] for row in objects(store, "TwinSyncRun").values())
    assert statuses == ["failed", "succeeded"]

    # A good reading is then taken as usual.
    scada.append("speeds", [("PU1", at(35), 1.25)])
    clock.set(at(46))
    third = tick(twin, clock, connect=connect)
    assert (third.status, objects(store, "Pump")["link:PU1"]["speed"]) == ("succeeded", 1.25)


class NoClock:
    kind = "source:plant"

    def now(self):
        raise ClockUnavailable("the clock of source 'plant' cannot be read: no answer")

    def close(self) -> None:
        return None


def test_without_a_clock_nothing_is_read_and_nothing_is_written(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, _, sources, sync = plant(tmp_path, [("T1", at(5), 1.0)])
    twin = sync_twin(sources, sync=sync)
    head = twin.store.head()
    connect = Connecting()

    with pytest.raises(ClockUnavailable) as caught:
        tick(twin, NoClock(), connect=connect)

    assert caught.value.code == "clock.unavailable"
    assert connect.opened == []
    assert twin.store.head() == head
    assert sync_versions(twin.store) == []
    assert not (tmp_path / "artifacts").exists()


def test_a_run_needs_the_time_between_runs(sync_twin: MakeTwin, tick: Tick, tmp_path: Path):
    _, _, sources, _ = plant(tmp_path, [("T1", at(5), 1.0)])
    twin = sync_twin(sources, sync={"backfill_interval": 900})
    head = twin.store.head()
    connect = Connecting()

    with pytest.raises(Exception) as caught:
        tick(twin, FixedClock(at(16)), connect=connect)

    assert caught.value.code == "sync.mapping_invalid"
    assert [problem["where"] for problem in caught.value.detail["problems"]] == ["sync.interval"]
    assert connect.opened == []
    assert twin.store.head() == head


def test_a_twin_installed_later_than_its_data_records_it_at_the_time_it_has_reached(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    _, _, sources, sync = plant(tmp_path, [("T1", at(5), 1.0), ("T1", at(20), 2.0)])
    # The twin's first version is later than both readings.
    twin = sync_twin(sources, sync=sync, t=at(40))
    store = twin.store

    report = tick(twin, FixedClock(at(46)))

    # A version's time never goes back: each interval is its own version, in
    # order, at the time the twin had reached, and keeps its own valid time.
    versions = store.versions(run=report.run)
    assert [version.t for version in versions] == [at(40), at(40), at(45)]
    assert changes_of(store, "node:T1", "level") == [(at(40), 1.0), (at(40), 2.0)]
    assert store.get("node:T1").provenance_of("level").t_valid == at(20)
    # The objects the readings are of were written first.
    assert objects(store, "Tank", at=versions[0])["node:T1"]["level"] == 1.0


def test_an_object_new_to_the_twin_is_there_before_its_readings_of_the_same_run(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada, sources, sync = plant(tmp_path, [("T1", at(5), 1.0)])
    registry.write("tanks", TANKS, [("T1", "Hill tank", 40.0, 0.5, 6.0)])
    twin = sync_twin(sources, sync=sync)
    store = twin.store
    clock = FixedClock(at(16))
    tick(twin, clock)

    # A tank is added to the registry together with an hour of its readings,
    # while the name of a tank the twin holds is corrected.
    registry.write(
        "tanks",
        TANKS,
        [("T1", "North hill tank", 40.0, 0.5, 6.0), ("T2", "Low tank", 20.0, 0.5, 6.0)],
    )
    scada.append("levels", [("T2", at(20), 2.0), ("T2", at(50), 2.5)])
    clock.set(at(61))
    report = tick(twin, clock)

    assert (report.status, report.rows_rejected) == ("succeeded", 0)
    assert [version.t for version in store.versions(run=report.run)] == [at(30), at(60)]
    # The new tank enters with its first reading; the correction is written last.
    assert changes_of(store, "node:T2", "level") == [(at(30), 2.0), (at(60), 2.5)]
    assert changes_of(store, "node:T1", "name") == [
        (at(15), "Hill tank"),
        (at(60), "North hill tank"),
    ]
