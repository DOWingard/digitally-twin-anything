"""What the interface's tests run against: the water twin's history with a few more
versions, the API and the interface served on loopback ports, and observers that
record what the API received and what the interface sent to the browser.

The observers wrap the real applications; nothing is replaced.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

import uvicorn
from api_testkit import HEAD_SEQ, WaterHistory, node, time_of
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance
from waitress.server import create_server

PORTS = range(18720, 18740)
# Fixed before measuring; the measured times are recorded by the test and printed at the end.
FIRST_RENDER_LIMIT_S = 5.0
FIRST_RENDER_S: dict[str, float] = {}

# Versions written on top of the water history; readings enough for several sheet blocks
# and for a neighbourhood larger than the graph's default node limit.
READINGS = 450
READINGS_SENSOR = "sensor:S2"
RUNS_SEQ = (HEAD_SEQ + 1, HEAD_SEQ + 2)
FIRST_RUN = "twin_sync_run:ui-1"
SECOND_RUN = "twin_sync_run:ui-2"


def _prov(t: Any, source: str = "scada") -> Provenance:
    return Provenance(source=source, kind=FactKind.observed, t_valid=t)


def extend_history(history: WaterHistory) -> None:
    """Two sync versions after the water history's head: many readings and two sync runs."""
    store = history.twin.store
    first_t = time_of(RUNS_SEQ[0])
    readings = [
        {
            "uid": f"reading:S2-{number:04d}",
            "sensor": READINGS_SENSOR,
            "observed_at": first_t - timedelta(minutes=READINGS - number),
            "value": float(number % 97),
            "quality": "good",
            "sequence": number,
        }
        for number in range(READINGS)
    ]
    run = {
        "uid": FIRST_RUN,
        "kind": "backfill",
        "status": "succeeded",
        "started_at": first_t - timedelta(minutes=5),
        "finished_at": first_t,
        "rows_read": READINGS,
        "rows_written": READINGS,
        "rows_rejected": 0,
        "versions_written": 1,
        "sources": ["scada"],
    }
    version = store.commit(
        ChangeSet(upserts={"Reading": readings, "TwinSyncRun": [run]}, provenance=_prov(first_t)),
        CommitMeta(cause="sync", actor="sync", t=first_t, run=FIRST_RUN, note="readings loaded"),
    )
    history.versions[version.seq] = version
    second_t = time_of(RUNS_SEQ[1])
    failed = {
        "uid": SECOND_RUN,
        "kind": "incremental",
        "status": "failed",
        "started_at": second_t - timedelta(minutes=1),
        "finished_at": second_t,
        "rows_read": 0,
        "rows_written": 0,
        "rows_rejected": 0,
        "versions_written": 1,
        "error": "the source did not answer",
    }
    version = store.commit(
        ChangeSet(
            upserts={
                "TwinSyncRun": [failed],
                "Junction": [{"uid": node("J1"), "demand": 42.0}],
            },
            provenance=_prov(second_t),
        ),
        CommitMeta(cause="sync", actor="sync", t=second_t, run=SECOND_RUN),
    )
    history.versions[version.seq] = version


def write_version(history: WaterHistory, demand: float) -> int:
    """One more sync version on the live branch; returns its seq."""
    store = history.twin.store
    head = store.head()
    t = head.t + timedelta(hours=1)
    version = store.commit(
        ChangeSet(
            upserts={"Junction": [{"uid": node("J2"), "demand": demand}]}, provenance=_prov(t)
        ),
        CommitMeta(cause="sync", actor="scada", t=t),
    )
    history.versions[version.seq] = version
    return version.seq


# -- ports and servers -----------------------------------------------------------

_taken: set[int] = set()
_ports_lock = threading.Lock()


def free_port() -> int:
    """A loopback port of the range given to these tests that nothing listens on."""
    with _ports_lock:
        for port in PORTS:
            if port in _taken:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            _taken.add(port)
            return port
    raise RuntimeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


def release_port(port: int) -> None:
    with _ports_lock:
        _taken.discard(port)


def wait_until(check: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{what} within {timeout} s")


@dataclass
class SeenRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def values(self, name: str) -> list[str]:
        return [value for key, value in self.query if key == name]

    def value(self, name: str) -> str | None:
        found = self.values(name)
        return found[0] if found else None


@dataclass
class RequestLog:
    """Requests an ASGI application received, in order."""

    app: Any
    seen: list[SeenRequest] = field(default_factory=list)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        query = tuple(parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True))
        seen = SeenRequest(scope["method"], scope["path"], query)
        self.seen.append(seen)

        async def receive_and_keep() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                seen.body += message.get("body", b"")
            return message

        await self.app(scope, receive_and_keep, send)

    def mark(self) -> int:
        return len(self.seen)

    def since(self, mark: int, path: str | None = None) -> list[SeenRequest]:
        found = self.seen[mark:]
        return [request for request in found if path is None or request.path == path]


@dataclass
class ResponseLog:
    """Every response body a WSGI application sent, with the path it answered."""

    app: Any
    sent: list[tuple[str, bytes]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        captured: dict[str, Any] = {}

        def record_start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            captured["headers"] = headers
            return start_response(status, headers, exc_info)

        result = self.app(environ, record_start)
        try:
            body = b"".join(result)
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        header_text = "".join(f"{name}: {value}\n" for name, value in captured.get("headers", []))
        with self._lock:
            self.sent.append((environ.get("PATH_INFO", ""), header_text.encode() + body))
        return [body]


class ApiServer:
    """The API application served by uvicorn in a thread."""

    def __init__(self, app: Any, port: int) -> None:
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
        )
        self.thread = threading.Thread(target=self.server.run, name=f"api-{port}", daemon=True)

    def __enter__(self) -> ApiServer:
        self.thread.start()
        wait_until(lambda: self.server.started, 30.0, "the API did not start")
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15.0)
        release_port(self.port)


class UiServer:
    """A WSGI application served by waitress in a thread, as the runtime serves the interface."""

    def __init__(self, app: Any, port: int) -> None:
        self.port = port
        self.server = create_server(app, host="127.0.0.1", port=port, threads=8)
        self.thread = threading.Thread(target=self.server.run, name=f"ui-{port}", daemon=True)

    def __enter__(self) -> UiServer:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.close()
        self.thread.join(timeout=5.0)
        release_port(self.port)


def lines(path: Any) -> Iterator[str]:
    with open(path, encoding="utf-8") as handle:
        yield from handle
