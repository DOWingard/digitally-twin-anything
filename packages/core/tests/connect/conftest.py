"""Fixtures of the connector tests: real SQLite files, the test PostgreSQL server
named by the `DTA_CONNECT_PG_*` environment variables (tests that need it are
skipped without them), a real HTTP server over a temporary directory, and a
capture of every log record written while a test runs."""

from __future__ import annotations

import functools
import http.server
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).parent))

from dta_connect_testkit import (  # noqa: E402
    PG_ENV,
    CapturedLogs,
    PostgresBackend,
    SqlBackend,
    SqliteBackend,
    _Collect,
)


@pytest.fixture
def captured_logs() -> Iterator[CapturedLogs]:
    handler = _Collect()
    loggers = [logging.getLogger(), logging.getLogger("dta"), logging.getLogger("dlt")]
    dta = logging.getLogger("dta")
    level = dta.level
    dta.setLevel(logging.DEBUG)
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield CapturedLogs(handler)
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
        dta.setLevel(level)


@pytest.fixture(scope="session")
def pg_env() -> dict[str, str]:
    missing = [name for name in PG_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "needs the test PostgreSQL server: set "
            + ", ".join(missing)
            + " (see packages/core/tests/connect/compose.yaml)"
        )
    return {name: os.environ[name] for name in PG_ENV}


@pytest.fixture(scope="session")
def pg_admin(pg_env: dict[str, str]) -> Iterator[sa.Engine]:
    url = sa.URL.create(
        "postgresql+psycopg2",
        username=pg_env["DTA_CONNECT_PG_USER"],
        password=pg_env["DTA_CONNECT_PG_PASSWORD"],
        host=pg_env["DTA_CONNECT_PG_HOST"],
        port=int(pg_env["DTA_CONNECT_PG_PORT"]),
        database=pg_env["DTA_CONNECT_PG_DATABASE"],
    )
    engine = sa.create_engine(url, connect_args={"connect_timeout": 20})
    try:
        with engine.connect():
            pass
    except sa.exc.OperationalError as exc:
        pytest.fail(f"the test PostgreSQL server named by DTA_CONNECT_PG_* is not reachable: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def postgres(pg_admin: sa.Engine, pg_env: dict[str, str]) -> Iterator[PostgresBackend]:
    backend = PostgresBackend(pg_admin, pg_env)
    yield backend
    backend.drop()


@pytest.fixture
def sqlite(tmp_path: Path) -> Iterator[SqliteBackend]:
    backend = SqliteBackend(tmp_path)
    yield backend
    backend.drop()


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest) -> SqlBackend:
    return request.getfixturevalue(request.param)


# -- HTTP ---------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None


@pytest.fixture
def http_server() -> Iterator[Callable[[Path], str]]:
    """Serve a directory over HTTP on a free loopback port; returns its base URL."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def serve(directory: Path) -> str:
        handler = functools.partial(_QuietHandler, directory=str(directory))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()
