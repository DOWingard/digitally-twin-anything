"""Helpers shared by the connector tests: log capture and the SQL backends the
same tests run against."""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dta_core.connect import open_connector
from dta_core.manifest.models import Source
from dta_core.sources import Connector

PG_ENV = (
    "DTA_CONNECT_PG_HOST",
    "DTA_CONNECT_PG_PORT",
    "DTA_CONNECT_PG_USER",
    "DTA_CONNECT_PG_PASSWORD",
    "DTA_CONNECT_PG_DATABASE",
)
PG_LOCATION = (
    "postgresql+psycopg2://${DTA_CONNECT_PG_USER}:${DTA_CONNECT_PG_PASSWORD}"
    "@${DTA_CONNECT_PG_HOST}:${DTA_CONNECT_PG_PORT}/${DTA_CONNECT_PG_DATABASE}"
)
SQLITE_LOCATION = "sqlite:///${DTA_TEST_SQLITE_DIR}/source.db"


# -- logs ---------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class CapturedLogs:
    """Every record logged during a test, rendered with its data and full exception chain."""

    def __init__(self, handler: _Collect) -> None:
        self._handler = handler

    @property
    def records(self) -> list[logging.LogRecord]:
        return self._handler.records

    def text(self) -> str:
        lines = []
        for record in self._handler.records:
            parts = [record.name, record.levelname, record.getMessage()]
            data = getattr(record, "data", None)
            if data:
                parts.append(json.dumps(data, default=str))
            if record.exc_info and record.exc_info[1] is not None:
                parts.append("".join(traceback.format_exception(*record.exc_info)))
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def messages(self, level: str | None = None) -> list[str]:
        return [
            r.getMessage() for r in self.records if level is None or r.levelname == level.upper()
        ]


# -- SQL backends ----------------------------------------------------------------


class SqlBackend:
    """One database to run the same SQL tests against."""

    name: str
    admin: sa.Engine
    schema: str | None
    env: dict[str, str]
    location: str

    def metadata(self) -> sa.MetaData:
        return sa.MetaData(schema=self.schema)

    def create(self, table: sa.Table, rows: list[dict[str, Any]] | None = None) -> None:
        table.create(self.admin)
        if rows:
            with self.admin.begin() as connection:
                connection.execute(table.insert(), rows)

    def execute(self, *statements: str) -> None:
        with self.admin.begin() as connection:
            for statement in statements:
                connection.execute(sa.text(statement))

    def qualified(self, table: str) -> str:
        return f'"{self.schema}"."{table}"' if self.schema else f'"{table}"'

    def source(self, **options: Any) -> Source:
        merged: dict[str, Any] = {"schema": self.schema} if self.schema else {}
        merged.update(options)
        return Source(connector="sql", location=self.location, options=merged)

    def open(self, **options: Any) -> Connector:
        return open_connector(f"{self.name}-source", self.source(**options), self.env)


class SqliteBackend(SqlBackend):
    name = "sqlite"
    schema = None
    location = SQLITE_LOCATION

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.env = {"DTA_TEST_SQLITE_DIR": str(directory)}
        self.admin = sa.create_engine(f"sqlite:///{directory / 'source.db'}")

    def drop(self) -> None:
        self.admin.dispose()


class PostgresBackend(SqlBackend):
    name = "postgres"
    location = PG_LOCATION

    def __init__(self, admin: sa.Engine, env: dict[str, str]) -> None:
        self.admin = admin
        self.env = dict(env)
        # Each test works in a schema of its own, dropped afterwards.
        self.schema = f"t_{uuid.uuid4().hex[:10]}"
        self.execute(f'CREATE SCHEMA "{self.schema}"')

    def drop(self) -> None:
        # A connector that leaked an open transaction holds locks on the schema;
        # the drop then fails after a while instead of waiting forever.
        self.execute(
            "SET LOCAL lock_timeout = '60s'", f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'
        )

    def open_sessions(self) -> list[str]:
        """States of the connector's sessions on the server."""
        with self.admin.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT state FROM pg_stat_activity "
                    "WHERE application_name = 'dta-connect' AND pid <> pg_backend_pid()"
                )
            )
            return [row[0] for row in rows]
