"""A real JSON-over-HTTP interface for the HTTP connector tests: the standard
library's server on a free loopback port, in a thread of the test process, with
routes that page, filter, fail and redirect the way real interfaces do."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from dta_core.connect import open_connector
from dta_core.manifest.models import Source
from dta_core.sources import Connector

T0 = datetime(2026, 1, 19, 6, 0, tzinfo=UTC)
LOCATION_VAR = "DTA_TEST_HTTP_URL"
KEY_VAR = "DTA_TEST_HTTP_KEY"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def readings(count: int, *, sensors: int = 3, step_s: int = 900) -> list[dict[str, Any]]:
    """Rows like a telemetry interface's: ordered by time, then by sensor."""
    return [
        {
            "sensor_id": f"S-{index % sensors}",
            "timestamp": iso(T0 + timedelta(seconds=step_s * (index // sensors))),
            "value": round(index * 0.25 + 1.0, 2),
            "quality": "good",
        }
        for index in range(count)
    ]


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    at: float

    def arg(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None


@dataclass
class Reply:
    body: Any = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


Route = Callable[[Call], Reply]


class Api:
    """A JSON interface on 127.0.0.1; every request it receives is kept in `calls`."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.calls: list[Call] = []
        self._lock = threading.Lock()
        api = self

        class Handler(_Handler):
            owner = api

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # A short poll keeps `shutdown` quick at the end of every test.
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def route(self, path: str, route: Route | Reply) -> None:
        self.routes[path] = route if callable(route) else (lambda call: route)

    def calls_to(self, path: str) -> list[Call]:
        with self._lock:
            return [call for call in self.calls if call.path == path]

    def record(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    owner: Api

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        call = Call(
            "GET",
            url.path,
            parse_qs(url.query, keep_blank_values=True),
            dict(self.headers.items()),
            time.monotonic(),
        )
        self.owner.record(call)
        route = self.owner.routes.get(url.path)
        reply = route(call) if route else Reply({"error": {"code": "not_found"}}, 404)
        if reply.delay_s:
            time.sleep(reply.delay_s)
        body = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
        try:
            self.send_response(reply.status)
            headers = {"Content-Type": "application/json", **reply.headers}
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client stopped waiting

    def _refuse(self) -> None:
        url = urlsplit(self.path)
        self.owner.record(Call(self.command, url.path, {}, dict(self.headers.items()), 0.0))
        self.send_error(405)

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815


# -- routes -----------------------------------------------------------------------


def after(rows: list[dict[str, Any]], call: Call, param: str, fmt: str) -> list[dict[str, Any]]:
    """Rows at or after the instant in `param`: inclusive, as many interfaces are."""
    value = call.arg(param)
    if value is None:
        return rows
    if fmt == "iso":
        since = datetime.fromisoformat(value)
    elif fmt == "epoch_s":
        since = datetime.fromtimestamp(int(value), tz=UTC)
    else:
        since = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    return [row for row in rows if datetime.fromisoformat(row["timestamp"]) >= since]


def cursor_route(
    rows: list[dict[str, Any]], size: int, *, since: tuple[str, str] | None = None
) -> Route:
    def route(call: Call) -> Reply:
        selected = after(rows, call, *since) if since else rows
        start = int(call.arg("cursor") or 0)
        end = start + size
        cursor = str(end) if end < len(selected) else None
        return Reply({"data": selected[start:end], "next_cursor": cursor})

    return route


def page_route(rows: list[dict[str, Any]], size: int, *, total: bool = False) -> Route:
    def route(call: Call) -> Reply:
        page = int(call.arg("p") or 1)
        body: dict[str, Any] = {"items": rows[(page - 1) * size : page * size]}
        if total:
            body["pages"] = -(-len(rows) // size)
        return Reply(body)

    return route


def offset_route(rows: list[dict[str, Any]], *, total: bool = False) -> Route:
    def route(call: Call) -> Reply:
        offset, limit = int(call.arg("skip") or 0), int(call.arg("take") or 10)
        body: dict[str, Any] = {"results": rows[offset : offset + limit]}
        if total:
            body["count"] = len(rows)
        return Reply(body)

    return route


def link_body_route(path: str, rows: list[dict[str, Any]], size: int, base: str = "") -> Route:
    """Next-page links in the body; relative, or absolute when `base` is given."""

    def route(call: Call) -> Reply:
        start = int(call.arg("from") or 0)
        end = start + size
        link = f"{base}{path}?from={end}" if end < len(rows) else None
        return Reply({"data": rows[start:end], "links": {"next": link}})

    return route


def link_header_route(path: str, rows: list[dict[str, Any]], size: int) -> Route:
    def route(call: Call) -> Reply:
        start = int(call.arg("from") or 0)
        end = start + size
        headers = {"Link": f'<{path}?from={end}>; rel="next"'} if end < len(rows) else {}
        return Reply(rows[start:end], headers=headers)

    return route


def sequence(*replies: Reply) -> Route:
    """Replies in turn; the last one repeats."""
    remaining = list(replies)

    def route(call: Call) -> Reply:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return route


# -- connectors -------------------------------------------------------------------


def source(datasets: dict[str, Any], **options: Any) -> Source:
    """A manifest entry whose address and key are placeholders, as a real one's are."""
    return Source(
        connector="http",
        location="${" + LOCATION_VAR + "}",
        options={"datasets": datasets, **options},
    )


def connect(
    target: Api | str, datasets: dict[str, Any], *, key: str = "k-0", **options: Any
) -> Connector:
    """Open the source through the factory; `target` is an interface or a bare address."""
    url = target if isinstance(target, str) else target.url
    env = {LOCATION_VAR: url, KEY_VAR: key}
    return open_connector("plant", source(datasets, **options), env)


def table_rows(connector: Connector, dataset: str, **kwargs: Any) -> list[dict[str, Any]]:
    return [row for table in connector.read(dataset, **kwargs) for row in table.to_pylist()]
