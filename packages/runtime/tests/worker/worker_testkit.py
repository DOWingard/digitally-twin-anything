"""The worker of the water twin, served over HTTP beside the twin's API, and what its
tests send it: requests, and code the twin brings as zip archives."""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
from api_testkit import TOKEN, WaterHistory
from dta_runtime.protocol import RESULT_KEY
from dta_runtime.settings import RuntimeSettings, WorkerSettings

AUTH = {"Authorization": f"Bearer {TOKEN}"}
ARROW = "application/vnd.apache.arrow.stream"
PROBLEM = "application/problem+json"
HERE = Path(__file__).parent
# The tests' handlers, imported by the worker's processes from this directory.
HANDLERS = {
    "function": "worker_handlers:function",
    "fit": "worker_handlers:fit",
    "backtest": "worker_absent_module:backtest",
    "predict": "worker_handlers:predict",
}
FRAME = {"target": "Tank.level", "start": "#4", "end": "head", "step": "PT1H"}
STATE = "0" * 64

# Code a twin brings: one module, one function per thing the tests make it do.
TWIN_CODE = """
import os
import resource
import signal
import sys
import time


def level(twin, artifacts, uid):
    found = twin.get(f"/objects/Tank/{uid}")
    value = found["data"]["values"]["level"]
    return {"level": value, "at": twin.at, "read": found["version"]["id"]}


def environment(twin, artifacts):
    with open("/proc/self/environ", "rb") as raw:
        initial = raw.read().decode("utf-8", "replace")
    try:
        import planted  # noqa: F401
        planted_found = True
    except ImportError:
        planted_found = False
    with open("mine.py", "w") as own:
        own.write("X = 1\\n")
    try:
        import mine  # noqa: F401
        mine_found = True
    except ImportError:
        mine_found = False
    return {
        "environ": dict(os.environ),
        "initial": initial,
        "path": sys.path,
        "cwd": os.getcwd(),
        "planted": planted_found,
        "mine": mine_found,
        "flags": {"isolated": sys.flags.isolated, "safe_path": sys.flags.safe_path},
        "limits": {
            "as": resource.getrlimit(resource.RLIMIT_AS),
            "cpu": resource.getrlimit(resource.RLIMIT_CPU),
        },
        "input": sys.stdin.read(),
    }


def noop(twin, artifacts):
    return None


def table(twin, artifacts):
    import pyarrow as pa

    return pa.table({"uid": ["node:T1"], "level": [1.0]})


def sleepy(twin, artifacts, s):
    time.sleep(s)
    return s


def hungry(twin, artifacts, size):
    hoard = bytearray(size)
    return len(hoard)


def broken(twin, artifacts):
    raise RuntimeError("valve stuck")


def die(twin, artifacts, name):
    os.kill(os.getpid(), getattr(signal, name))
    time.sleep(5)


def shout(twin, artifacts):
    print("to standard output")
    print("to standard error", file=sys.stderr)
    return "said"


def family(twin, artifacts, **params):
    return {"at": twin.at, "branch": twin.branch, "params": params}
"""


def archive(files: Mapping[str, str]) -> bytes:
    """A zip archive of the files, by name, as given."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        for name, text in files.items():
            written.writestr(zipfile.ZipInfo(name), text)
    return buffer.getvalue()


def twin_code(module: str = "twin_code") -> bytes:
    """The twin's code as an archive; a distinct one each call, so nothing is cached."""
    return archive({f"{module}.py": f"# {uuid.uuid4().hex}\n{TWIN_CODE}"})


def trace_id() -> str:
    return uuid.uuid4().hex


def function_body(sha256: str, entry: str, at: str, **more: Any) -> dict[str, Any]:
    return {
        "op": "function",
        "budget_s": 30,
        "trace_id": trace_id(),
        "artifact": sha256,
        "entry": entry,
        "at": at,
        **more,
    }


def predict_body(do: str = "noop", budget_s: float = 30, **params: Any) -> dict[str, Any]:
    return {
        "op": "predict",
        "budget_s": budget_s,
        "trace_id": trace_id(),
        "family": {"name": "persistence"},
        "params": {"do": do, **params},
        "state": STATE,
        "history": FRAME,
        "horizon": 2,
    }


def fit_body(do: str = "noop", budget_s: float = 30, **params: Any) -> dict[str, Any]:
    return {
        "op": "fit",
        "budget_s": budget_s,
        "trace_id": trace_id(),
        "family": {"name": "persistence"},
        "params": {"do": do, **params},
        "frame": FRAME,
        "horizon": 2,
    }


def table_of(response: httpx.Response) -> tuple[pa.Table, dict[str, Any]]:
    """The table of an Arrow answer and the result in its metadata."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == ARROW
    table = pa.ipc.open_stream(response.content).read_all()
    return table, json.loads(table.schema.metadata[RESULT_KEY.encode()])


def problem_of(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    assert response.headers["content-type"].startswith(PROBLEM), response.text
    document = response.json()
    assert document["code"] == code, document
    assert {"type", "title", "status", "code", "detail", "trace_id"} <= set(document)
    return document


@dataclass
class TwinApi:
    """The water twin's API, served over HTTP."""

    url: str
    settings: RuntimeSettings
    water: WaterHistory
    app: Any

    def upload(self, content: bytes) -> str:
        response = httpx.post(
            f"{self.url}/api/v1/artifacts",
            content=content,
            headers={**AUTH, "Content-Type": "application/zip"},
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["sha256"]

    def blob(self, sha256: str) -> Path:
        return self.settings.artifacts_dir / "blobs" / sha256[:2] / sha256


@dataclass
class Worker:
    """A worker served over HTTP, and its application (to look at its pools)."""

    url: str
    app: Any
    settings: WorkerSettings
    client: httpx.Client

    @property
    def pools(self) -> Any:
        return self.app.state.pools

    def run(self, body: Any, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
        return self.client.post(
            "/run", json=body, headers=dict(AUTH if headers is None else headers)
        )

    def ok(self, body: Any) -> dict[str, Any]:
        response = self.run(body)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/json"
        return response.json()


def wait_until(check: Any, timeout_s: float = 30.0, what: str = "a condition") -> None:
    deadline = time.monotonic() + timeout_s
    pause = threading.Event()
    while not check():
        assert time.monotonic() < deadline, f"{what} did not come within {timeout_s} s"
        pause.wait(0.01)
