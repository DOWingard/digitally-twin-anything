"""Failures while the twin keeps running: a source fails alone and is tried again, a
store that stops answering fails the run, and the next run takes up what was left."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dta_core.errors import ConfigError
from dta_core.sources import SourceRateLimited, SourceUnavailable
from dta_core.store import StoreUnavailable
from dta_runtime.clock import FixedClock
from dta_runtime.sync import loader as loader_module
from dta_runtime.twin import Twin
from live_testkit import (
    LIVE,
    META_TYPES,
    TIME,
    Connecting,
    Folder,
    at,
    csv_source,
    domain_changes,
    levels_mapping,
    levels_source,
    readings_source,
    registry_source,
    speeds_source,
    stored,
)
from sync_testkit import Flaky, changes_of, objects, sync_versions

Tick = Callable[..., Any]
MakeTwin = Callable[..., Twin]

DOWN = "the source did not answer in time"
SLOW_DOWN = "the source asked for fewer requests"
SECRET = "s3cr3t-value-23"


def down() -> SourceUnavailable:
    return SourceUnavailable(DOWN)


def rate_limited() -> SourceRateLimited:
    return SourceRateLimited(SLOW_DOWN, detail={"retry_after_s": 120})


def test_a_source_that_is_down_and_one_that_is_rate_limited_fail_alone_and_recover(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry = Folder(tmp_path / "registry")
    scada, telemetry, lab = (Folder(tmp_path / name) for name in ("scada", "telemetry", "lab"))
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
        "telemetry": speeds_source(telemetry, [("PU1", at(5), 1.0)]),
        "lab": readings_source(lab, [("R1", "S7", at(5), 3.0, 1)], cursor="rev"),
    }
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    first = tick(twin, clock, connect=connect)
    assert (first.status, {found.status for found in first.sources.values()}) == (
        "succeeded",
        {"ok"},
    )

    # Two sources stop answering while all three go on gaining rows.
    connect.break_("telemetry", down())
    connect.break_("lab", rate_limited())
    scada.append("levels", [("T1", at(20), 2.0)])
    telemetry.append("speeds", [("PU1", at(20), 1.25)])
    lab.append("readings", [("R2", "S7", at(20), 3.5, 2)])
    clock.set(at(31))
    before = store.head()
    second = tick(twin, clock, connect=connect)

    # The healthy source is synced.
    assert (second.status, second.error) == ("partial", None)
    assert objects(store, "Tank")["node:T1"]["level"] == 2.0
    assert domain_changes(store, before) == {"Tank"}
    assert (second.sources["scada"].status, second.sources["registry"].status) == ("ok", "ok")
    # The others are named with the code of what went wrong, and keep what they had.
    idle = second.sources["telemetry"]
    assert (idle.status, idle.error) == (
        "failing",
        {"code": "source.unavailable", "message": DOWN},
    )
    assert (idle.rows_read, idle.rows_written) == (0, 0)
    assert idle.datasets["speeds"].cursor == "2026-01-05T00:15:00Z"
    assert (idle.watermark, idle.freshness_s) == (at(5), 26 * 60.0)
    limited = second.sources["lab"]
    assert (limited.status, limited.error["code"]) == ("failing", "source.rate_limited")
    assert limited.datasets["readings"].cursor == "1"
    record = stored(store, "TwinSource", "twin_source:telemetry")
    assert (record["status"], record["last_error"]) == ("failing", f"source.unavailable: {DOWN}")
    assert (record["watermark"], record["last_success_at"]) == (at(5), at(15))
    record = stored(store, "TwinSource", "twin_source:lab")
    assert (record["status"], record["last_error"]) == (
        "failing",
        f"source.rate_limited: {SLOW_DOWN}",
    )
    assert stored(store, "TwinSource", "twin_source:scada")["status"] == "ok"
    dataset = stored(store, "TwinDataset", "twin_dataset:telemetry/speeds")
    assert (dataset["cursor"], dataset["last_read_at"]) == ("2026-01-05T00:15:00Z", at(15))
    run = stored(store, "TwinSyncRun", second.run)
    assert run["status"] == "failed"
    assert run["error"] == (
        "sync.partial: some sources failed and the others were synced: "
        "lab (source.rate_limited), telemetry (source.unavailable)"
    )

    # Still failing a run later; the rows keep coming.
    scada.append("levels", [("T1", at(35), 3.0)])
    telemetry.append("speeds", [("PU1", at(35), 1.5)])
    lab.append("readings", [("R3", "S7", at(35), 4.0, 3)])
    clock.set(at(46))
    third = tick(twin, clock, connect=connect)

    assert third.status == "partial"
    assert third.sources["telemetry"].freshness_s == 41 * 60.0
    assert objects(store, "Pump")["link:PU1"]["speed"] == 1.0
    assert len(objects(store, "Reading")) == 1

    # Both answer again.
    connect.mend()
    scada.append("levels", [("T1", at(50), 4.0)])
    telemetry.append("speeds", [("PU1", at(50), 1.75)])
    lab.append("readings", [("R4", "S7", at(50), 4.5, 4)])
    clock.set(at(61))
    fourth = tick(twin, clock, connect=connect)

    assert (fourth.status, fourth.error, fourth.rows_rejected) == ("succeeded", None, 0)
    assert {found.status for found in fourth.sources.values()} == {"ok"}
    # They were asked for everything after what they had given: nothing is lost.
    asked = {read["dataset"]: read["since"] for read in connect.asked[-7:]}
    assert (asked["speeds"], asked["readings"]) == (at(15), 1)
    # Each interval that was missed is its own version, in order. A version's
    # time never goes back, so the missed ones are written at the time the twin
    # had reached, and each value keeps the interval it belongs to.
    assert changes_of(store, "link:PU1", "speed") == [
        (at(15), 1.0),
        (at(45), 1.25),
        (at(45), 1.5),
        (at(60), 1.75),
    ]
    assert [version.t for version in store.versions(run=fourth.run)] == [at(45), at(45), at(60)]
    assert store.get("link:PU1").provenance_of("speed").t_valid == at(50)
    # Nothing is written twice: one object per event, at the time it happened.
    found = objects(store, "Reading")
    assert {uid: row["value"] for uid, row in found.items()} == {
        "reading:R1": 3.0,
        "reading:R2": 3.5,
        "reading:R3": 4.0,
        "reading:R4": 4.5,
    }
    assert store.get("reading:R2").provenance_of("value").t_valid == at(20)
    assert changes_of(store, "reading:R2", "value") == [(at(60), 3.5)]
    # The healthy source was not held up by the others.
    assert changes_of(store, "node:T1", "level") == [
        (at(15), 1.0),
        (at(30), 2.0),
        (at(45), 3.0),
        (at(60), 4.0),
    ]
    for source_id in ("telemetry", "lab"):
        record = stored(store, "TwinSource", f"twin_source:{source_id}")
        assert (record["status"], record["last_error"]) == ("ok", None)
        assert (record["watermark"], record["last_success_at"]) == (at(50), at(60))
    assert fourth.sources["telemetry"].freshness_s == 11 * 60.0
    assert fourth.sources["lab"].datasets["readings"].cursor == "4"
    assert stored(store, "TwinSyncRun", fourth.run)["status"] == "succeeded"

    # And nothing of it is written again.
    before = store.head()
    fifth = tick(twin, clock, connect=connect)
    assert (fifth.status, fifth.rows_written) == ("succeeded", 0)
    assert set(store.diff(before, "head")) <= META_TYPES


def test_when_every_source_fails_the_run_fails_and_says_so(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    connect.break_("registry", down())
    connect.break_("scada", rate_limited())
    clock = FixedClock(at(16))
    before = store.head()

    caplog.set_level(logging.DEBUG, logger="dta")
    report = tick(twin, clock, connect=connect)

    assert report.status == "failed"
    # One line at error says that it failed and what was reached.
    (ended,) = [found for found in caplog.records if found.getMessage().startswith("sync run ")][1:]
    assert (ended.levelno, ended.getMessage()) == (
        logging.ERROR,
        "sync run failed: every source of the run failed",
    )
    assert (ended.data["status"], ended.data["versions_written"]) == ("failed", 1)
    assert ended.data["failing_sources"] == {
        "registry": "source.unavailable",
        "scada": "source.rate_limited",
    }
    assert report.error == {
        "code": "sync.source_failed",
        "message": "every source of the run failed: "
        "registry (source.unavailable), scada (source.rate_limited)",
    }
    # The failure is one version: the run and the sources at fault.
    (version,) = store.versions(run=report.run)
    assert (version.t, report.versions_written, report.rows_written) == (at(15), 1, 0)
    assert set(store.diff(before, "head")) <= META_TYPES
    assert objects(store, "Tank") == {}
    run = stored(store, "TwinSyncRun", report.run)
    assert (run["status"], run["error"]) == (
        "failed",
        f"{report.error['code']}: {report.error['message']}",
    )
    assert stored(store, "TwinSource", "twin_source:scada")["status"] == "failing"
    # A source that never gave anything has no watermark to be fresh by.
    assert (report.sources["scada"].watermark, report.sources["scada"].freshness_s) == (None, None)

    connect.mend()
    recovered = tick(twin, clock, connect=connect)

    assert (recovered.status, objects(store, "Tank")["node:T1"]["level"]) == ("succeeded", 1.0)
    assert stored(store, "TwinSource", "twin_source:scada")["status"] == "ok"


def joined_sources(tmp_path: Path) -> tuple[Folder, Folder, dict[str, Any]]:
    """`ops` maps tank levels through a view that joins its own list with a dataset of `scada`."""
    registry, scada, ops = (Folder(tmp_path / name) for name in ("registry", "scada", "ops"))
    feed = speeds_source(scada, [("PU1", at(5), 1.0)])
    scada.write("levels", ("tank", "ts", "level"), [("T1", at(5), 1.0), ("T2", at(6), 7.0)])
    feed["options"]["datasets"]["levels"] = {
        "glob": "levels.csv",
        "format": "csv",
        "types": {"tank": "string", "ts": TIME, "level": "double"},
    }
    ops.write("watched", ("tank",), [("T1",)])
    watch = csv_source(
        ops,
        {"watched": {"tank": "string"}},
        {"levels": levels_mapping("watched_levels")},
        views={
            "watched_levels": 'SELECT l.tank, l.ts, l.level FROM "scada"."levels" l '
            'JOIN "ops"."watched" w ON w.tank = l.tank'
        },
    )
    return scada, ops, {"registry": registry_source(registry), "scada": feed, "ops": watch}


def test_a_view_that_joins_a_failing_source_is_left_out_with_its_own_source(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    scada, _, sources = joined_sources(tmp_path)
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    connect = Connecting()
    clock = FixedClock(at(16))
    first = tick(twin, clock, connect=connect)
    assert first.status == "succeeded"
    tanks = objects(store, "Tank")
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (1.0, None)

    connect.break_("scada", down())
    scada.append("levels", [("T1", at(20), 2.0)])
    scada.append("speeds", [("PU1", at(20), 1.5)])
    clock.set(at(31))
    before = store.head()
    second = tick(twin, clock, connect=connect)

    assert second.status == "partial"
    assert second.sources["scada"].error["code"] == "source.unavailable"
    # The source whose view needs it is healthy itself, and is named as held up.
    held = second.sources["ops"]
    assert (held.status, held.error["code"]) == ("failing", "sync.dependency_failed")
    assert "scada.levels" in held.error["message"]
    assert held.datasets["watched_levels"].cursor == "2026-01-05T00:15:00Z"
    record = stored(store, "TwinSource", "twin_source:ops")
    assert record["status"] == "failing"
    assert record["last_error"].startswith("sync.dependency_failed: ")
    assert second.sources["registry"].status == "ok"
    assert domain_changes(store, before) == set()

    connect.mend()
    clock.set(at(46))
    third = tick(twin, clock, connect=connect)

    assert (third.status, {found.status for found in third.sources.values()}) == (
        "succeeded",
        {"ok"},
    )
    assert changes_of(store, "node:T1", "level") == [(at(15), 1.0), (at(30), 2.0)]
    assert changes_of(store, "link:PU1", "speed") == [(at(15), 1.0), (at(30), 1.5)]
    assert stored(store, "TwinSource", "twin_source:ops")["last_error"] is None


def test_two_mappings_that_claim_one_value_fail_the_run_and_nothing_is_taken(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    registry, scada, backup = (Folder(tmp_path / name) for name in ("registry", "scada", "backup"))
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0)]),
        "backup": levels_source(backup, [("T2", at(5), 2.0)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    store = twin.store
    clock = FixedClock(at(16))
    assert tick(twin, clock).status == "succeeded"

    # Both feeds now report the same tank in the same interval.
    scada.append("levels", [("T1", at(20), 1.5)])
    backup.append("levels", [("T1", at(21), 1.75), ("T2", at(22), 2.5)])
    clock.set(at(31))
    before = store.head()
    report = tick(twin, clock)

    assert (report.status, report.error["code"]) == ("failed", "sync.mapping_invalid")
    assert (report.versions_written, report.rows_written) == (1, 0)
    assert set(store.diff(before, "head")) <= META_TYPES
    # The failure is recorded at the time the twin had reached, not at the
    # end of the interval the run could not write.
    run = stored(store, "TwinSyncRun", report.run)
    assert (run["status"], run["finished_at"]) == ("failed", at(15))
    assert run["error"].startswith("sync.mapping_invalid: ")
    assert report.head.t == at(15)
    # No cursor moved: the rows are still to be taken once the sources agree.
    assert report.sources["scada"].datasets["levels"].cursor == "2026-01-05T00:15:00Z"
    assert report.sources["backup"].datasets["levels"].cursor == "2026-01-05T00:15:00Z"

    backup.write("levels", ("tank", "ts", "level"), [("T2", at(5), 2.0), ("T2", at(22), 2.5)])
    again = tick(twin, clock)

    # The interval is then written at its own end, as if nothing had happened.
    assert again.status == "succeeded"
    assert changes_of(store, "node:T1", "level") == [(at(15), 1.0), (at(30), 1.5)]
    assert changes_of(store, "node:T2", "level") == [(at(15), 2.0), (at(30), 2.5)]


def four_quarters(tmp_path: Path) -> dict[str, Any]:
    """A registry and an hour of tank levels, one reading in each quarter."""
    levels = [("T1", at(5), 1.0), ("T1", at(20), 2.0), ("T1", at(35), 3.0), ("T1", at(50), 4.0)]
    return {
        "registry": registry_source(Folder(tmp_path / "registry")),
        "scada": levels_source(Folder(tmp_path / "scada"), levels),
    }


HOUR = [(at(15), 1.0), (at(30), 2.0), (at(45), 3.0), (at(60), 4.0)]


def test_a_store_that_stays_away_fails_the_run_and_the_next_one_continues(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(four_quarters(tmp_path), sync=LIVE)
    store = twin.store
    flaky = Flaky(twin, monkeypatch)
    # The third version cannot be written, however often it is tried; the
    # record of the failure that follows can.
    for call in range(3, 3 + loader_module._COMMIT_ATTEMPTS):
        flaky.plan[call] = ("before", StoreUnavailable("server is gone"))
    clock = FixedClock(at(61))

    report = tick(twin, clock)

    # The run does not raise: what happened is in its report.
    assert (report.status, report.error["code"]) == ("failed", "sync.store_failed")
    assert (report.versions_written, report.head.t) == (3, at(30))
    # Two intervals are in place, then the record of the failure at the time reached.
    assert [version.t for version in store.versions(run=report.run)] == [at(15), at(30), at(30)]
    failed = stored(store, "TwinSyncRun", report.run)
    assert (failed["status"], failed["versions_written"]) == ("failed", 3)
    assert failed["error"].startswith("sync.store_failed: ")
    assert (failed["started_at"], failed["finished_at"]) == (at(15), at(30))
    # The twin knows how far the series was read.
    assert report.sources["scada"].datasets["levels"].cursor == "2026-01-05T00:30:00Z"

    # The next run takes up where that one stopped; no interval is written twice.
    monkeypatch.undo()
    following = tick(twin, clock)

    assert (following.status, following.resumed_run) == ("succeeded", None)
    assert [version.t for version in store.versions(run=following.run)] == [at(45), at(60)]
    assert changes_of(store, "node:T1", "level") == HOUR
    assert following.mappings["scada.levels"].rows_read == 2


def test_a_run_cut_short_is_continued_and_put_on_record_by_the_next(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(four_quarters(tmp_path), sync=LIVE)
    store = twin.store
    flaky = Flaky(twin, monkeypatch)
    # The process is stopped while the third version is on its way: nothing can
    # be recorded about the run any more.
    flaky.plan[3] = ("before", KeyboardInterrupt())
    clock = FixedClock(at(61))

    with pytest.raises(KeyboardInterrupt):
        tick(twin, clock)

    stopped = sync_versions(store)
    assert [version.t for version in stopped] == [at(15), at(30)]
    cut_short = stopped[0].run
    assert objects(store, "TwinSyncRun") == {}

    monkeypatch.undo()
    report = tick(twin, clock)

    assert (report.status, report.resumed_run) == ("succeeded", cut_short)
    assert report.run != cut_short
    # Only the intervals the first run did not reach are written.
    assert [version.t for version in store.versions(run=report.run)] == [at(45), at(60)]
    assert changes_of(store, "node:T1", "level") == HOUR
    earlier = stored(store, "TwinSyncRun", cut_short)
    assert (earlier["status"], earlier["kind"], earlier["versions_written"]) == (
        "failed",
        "incremental",
        2,
    )
    assert earlier["error"].startswith("sync.interrupted: ")
    assert stored(store, "TwinSyncRun", report.run)["status"] == "succeeded"


def test_a_version_written_although_the_call_failed_is_not_written_twice(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(four_quarters(tmp_path), sync=LIVE)
    flaky = Flaky(twin, monkeypatch)
    # The second version reaches the store, but the answer is lost on the way back.
    flaky.plan[2] = ("after", StoreUnavailable("connection lost"))

    report = tick(twin, FixedClock(at(61)))

    assert (report.status, report.versions_written, flaky.calls) == ("succeeded", 4, 4)
    assert changes_of(twin.store, "node:T1", "level") == HOUR


def guarded_sources(tmp_path: Path) -> dict[str, Any]:
    """A registry, pump speeds, and a lab that needs a token from the environment."""
    registry, telemetry, lab = (
        Folder(tmp_path / name) for name in ("registry", "telemetry", "lab")
    )
    sources = {
        "registry": registry_source(registry),
        # The second speed is beyond what a pump can do.
        "telemetry": speeds_source(telemetry, [("PU1", at(5), 1.0), ("PU1", at(9), 5.4321)]),
        "lab": readings_source(lab, [("R1", "S7", at(5), 3.0, 1)], cursor="rev"),
    }
    sources["lab"]["env"] = ["LAB_TOKEN"]
    return sources


def test_what_a_run_logs_and_what_it_never_does(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    twin = sync_twin(guarded_sources(tmp_path), sync=LIVE)
    connect = Connecting()
    connect.break_(
        "lab",
        SourceRateLimited(f"token {SECRET} is over its quota", detail={"retry_after_s": 120}),
    )

    caplog.set_level(logging.DEBUG, logger="dta")
    report = tick(twin, FixedClock(at(16)), connect=connect, env={"LAB_TOKEN": SECRET})

    assert (report.status, report.rows_rejected) == ("partial", 1)
    records = caplog.records
    # One line says how the run ended, with its counts and how long it took.
    (ended,) = [
        found
        for found in records
        if found.levelno >= logging.INFO and found.getMessage().startswith("sync run finished")
    ]
    assert ended.levelno == logging.WARNING
    counts = ended.data
    assert (counts["run"], counts["kind"], counts["status"]) == (
        report.run,
        "incremental",
        "partial",
    )
    assert (counts["versions_written"], counts["rows_written"]) == (1, report.rows_written)
    assert (counts["rows_read"], counts["rows_rejected"], counts["rows_late"]) == (8, 1, 0)
    # Rows set aside are counted by the reason they were set aside for.
    assert counts["set_aside_by_rule"] == {"range": 1}
    assert counts["failing_sources"] == {"lab": "source.rate_limited"}
    assert counts["duration_ms"] == report.duration_ms
    # Each version is one line, for tracing only.
    written = [found for found in records if found.getMessage() == "version written"]
    assert [found.levelno for found in written] == [logging.DEBUG]
    # The source that failed is a warning with its code: the run went on.
    (failed,) = [found for found in records if found.getMessage().startswith("source cannot be")]
    assert (failed.levelno, failed.data["source"], failed.data["code"]) == (
        logging.WARNING,
        "lab",
        "source.rate_limited",
    )
    assert failed.data["retry_after_s"] == 120
    # A secret is nowhere; a value of a row is not in what is logged at info or above.
    everything = "\n".join(
        f"{found.getMessage()} {found.data} {found.exc_text or ''}" for found in records
    )
    loud = "\n".join(
        f"{found.getMessage()} {found.data}" for found in records if found.levelno >= logging.INFO
    )
    assert SECRET not in everything
    assert "5.4321" not in loud
    assert SECRET not in json.dumps(report.as_dict())
    assert report.sources["lab"].error == {
        "code": "source.rate_limited",
        "message": "token ${LAB_TOKEN} is over its quota",
    }
    record = stored(twin.store, "TwinSource", "twin_source:lab")
    assert record["last_error"] == "source.rate_limited: token ${LAB_TOKEN} is over its quota"
    text = (tmp_path / "artifacts" / report.rejects_location).read_text(encoding="utf-8")
    assert SECRET not in text


def test_a_variable_a_source_needs_is_named_before_anything_is_read(
    sync_twin: MakeTwin, tick: Tick, tmp_path: Path
):
    twin = sync_twin(guarded_sources(tmp_path), sync=LIVE)
    head = twin.store.head()
    connect = Connecting()

    with pytest.raises(ConfigError) as caught:
        tick(twin, FixedClock(at(16)), connect=connect)

    assert (caught.value.code, caught.value.detail) == (
        "config.missing_env",
        {"missing": ["LAB_TOKEN"]},
    )
    assert (connect.opened, twin.store.head()) == ([], head)
