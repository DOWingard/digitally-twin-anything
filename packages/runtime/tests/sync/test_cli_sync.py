"""`dta-runtime sync`: one run of a live twin from the command line.

The twin is a real one in the store and its sources are folders of CSV files.
Its manifest names no source as its clock, so the run is handed the wall
clock: every interval of the files, which hold January 2026, has ended.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from dta_runtime.cli import app
from dta_runtime.twin import Twin
from live_testkit import LIVE, Folder, at, levels_source, registry_source, stored
from sync_testkit import objects
from typer.testing import CliRunner

MakeTwin = Callable[..., Twin]


def _env(twin: Twin, tmp_path: Path) -> dict[str, str]:
    store = twin.store.settings
    return {
        "DTA_STORE_HOST": store.host,
        "DTA_STORE_PORT": str(store.port),
        "DTA_STORE_USER": store.user,
        "DTA_STORE_PASSWORD": store.password,
        "DTA_STORE_DATABASE": store.database,
        "DTA_API_TOKEN": "t" * 32,
        "DTA_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "DTA_LOGS_DIR": str(tmp_path / "logs"),
    }


@contextlib.contextmanager
def _logging_kept() -> Iterator[None]:
    """The command sets logging up for a process of its own, on streams that close
    with it; the tests that follow get back what they share."""
    root = logging.getLogger("dta")
    handlers, propagate, level = list(root.handlers), root.propagate, root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in handlers:
                handler.close()
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)


def _sync(args: list[str], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    with _logging_kept():
        result = CliRunner().invoke(app, ["sync", *args], env=env)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.output  # exactly one document on standard output
    return result.exit_code, json.loads(lines[0])


def _twin(sync_twin: MakeTwin, tmp_path: Path) -> tuple[Twin, Folder, Folder]:
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0), ("T1", at(10), 1.2)]),
    }
    return sync_twin(sources, sync=LIVE), registry, scada


def test_sync_makes_one_run_and_prints_its_report(sync_twin: MakeTwin, tmp_path: Path):
    twin, _, _ = _twin(sync_twin, tmp_path)
    env = _env(twin, tmp_path)

    code, report = _sync([], env)

    assert code == 0
    assert (report["status"], report["kind"], report["error"]) == ("succeeded", "incremental", None)
    assert report["trace_id"] and report["now"]
    assert report["sources"]["scada"]["status"] == "ok"
    assert objects(twin.store, "Tank")["node:T1"]["level"] == 1.2
    run = stored(twin.store, "TwinSyncRun", report["run"])
    assert (run["status"], run["kind"]) == ("succeeded", "incremental")
    # The time the run was handed is kept with its record.
    assert run["clock_at"].isoformat().replace("+00:00", "Z") == report["now"]

    # A run kept to one source reads only that source, and records itself.
    code, second = _sync(["--source", "scada"], env)
    assert code == 0 and list(second["sources"]) == ["scada"]
    assert (second["versions_written"], second["rows_written"]) == (1, 0)


def test_sync_refuses_what_cannot_be_asked_before_reading_anything(
    sync_twin: MakeTwin, tmp_path: Path
):
    twin, _, _ = _twin(sync_twin, tmp_path)
    env = _env(twin, tmp_path)
    before = twin.store.head().seq

    code, document = _sync(["--replay", "levels"], env)
    assert code != 0 and document["error"]["code"] == "runtime.bad_replay"
    code, document = _sync(["--replay", "scada.levels"], env)
    assert code != 0 and document["error"]["code"] == "sync.replay_unsupported"
    code, document = _sync(["--source", "nowhere"], env)
    assert code != 0 and document["error"]["code"] == "sync.unknown_source"

    assert twin.store.head().seq == before


def test_sync_of_which_every_source_failed_exits_with_the_report(
    sync_twin: MakeTwin, tmp_path: Path
):
    twin, registry, scada = _twin(sync_twin, tmp_path)
    env = _env(twin, tmp_path)
    shutil.rmtree(registry.path)
    shutil.rmtree(scada.path)

    code, document = _sync([], env)

    assert code != 0
    error = document["error"]
    assert error["code"] == "sync.source_failed"
    report = error["detail"]["report"]
    assert report["status"] == "failed"
    assert {name: found["status"] for name, found in report["sources"].items()} == {
        "registry": "failing",
        "scada": "failing",
    }
    # The failed run is in the twin all the same.
    assert stored(twin.store, "TwinSyncRun", report["run"])["status"] == "failed"
