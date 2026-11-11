"""Runs that fail: a source that cannot be read, a manifest that cannot be used, a store
that stops answering, a run that is cut short."""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dta_core.errors import DtaError
from dta_core.log import trace
from dta_core.sources import SourceUnavailable
from dta_core.store import StoreUnavailable
from dta_runtime.sync import MappingInvalid, SourceFailed, StoreFailed, load
from dta_runtime.sync import loader as loader_module
from dta_runtime.twin import Twin
from sync_testkit import (
    MINUTE,
    T0,
    TRACE,
    Flaky,
    Sources,
    changes_of,
    columns,
    objects,
    source,
    sync_versions,
    table,
)
from telemetry import BACKFILL, OBJECT_OF, READINGS, expected_changes
from water_scenario import (
    JUNCTIONS,
    TANKS,
    junction_mapping,
    network_sources,
    network_tables,
    tank_mapping,
)

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]

SECRET = "s3cr3t-value-17"


def test_a_source_that_cannot_be_read_leaves_only_a_record_of_the_failure(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    sources = Sources(**network_tables(READINGS))
    sources.break_("scada")
    store = twin.store
    head = store.head()

    with pytest.raises(SourceFailed) as caught:
        run(twin, sources)

    error = caught.value
    assert error.code == "sync.source_failed"
    assert error.message == "source scada.readings cannot be read: connection refused"
    assert (error.detail["source"], error.detail["dataset"], error.detail["cause"]) == (
        "scada",
        "readings",
        "source.unavailable",
    )
    report = error.detail["report"]
    assert (report["status"], report["versions_written"], report["rows_written"]) == (
        "failed",
        1,
        0,
    )
    assert report["sources"]["scada"]["status"] == "failing"
    assert report["sources"]["registry"]["status"] == "ok"

    # Nothing of the data went in, though the registry had been read by then.
    assert sources.opened == ["registry", "scada"]
    assert objects(store, "Junction") == {}
    versions = sync_versions(store)
    assert [(v.seq, v.t, v.extra, v.run) for v in versions] == [
        (head.seq + 1, head.t, {}, report["run"])
    ]
    assert set(store.diff(head, "head")) == {"TwinSyncRun", "TwinSource"}
    recorded = objects(store, "TwinSyncRun")[report["run"]]
    assert (recorded["status"], recorded["kind"]) == ("failed", "backfill")
    assert recorded["error"] == (
        "sync.source_failed: source scada.readings cannot be read: connection refused"
    )
    assert (recorded["started_at"], recorded["finished_at"]) == (head.t, head.t)
    assert (recorded["versions_written"], recorded["rows_written"]) == (1, 0)
    feeds = objects(store, "TwinSource")
    assert feeds["twin_source:scada"]["status"] == "failing"
    assert feeds["twin_source:scada"]["last_error"] == "source.unavailable: connection refused"
    assert feeds["twin_source:scada"]["last_success_at"] is None
    assert feeds["twin_source:registry"]["status"] == "ok"
    assert objects(store, "TwinDataset") == {}
    assert not (tmp_path / "artifacts" / "rejects").exists()
    # Every connector opened was closed again.
    assert (sources["registry"].closed, sources["scada"].closed) == (1, 1)

    # A later run that can read the source loads everything and says the source is well.
    sources.mend("scada")
    report = run(twin, sources)

    assert report.status == "succeeded"
    assert report.resumed_run is None
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(store, uid, prop) == wanted[signal], signal
    feed = objects(store, "TwinSource")["twin_source:scada"]
    assert (feed["status"], feed["last_error"]) == ("ok", None)
    assert (feed["last_success_at"], feed["watermark"]) == (T0 + 60 * MINUTE, T0 + 50 * MINUTE)
    # The failed run stays on record.
    statuses = sorted(row["status"] for row in objects(store, "TwinSyncRun").values())
    assert statuses == ["failed", "succeeded"]


def secret_sources() -> dict[str, Any]:
    feed = source({"junctions": junction_mapping()})
    feed["location"] = "memory://reader:${PLANT_PASSWORD}@plant/registry"
    feed["env"] = ["PLANT_PASSWORD", "PLANT_REGION"]
    return {"registry": feed}


def everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{record.getMessage()} {getattr(record, 'data', '')} {record.exc_text or ''}"
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("failure", "recorded"),
    [
        (
            SourceUnavailable(f"cannot reach memory://reader:{SECRET}@plant/registry in north-3"),
            "source.unavailable: cannot reach memory://reader:${PLANT_PASSWORD}@plant/registry "
            "in ${PLANT_REGION}",
        ),
        # An error that is not the toolkit's own may say anything: only its type is kept.
        (
            RuntimeError(f"login reader:{SECRET} refused"),
            "source.error: RuntimeError while reading the source",
        ),
    ],
)
def test_what_is_kept_of_a_failure_never_holds_a_secret(
    sync_twin: MakeTwin,
    settings_of: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    recorded: str,
):
    twin = sync_twin(secret_sources())
    sources = Sources(registry={"junctions": table(JUNCTIONS)})
    sources["registry"].fail = failure
    env = {"PLANT_PASSWORD": SECRET, "PLANT_REGION": "north-3"}

    caplog.set_level(logging.DEBUG, logger="dta")
    with pytest.raises(SourceFailed) as caught, trace(TRACE):
        load(twin, settings_of(twin), env, connect=sources)

    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.detail)
    feed = objects(twin.store, "TwinSource")["twin_source:registry"]
    assert feed["last_error"] == recorded
    # The address is stored as the manifest writes it, never filled in.
    assert feed["location"] == "memory://reader:${PLANT_PASSWORD}@plant/registry"
    for row in objects(twin.store, "TwinSyncRun").values():
        assert SECRET not in row["error"]
    logged = everything_logged(caplog)
    assert "source cannot be read" in logged
    assert SECRET not in logged


