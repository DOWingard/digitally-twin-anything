"""A live twin of a small plant behind the API.

Its sources are folders of CSV files read by the real file connector; their
addresses name the folder through a variable, as a real manifest names
anything secret. The clock is one the test moves by hand. A source can be made
to fail, or to hold its reads until the test lets them go.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import uvicorn
from api_testkit import TOKEN
from dta_core.manifest import Manifest
from dta_core.manifest.models import Source
from dta_core.sources import Connector
from dta_runtime.clock import ClockUnavailable, FixedClock
from dta_runtime.settings import RuntimeSettings
from dta_runtime.twin import Twin
from httpx import Response
from live_testkit import (
    LIVE,
    Connecting,
    Folder,
    at,
    levels_source,
    readings_source,
    registry_source,
)
from runtime_testkit import water_manifest

# The variable holding the folder the plant's sources live in.
PLANT_ENV = "DTA_TEST_PLANT_DIR"
STALENESS_S = 3600.0
QUESTIONS = {
    "levels": {"text": "How full is each tank?", "staleness": int(STALENESS_S)},
    "flows": {"text": "What does each sensor read?", "staleness": 2 * int(STALENESS_S)},
}
JOB_TIMEOUT_S = 90.0
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def live_manifest(
    ontology_text: str,
    sources: Mapping[str, Any],
    *,
    sync: Mapping[str, Any] | None = LIVE,
    tier: str = "live",
) -> Manifest:
    sections: dict[str, Any] = {
        "contract": {"tier": tier, "questions": QUESTIONS},
        "sources": dict(sources),
    }
    if sync is not None:
        sections["sync"] = dict(sync)
    return water_manifest(ontology_text, **sections)


def hidden(document: dict[str, Any], source_id: str) -> dict[str, Any]:
    """A source whose folder is named through the plant's variable."""
    return {**document, "location": f"${{{PLANT_ENV}}}/{source_id}"}


class Gate:
    """Holds the reads of a source until the test opens it."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.reached = threading.Event()

    def open(self) -> None:
        self.opened.set()


class Held:
    """A connector whose reads wait at a gate."""

    def __init__(self, connector: Connector, gate: Gate) -> None:
        self.source_id = connector.source_id
        self._connector = connector
        self._gate = gate

    def datasets(self) -> list[str]:
        return self._connector.datasets()

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        self._gate.reached.set()
        if not self._gate.opened.wait(JOB_TIMEOUT_S):
            raise TimeoutError("the test never opened the gate")
        return self._connector.read(
            dataset, columns=columns, since=since, cursor_column=cursor_column, limit=limit
        )

    def close(self) -> None:
        self._connector.close()

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Plant(Connecting):
    """The real file connectors of the plant's sources, broken or held at the test's word."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, Gate] = {}

    def hold(self, source_id: str) -> Gate:
        gate = self.gates[source_id] = Gate()
        return gate

    def release(self) -> None:
        for gate in self.gates.values():
            gate.open()
        self.gates.clear()

    def __call__(self, source_id: str, source: Source, env: Mapping[str, str]) -> Connector:
        connector = super().__call__(source_id, source, env)
        gate = self.gates.get(source_id)
        return Held(connector, gate) if gate is not None else connector


class SwitchedClock(FixedClock):
    """A fixed clock that can be made to fail as a source's clock fails."""

    kind = "source:plant"

    def __init__(self, at: datetime) -> None:
        super().__init__(at)
        self.down = False

    def now(self) -> datetime:
        if self.down:
            raise ClockUnavailable(
                "the clock of source 'plant' cannot be read: it did not answer",
                detail={"source": "plant", "cause": "source.unavailable"},
            )
        return super().now()


@dataclass
class LiveApi:
    """The API of a live twin, called in process (`TestClient`) or over HTTP."""

    client: httpx.Client
    twin: Twin
    clock: FixedClock
    plant: Plant
    settings: RuntimeSettings
    app: Any

    @property
    def services(self) -> Any:
        return self.app.state.services

    def health(self) -> dict[str, Any]:
        return self.ok("/api/v1/meta/health")

    def runs(self, **params: Any) -> list[dict[str, Any]]:
        return self.ok("/api/v1/sync/runs", params=params)["data"]

    def sources(self) -> dict[str, dict[str, Any]]:
        return {found["id"]: found for found in self.ok("/api/v1/sync/sources")["data"]}

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.client.get(path, headers=HEADERS, **kwargs)

    def post(self, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Response:
        return self.client.post(path, headers={**HEADERS, **(headers or {})}, **kwargs)

    def ok(self, path: str, **kwargs: Any) -> Any:
        response = self.get(path, **kwargs)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()

    def wait(self, job_id: str, timeout_s: float = JOB_TIMEOUT_S) -> dict[str, Any]:
        """The job once it has ended."""
        return wait_for_job(lambda: self.ok(f"/api/v1/lifecycle/jobs/{job_id}")["data"], timeout_s)

    def run(self, body: Any = None, **headers: str) -> dict[str, Any]:
        """Start a run, wait for it and give back its finished job."""
        response = self.post("/api/v1/sync/runs", json=body, headers=headers or None)
        assert response.status_code == 202, response.text
        return self.wait(response.json()["job"]["id"])


def wait_for_job(read: Any, timeout_s: float = JOB_TIMEOUT_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    pause = threading.Event()
    while True:
        job = read()
        if job["state"] in ("succeeded", "failed"):
            return job
        assert time.monotonic() < deadline, f"job still {job['state']} after {timeout_s}s"
        pause.wait(0.05)


def records(path: Path) -> list[dict[str, Any]]:
    """The log lines written to a file, as JSON."""
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def problem_code(response: Response) -> str | None:
    assert response.headers["content-type"].startswith("application/problem+json"), response.text
    return response.json().get("code")


def percentile(values: Sequence[float], share: float) -> float:
    """The value below which `share` of the values lie (nearest rank)."""
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return ordered[rank - 1]


def free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def served(app: Any) -> Iterator[str]:
    """The application served by uvicorn in a thread; yields its base URL."""
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, name=f"api-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30.0
    pause = threading.Event()
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the API did not start"
        pause.wait(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=60.0)


def http_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=HEADERS, timeout=httpx.Timeout(60.0))


LEVELS = [("T1", at(5), 1.0), ("T2", at(7), 2.5)]
READINGS = [("R1", "S7", at(5), 3.0, 1), ("R2", "S7", at(9), 3.5, 2)]


def plant(
    root: Path, *names: str, levels: Sequence[Any] = LEVELS, readings: Sequence[Any] = READINGS
) -> tuple[dict[str, Any], dict[str, Folder]]:
    """Sources of the plant and their folders: `registry` (objects without a time),
    `scada` (tank levels, a state series) and `lab` (sensor readings, events)."""
    chosen = names or ("registry", "scada", "lab")
    folders = {name: Folder(root / name) for name in chosen}
    sources: dict[str, Any] = {}
    for name in chosen:
        if name == "registry":
            document = registry_source(folders[name])
        elif name == "scada":
            document = levels_source(folders[name], levels)
        elif name == "lab":
            document = readings_source(folders[name], readings, cursor="rev")
        else:
            raise ValueError(f"the plant has no source {name}")
        sources[name] = hidden(document, name)
    return sources, folders
