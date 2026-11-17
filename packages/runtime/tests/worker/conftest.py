"""Fixtures of the worker's tests: the water twin's API served over HTTP on the test
Dolt server, and a worker beside it, served the same way, reaching it as a stack's
worker reaches its API.

Without the `DTA_STORE_*` variables every test that needs them is skipped.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
from dta_core import log as core_log
from dta_core.log import current_trace_id
from dta_core.store import StoreSettings

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "api"))
# The API kits use the sync tests' sources.
sys.path.append(str(HERE.parent / "sync"))

from api_testkit import (  # noqa: E402
    TOKEN,
    WaterHistory,
    build_water_history,
    database_name,
    with_database,
)
from dta_runtime.api import create_app as create_api  # noqa: E402
from dta_runtime.protocol import TrainingSetRequest  # noqa: E402
from dta_runtime.settings import RuntimeSettings, WorkerSettings  # noqa: E402
from dta_runtime.worker import create_app as create_worker  # noqa: E402
from fastapi import Request  # noqa: E402
from live_api_testkit import served  # noqa: E402
from starlette.responses import Response  # noqa: E402
from worker_testkit import ARROW, HANDLERS, TwinApi, Worker  # noqa: E402

WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
)
# Two interactive processes and one batch process, each allowed 1 GiB of address space.
CPUS = 2.0
MEMORY_MB = 3 * 1024


@pytest.fixture(scope="session")
def water(server_settings: StoreSettings, _databases: list[str]) -> Iterator[WaterHistory]:
    name = database_name(server_settings, "worker")
    _databases.append(name)
    history = build_water_history(
        with_database(server_settings, name), WATER_ONTOLOGY.read_text(encoding="utf-8")
    )
    yield history
    history.twin.close()


def _training_set(seen: list) -> object:
    """What the API will answer on `POST /block/training-set`: a small frame."""

    async def training_set(request: Request) -> Response:
        body = await request.json()
        ref = TrainingSetRequest.model_validate(body)
        seen.append((current_trace_id(), body))
        table = pa.table(
            {
                "unique_id": ["node:T1"] * 3,
                "ds": pa.array([0, 1, 2], pa.timestamp("us", tz="UTC")),
                "y": [2.4, 2.5, 2.6],
                "target": [ref.target] * 3,
            }
        )
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return Response(sink.getvalue().to_pybytes(), media_type=ARROW)

    return training_set


@pytest.fixture(scope="session")
def twin_api(water: WaterHistory, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TwinApi]:
    settings = RuntimeSettings(
        store=water.settings,
        api_token=TOKEN,
        artifacts_dir=tmp_path_factory.mktemp("worker-artifacts"),
        logs_dir=tmp_path_factory.mktemp("worker-logs"),
    )
    app = create_api(settings, water.twin)
    frames: list = []
    app.add_api_route("/api/v1/block/training-set", _training_set(frames), methods=["POST"])
    with served(app) as url:
        yield TwinApi(url=url, settings=settings, water=water, app=app, frames=frames)


@contextlib.contextmanager
def serve_worker(
    api: TwinApi,
    work_dir: Path,
    *,
    cpus: float = CPUS,
    memory_mb: int = MEMORY_MB,
    handlers: Mapping[str, str] = HANDLERS,
) -> Iterator[Worker]:
    settings = WorkerSettings(api_url=api.url, api_token=TOKEN, cpus=cpus, memory_mb=memory_mb)
    app = create_worker(settings, handlers=handlers, handler_paths=[HERE], work_dir=work_dir)
    with served(app) as url:
        with httpx.Client(base_url=url, timeout=httpx.Timeout(120.0)) as client:
            yield Worker(url=url, app=app, settings=settings, client=client)


@pytest.fixture(scope="session")
def worker(twin_api: TwinApi, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worker]:
    with serve_worker(twin_api, tmp_path_factory.mktemp("worker")) as found:
        yield found


@pytest.fixture
def start_worker(twin_api: TwinApi, tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """A worker of the test's own, with other limits; stopped after the test."""
    stack = contextlib.ExitStack()

    def start(**options: object) -> Worker:
        return stack.enter_context(
            serve_worker(twin_api, tmp_path_factory.mktemp("worker"), **options)  # type: ignore[arg-type]
        )

    with stack:
        yield start


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    """Every line this process logs during the test, written as JSON to the returned file."""
    root = logging.getLogger("dta")
    saved = (list(root.handlers), root.propagate, root.level)
    path = tmp_path / "test.jsonl"
    core_log.configure(log_file=path, level="debug")
    try:
        yield path
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        handlers, propagate, level = saved
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)