def test_a_source_that_cannot_be_opened_fails_the_run_the_same_way(sync_twin: MakeTwin, run: Run):
    twin = sync_twin(network_sources())

    def refuse(source_id: str, *_: Any) -> Any:
        raise SourceUnavailable(f"no route to {source_id}")

    with pytest.raises(SourceFailed) as caught:
        run(twin, refuse)

    assert caught.value.detail["source"] == "registry"
    assert caught.value.detail["dataset"] is None
    feed = objects(twin.store, "TwinSource")["twin_source:registry"]
    assert (feed["status"], feed["last_error"]) == (
        "failing",
        "source.unavailable: no route to registry",
    )


def test_sources_are_opened_by_the_toolkit_unless_the_caller_says_how(
    sync_twin: MakeTwin, settings_of: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin({"registry": source({"junctions": junction_mapping()})})
    sources = Sources(registry={"junctions": table(JUNCTIONS)})
    connectors = types.ModuleType("dta_core.connect")
    connectors.open_connector = sources  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dta_core.connect", connectors)

    with trace(TRACE):
        report = load(twin, settings_of(twin), {})

    assert sources.opened == ["registry"]
    assert (report.status, report.rows_written) == ("succeeded", 3)


def test_without_connectors_nothing_is_read_or_written(
    sync_twin: MakeTwin, settings_of: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin({"registry": source({"junctions": junction_mapping()})})
    # Makes the import fail whether or not the connectors are installed.
    monkeypatch.setitem(sys.modules, "dta_core.connect", None)
    head = twin.store.head()

    with pytest.raises(ImportError), trace(TRACE):
        load(twin, settings_of(twin), {})

    assert twin.store.head() == head


def test_a_manifest_that_maps_nothing_loads_nothing(sync_twin: MakeTwin, run: Run):
    twin = sync_twin({})
    head = twin.store.head()

    report = run(twin, Sources())

    assert (report.status, report.versions_written, report.run) == ("succeeded", 0, "")
    assert twin.store.head() == head


def test_a_missing_variable_is_named_before_anything_is_read(sync_twin: MakeTwin, run: Run):
    twin = sync_twin(secret_sources())
    sources = Sources(registry={"junctions": table(JUNCTIONS)})
    head = twin.store.head()

    with pytest.raises(DtaError) as caught:
        run(twin, sources)

    assert caught.value.code == "config.missing_env"
    assert caught.value.detail["missing"] == ["PLANT_PASSWORD", "PLANT_REGION"]
    assert sources.opened == []
    assert twin.store.head() == head


def unusable_sources() -> dict[str, Any]:
    registry = source(
        {
            "misspelt": {**junction_mapping(), "object_type": "Junktion"},
            "abstract": {**junction_mapping(), "object_type": "Node"},
            "internal": {**junction_mapping(), "object_type": "TwinSource"},
            "tanks": {
                **tank_mapping(),
                "identity": [],
                "kind": "simulated",
                "filter": "max_level > (SELECT 1)",
                "properties": {
                    "depth": {"column": "depth"},
                    "name": {"column": "name", "expression": "upper(name)"},
                    "elevation": {},
                    "min_level": {"expression": "lag(min_level) OVER ()"},
                    "max_level": {"expression": "1; DROP TABLE tanks"},
                },
            },
            "sensors": {
                "dataset": "sensors",
                "object_type": "Sensor",
                "identity": ["id"],
                "mode": "snapshot",
                "properties": columns("quantity", "active", measures="node"),
            },
            "more_sensors": {
                "dataset": "more_sensors",
                "object_type": "Sensor",
                "identity": ["id"],
                "properties": columns("quantity", "active", measures="node"),
            },
        },
        views={"tidy": "DELETE FROM junctions"},
    )
    scada = source(
        {
            "levels": {
                "dataset": "by_tank",
                "object_type": "Tank",
                "identity": ["tank"],
                "valid_time": "ts",
                "cursor": "loaded_at",
                "mode": "snapshot",
                "properties": {},
            },
        },
        views={
            "by_tank": 'SELECT * FROM readings JOIN "archive"."signals" USING (signal)',
        },
    )
    return {"registry": registry, "scada": scada}


def test_every_problem_of_the_manifest_is_reported_together(
    sync_twin: MakeTwin, run: Run, tmp_path: Path
):
    twin = sync_twin(unusable_sources(), sync={"backfill_interval": 0.0005})
    sources = Sources(registry={}, scada={})
    head = twin.store.head()

    with pytest.raises(MappingInvalid) as caught:
        run(twin, sources)

    error = caught.value
    assert error.code == "sync.mapping_invalid"
    problems = {problem["where"]: problem["message"] for problem in error.detail["problems"]}
    registry, scada = "sources.registry.mappings", "sources.scada.mappings"
    assert set(problems) == {
        "sync.backfill_interval",
        f"{registry}.misspelt.object_type",
        f"{registry}.abstract.object_type",
        f"{registry}.internal.object_type",
        f"{registry}.tanks.identity",
        f"{registry}.tanks.kind",
        f"{registry}.tanks.filter",
        f"{registry}.tanks.properties.depth",
        f"{registry}.tanks.properties.name",
        f"{registry}.tanks.properties.elevation",
        f"{registry}.tanks.properties.min_level.expression",
        f"{registry}.tanks.properties.max_level.expression",
        f"{registry}.sensors.mode",
        "sources.registry.views.tidy",
        f"{scada}.levels.cursor",
        f"{scada}.levels.mode",
        f"{scada}.levels.properties",
        "sources.scada.views.by_tank",
    }
    # The snapshot of a time series is wrong twice over, and so is the view.
    assert len(error.detail["problems"]) == len(problems) + 2
    assert problems[f"{registry}.misspelt.object_type"] == "the ontology has no type Junktion"
    assert problems[f"{registry}.abstract.object_type"].startswith("Node is abstract; map onto ")
    assert problems[f"{registry}.tanks.properties.depth"] == "Tank has no property depth"
    assert "the only mapping of Sensor" in problems[f"{registry}.sensors.mode"]
    assert problems["sources.registry.views.tidy"] == "is not a SELECT statement"
    assert all(problem["code"] == "sync.mapping_invalid" for problem in error.detail["problems"])
    by_tank = sorted(
        problem["message"]
        for problem in error.detail["problems"]
        if problem["where"] == "sources.scada.views.by_tank"
    )
    assert by_tank == [
        'refers to readings without its source; write "<source>"."readings"',
        "refers to source 'archive', which the manifest does not have",
    ]

    # Nothing was read and nothing was written, not even a record of the run.
    assert sources.opened == []
    assert twin.store.head() == head
    assert not (tmp_path / "artifacts").exists()


def test_mappings_that_do_not_fit_the_data_fail_the_run_once_it_is_read(
    sync_twin: MakeTwin, run: Run
):
    feeds = {
        "registry": source(
            {
                # The dataset has no such column.
                "junctions": {
                    **junction_mapping(),
                    "properties": columns("name", elevation="height_m"),
                },
                # The view cannot be evaluated, so neither can its mapping.
                "tanks": {**tank_mapping(), "dataset": "tall_tanks"},
                # The valid-time column holds no time.
                "levels": {
                    "dataset": "tanks",
                    "object_type": "Tank",
                    "identity": ["id"],
                    "valid_time": "max_level",
                    "properties": columns(level="min_level"),
                },
            },
            views={"tall_tanks": 'SELECT * FROM "registry"."tanks" WHERE height > 3'},
        )
    }
    twin = sync_twin(feeds)
    sources = Sources(registry={"junctions": table(JUNCTIONS), "tanks": table(TANKS)})
    head = twin.store.head()

    with pytest.raises(MappingInvalid) as caught:
        run(twin, sources)

    problems = {problem["where"]: problem["message"] for problem in caught.value.detail["problems"]}
    assert set(problems) == {
        "sources.registry.mappings.junctions",
        "sources.registry.views.tall_tanks",
        "sources.registry.mappings.levels.valid_time",
    }
    assert "height_m" in problems["sources.registry.mappings.junctions"]
    assert problems["sources.registry.mappings.levels.valid_time"] == (
        "max_level holds DOUBLE, which is not a time"
    )
    # The data was read, so the run is on record as failed; no source is at fault.
    store = twin.store
    assert objects(store, "Junction") == {}
    assert len(sync_versions(store)) == 1
    assert set(store.diff(head, "head")) == {"TwinSyncRun"}
    (recorded,) = objects(store, "TwinSyncRun").values()
    assert recorded["status"] == "failed"
    assert recorded["error"].startswith("sync.mapping_invalid: sources.registry.")
    assert objects(store, "TwinSource")["twin_source:registry"]["status"] == "ok"


def test_two_mappings_that_write_one_property_of_one_object_are_refused(
    sync_twin: MakeTwin, run: Run
):
    feeds = {
        "registry": source({"junctions": junction_mapping()}),
        "survey": source(
            {
                "heights": {
                    "dataset": "heights",
                    "object_type": "Junction",
                    "identity": ["node"],
                    "properties": columns(elevation="height"),
                }
            }
        ),
    }
    twin = sync_twin(feeds)
    tables = {
        "registry": {"junctions": table(JUNCTIONS)},
        "survey": {"heights": table([{"node": "J2", "height": 10.5}])},
    }

    with pytest.raises(MappingInvalid) as caught:
        run(twin, Sources(**tables))

    (problem,) = caught.value.detail["problems"]
    assert problem["where"] == "sources.survey.mappings.heights.properties.elevation"
    assert "registry.junctions, survey.heights" in problem["message"]
    assert "node:J2" in problem["message"]
    assert objects(twin.store, "Junction") == {}


def test_a_version_that_was_written_although_the_call_failed_is_not_written_twice(
    sync_twin: MakeTwin, run: Run, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    flaky = Flaky(twin, monkeypatch)
    # The second version reaches the store, but the answer is lost on the way back.
    flaky.plan[2] = ("after", StoreUnavailable("connection lost"))

    report = run(twin, Sources(**network_tables(READINGS)))

    assert report.status == "succeeded"
    assert flaky.calls == 4
    assert [version.t for version in sync_versions(twin.store)] == [
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
        T0 + 45 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    assert (report.first_seq, report.last_seq, report.versions_written) == (2, 5, 4)
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(twin.store, uid, prop) == wanted[signal], signal


def test_a_version_that_was_not_written_is_committed_again(
    sync_twin: MakeTwin, run: Run, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    flaky = Flaky(twin, monkeypatch)
    flaky.plan[2] = ("before", StoreUnavailable("server is restarting"))
    flaky.plan[3] = ("before", StoreUnavailable("server is restarting"))

    report = run(twin, Sources(**network_tables(READINGS)))

    assert report.status == "succeeded"
    assert flaky.calls == 6
    assert report.versions_written == 4
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(twin.store, uid, prop) == wanted[signal], signal


def test_a_store_that_stays_away_stops_the_run_and_the_next_one_continues(
    sync_twin: MakeTwin, run: Run, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    flaky = Flaky(twin, monkeypatch)
    # The third version cannot be written, however often it is tried; the
    # record of the failure that follows can.
    for call in range(3, 3 + loader_module._COMMIT_ATTEMPTS):
        flaky.plan[call] = ("before", StoreUnavailable("server is gone"))
    sources = Sources(**network_tables(READINGS))

    with pytest.raises(StoreFailed) as caught:
        run(twin, sources)

    assert caught.value.code == "sync.store_failed"
    report = caught.value.detail["report"]
    assert (report["status"], report["versions_written"]) == ("failed", 3)
    store = twin.store
    # Two intervals are in place, then the record of the failure at the time reached.
    assert [version.t for version in sync_versions(store)] == [
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
        T0 + 30 * MINUTE,
    ]
    failed = objects(store, "TwinSyncRun")[report["run"]]
    assert failed["status"] == "failed"
    assert failed["error"].startswith("sync.store_failed: ")
    assert (failed["started_at"], failed["finished_at"]) == (T0 + 15 * MINUTE, T0 + 30 * MINUTE)
    assert failed["versions_written"] == 3

    # The next run takes up where that one stopped; no interval is written twice.
    monkeypatch.undo()
    report = run(twin, sources)

    assert report.status == "succeeded"
    assert report.resumed_run is None
    assert [version.t for version in store.versions(run=report.run)] == [
        T0 + 45 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(store, uid, prop) == wanted[signal], signal
    assert report.mappings["scada.levels"].rows_read == 3


def test_a_run_cut_short_is_continued_and_put_on_record_by_the_next(
    sync_twin: MakeTwin, run: Run, monkeypatch: pytest.MonkeyPatch
):
    twin = sync_twin(network_sources(), sync=BACKFILL)
    flaky = Flaky(twin, monkeypatch)
    # The process is stopped while the third version is on its way: nothing can
    # be recorded about the run any more.
    flaky.plan[3] = ("before", KeyboardInterrupt())
    sources = Sources(**network_tables(READINGS))

    with pytest.raises(KeyboardInterrupt):
        run(twin, sources)

    store = twin.store
    stopped = sync_versions(store)
    assert [version.t for version in stopped] == [T0 + 15 * MINUTE, T0 + 30 * MINUTE]
    cut_short = stopped[0].run
    assert objects(store, "TwinSyncRun") == {}

    monkeypatch.undo()
    report = run(twin, sources)

    assert report.status == "succeeded"
    assert report.resumed_run == cut_short
    assert report.run != cut_short
    # Only the intervals the first run did not reach are written.
    assert [version.t for version in store.versions(run=report.run)] == [
        T0 + 45 * MINUTE,
        T0 + 60 * MINUTE,
    ]
    assert len(sync_versions(store)) == 4
    wanted = expected_changes(READINGS)
    for signal, (uid, prop) in OBJECT_OF.items():
        assert changes_of(store, uid, prop) == wanted[signal], signal
    runs = objects(store, "TwinSyncRun")
    assert set(runs) == {cut_short, report.run}
    earlier = runs[cut_short]
    assert (earlier["status"], earlier["kind"], earlier["versions_written"]) == (
        "failed",
        "backfill",
        2,
    )
    assert earlier["error"].startswith("sync.interrupted: ")
    assert (earlier["started_at"], earlier["finished_at"]) == (
        T0 + 15 * MINUTE,
        T0 + 30 * MINUTE,
    )
    assert runs[report.run]["status"] == "succeeded"
