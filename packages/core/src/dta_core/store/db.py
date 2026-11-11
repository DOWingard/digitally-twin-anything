"""Connections to the database: one pool, and the only place statements are sent from.

Pooled connections sit on the live branch. Reads name the commit they want in
every statement, so they never depend on what a connection is pointed at; a
write points its connection at the branch being written and puts it back.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pymysql
import sqlalchemy.exc
from sqlalchemy import event
from sqlalchemy.pool import QueuePool

from ..log import Log
from .errors import NotInstalled, StoreUnavailable, UnknownBranch
from .model import LIVE_BRANCH
from .settings import StoreSettings

log = Log("store.db")

ER_UNKNOWN_DATABASE = 1049
ER_ACCESS_DENIED = 1045
# Client-side codes meaning the connection itself is gone.
_CONNECTION_LOST = {0, 2002, 2003, 2006, 2013, 2014, 2055}
_RECYCLE_S = 1800


def is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, pymysql.err.InterfaceError | ConnectionError | EOFError | TimeoutError):
        return True
    if isinstance(exc, pymysql.err.OperationalError) and exc.args:
        return exc.args[0] in _CONNECTION_LOST
    return False


def error_code(exc: BaseException) -> int | None:
    if isinstance(exc, pymysql.MySQLError) and exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def error_text(exc: BaseException) -> str:
    if isinstance(exc, pymysql.MySQLError) and len(exc.args) > 1:
        return str(exc.args[1])
    return str(exc)


class Conn:
    """One connection. A failure of the connection itself is raised as
    `store.unavailable`; an error returned by the server for a statement is
    raised as it is, for the caller to interpret."""

    def __init__(self, raw: Any, slow_ms: float) -> None:
        self._raw = raw
        self._slow_ms = slow_ms
        self.broken = False
        # Set once a transaction has been opened, so the connection is rolled
        # back before it is handed to anyone else.
        self.transacted = False

    def _run(self, sql: str, params: Any, many: bool, fetch: bool) -> Any:
        started = time.perf_counter()
        try:
            with self._raw.cursor() as cursor:
                if many:
                    affected = cursor.executemany(sql, params)
                else:
                    affected = cursor.execute(sql, params)
                result = cursor.fetchall() if fetch else affected
        except Exception as exc:
            if is_connection_error(exc):
                self.broken = True
                log.error("connection to the database was lost", exc=exc, statement=sql[:200])
                raise StoreUnavailable(
                    f"the database connection was lost: {error_text(exc)}",
                    detail={"statement": sql[:200]},
                ) from exc
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > self._slow_ms:
            log.warn("slow statement", elapsed_ms=round(elapsed_ms, 1), statement=sql[:200])
        return result

    def query(self, sql: str, params: Sequence[Any] | None = None) -> tuple[tuple[Any, ...], ...]:
        return self._run(sql, params, many=False, fetch=True)

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        rows = self._run(sql, params, many=False, fetch=True)
        return rows[0][0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        return self._run(sql, params, many=False, fetch=False)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        return self._run(sql, rows, many=True, fetch=False)

    def call(self, sql: str, params: Sequence[Any] | None = None) -> tuple[Any, ...]:
        """Run a procedure and return its single result row."""
        rows = self._run(sql, params, many=False, fetch=True)
        return rows[0] if rows else ()

    def begin(self) -> None:
        self.transacted = True
        self._run("START TRANSACTION", None, many=False, fetch=False)

    def rollback(self) -> None:
        self._run("ROLLBACK", None, many=False, fetch=False)


class Database:
    def __init__(self, settings: StoreSettings) -> None:
        self._settings = settings
        self.name = settings.database
        self._pool = QueuePool(
            self._connect_live,
            pool_size=settings.pool_size,
            max_overflow=settings.pool_overflow,
            timeout=settings.pool_timeout_s,
            recycle=_RECYCLE_S,
            # Reads never open a transaction; a connection that did is rolled
            # back in `_release`, which saves a round trip on every read.
            reset_on_return=None,
        )
        event.listen(self._pool, "checkout", self._ping)

    # -- connecting ---------------------------------------------------------

    def _open(self, database: str | None) -> pymysql.connections.Connection:
        settings = self._settings
        try:
            return pymysql.connect(
                host=settings.host,
                port=settings.port,
                user=settings.user,
                password=settings.password,
                database=database,
                autocommit=True,
                charset="utf8mb4",
                connect_timeout=settings.connect_timeout_s,
                read_timeout=settings.query_timeout_s,
                write_timeout=settings.query_timeout_s,
            )
        except pymysql.MySQLError as exc:
            code = error_code(exc)
            if code == ER_UNKNOWN_DATABASE:
                log.error("database does not exist", database=self.name)
                raise NotInstalled(
                    f"database {self.name} does not exist; install the twin first",
                    detail={"database": self.name},
                ) from exc
            # The server's text is kept out of the message: on a refused login it names the user.
            log.error(
                "cannot connect to the database",
                exc=exc if code != ER_ACCESS_DENIED else None,
                host=settings.host,
                port=settings.port,
                database=self.name,
                error_code=code,
            )
            reason = "access denied" if code == ER_ACCESS_DENIED else "connection failed"
            raise StoreUnavailable(
                f"cannot connect to the database at {settings.host}:{settings.port}: {reason}",
                detail={"host": settings.host, "port": settings.port, "error_code": code},
            ) from exc

    def _connect_live(self) -> pymysql.connections.Connection:
        return self._open(f"{self.name}/{LIVE_BRANCH}")

    @staticmethod
    def _ping(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        """A connection that died while idle is replaced before it is handed out."""
        try:
            dbapi_connection.ping(reconnect=False)
        except Exception as exc:
            log.warn("pooled connection was dead, reconnecting", error=repr(exc))
            raise sqlalchemy.exc.DisconnectionError() from exc

    @contextmanager
    def connect(self, branch: str = LIVE_BRANCH) -> Iterator[Conn]:
        """A pooled connection, pointed at `branch` for the duration of the block."""
        try:
            proxy = self._pool.connect()
        except sqlalchemy.exc.TimeoutError as exc:
            log.warn(
                "no pooled connection became free in time",
                pool_size=self._settings.pool_size,
                overflow=self._settings.pool_overflow,
                waited_s=self._settings.pool_timeout_s,
            )
            raise StoreUnavailable(
                "every database connection is in use", detail={"waited_s": self._pool.timeout()}
            ) from exc
        conn = Conn(proxy, self._settings.slow_ms)
        moved = False
        try:
            if branch != LIVE_BRANCH:
                try:
                    conn.execute(f"USE `{self.name}/{branch}`")
                except pymysql.MySQLError as exc:
                    if error_code(exc) == ER_UNKNOWN_DATABASE:
                        raise UnknownBranch(
                            f"no branch named {branch}", detail={"branch": branch}
                        ) from exc
                    raise
                moved = True
            yield conn
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # Interrupted mid-statement: the protocol state is unknown.
                conn.broken = True
            raise
        finally:
            self._release(proxy, conn, moved)

    def _release(self, proxy: Any, conn: Conn, moved: bool) -> None:
        if not conn.broken and (moved or conn.transacted):
            try:
                if conn.transacted:
                    conn.rollback()
                if moved:
                    conn.execute(f"USE `{self.name}/{LIVE_BRANCH}`")
            except Exception as exc:
                log.warn("could not reset a connection, discarding it", error=repr(exc))
                conn.broken = True
        try:
            if conn.broken:
                proxy.invalidate()
            proxy.close()
        except Exception as exc:
            log.warn("closing a pooled connection failed", error=repr(exc))

    @contextmanager
    def server(self) -> Iterator[Conn]:
        """A connection to the server with no database selected, outside the pool."""
        raw = self._open(None)
        try:
            yield Conn(raw, self._settings.slow_ms)
        finally:
            try:
                raw.close()
            except Exception as exc:
                log.warn("closing a server connection failed", error=repr(exc))

    def dispose(self) -> None:
        self._pool.dispose()
