"""Read-only connector to a database that SQLAlchemy can reach.

Tables are extracted with dlt's `sql_table` (Arrow backend) used as a plain
iterator, without a dlt pipeline or destination. A dataset declared as a query
runs through SQLAlchemy and is converted to Arrow with dlt's row converter.
Filters on the cursor column and row limits are compiled into the statement,
so the database never sends rows that would be thrown away.
"""

from __future__ import annotations

import importlib.util
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pyarrow as pa
import sqlalchemy as sa
from dlt.sources.sql_database import TableLoader, sql_table
from dlt.sources.sql_database.arrow_helpers import row_tuples_to_arrow
from sqlalchemy import exc as sa_exc
from sqlalchemy import types as sa_types

from ..sources import (
    BATCH_ROWS,
    SourceAuthFailed,
    SourceConfigError,
    SourceError,
    SourceReadError,
    SourceUnavailable,
)
from . import options as opt
from .base import BaseConnector, Piece, ReadRequest, Redactor, _elapsed_ms
from .normalize import as_utc_datetime, local_naive, normalize_table

OPTION_KEYS = ("schema", "timezone", "datasets", "connect_timeout_s")
DATASET_KEYS = ("table", "query")
DEFAULT_CONNECT_TIMEOUT_S = 10.0

_QUERY_ALIAS = "dta_query"
_LEADING_KEYWORD = re.compile(r"^\s*(select|with|values)\b", re.I)
# Inside one statement that starts with SELECT or WITH, data changes come from
# data-modifying CTEs, SELECT INTO and row locks; DDL words are refused as well.
_WRITING_KEYWORD = re.compile(
    r"\b(insert|update|delete|merge|into|create|drop|alter|truncate|grant|revoke|copy"
    r"|for\s+(?:no\s+key\s+|key\s+)?share)\b",
    re.I,
)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")

_AUTH_TEXT = re.compile(
    r"authentication failed|access denied|password|permission denied|not authori[sz]ed", re.I
)


@dataclass(frozen=True)
class SqlDataset:
    name: str
    table: str | None = None
    query: str | None = None


@dataclass(frozen=True)
class SqlOptions:
    schema: str | None
    timezone: str
    datasets: dict[str, SqlDataset] | None
    connect_timeout_s: float


def parse_options(raw: Mapping[str, Any]) -> SqlOptions:
    opt.mapping(raw, "options")
    opt.only_keys(raw, OPTION_KEYS, "")
    schema = opt.text(raw["schema"], "schema") if raw.get("schema") is not None else None
    timezone = opt.timezone(raw.get("timezone", "UTC"), "timezone")
    timeout = DEFAULT_CONNECT_TIMEOUT_S
    if raw.get("connect_timeout_s") is not None:
        timeout = opt.number(raw["connect_timeout_s"], "connect_timeout_s")
    datasets: dict[str, SqlDataset] | None = None
    if raw.get("datasets") is not None:
        declared = opt.mapping(raw["datasets"], "datasets")
        if not declared:
            raise opt.invalid("datasets", "must declare at least one dataset")
        datasets = {}
        for name, entry in declared.items():
            path = f"datasets.{name}"
            entry = opt.mapping(entry, path)
            opt.only_keys(entry, DATASET_KEYS, path)
            given = [key for key in DATASET_KEYS if entry.get(key) is not None]
            if len(given) != 1:
                raise opt.invalid(path, "needs exactly one of table or query")
            if given[0] == "table":
                datasets[name] = SqlDataset(name, table=opt.text(entry["table"], f"{path}.table"))
            else:
                query = check_query(opt.text(entry["query"], f"{path}.query"), f"{path}.query")
                datasets[name] = SqlDataset(name, query=query)
    return SqlOptions(schema, timezone, datasets, timeout)


def check_query(query: str, path: str) -> str:
    """One SELECT statement, without a trailing semicolon; anything else is refused."""
    stripped = _SQL_COMMENT.sub(" ", query).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    bare = _SQL_QUOTED.sub("''", stripped)
    if ";" in bare:
        raise opt.invalid(path, "must be a single statement")
    if not _LEADING_KEYWORD.match(bare):
        raise opt.invalid(path, "must be a SELECT statement")
    if match := _WRITING_KEYWORD.search(bare):
        raise opt.invalid(path, f"must only read; found {match.group(1).upper()}")
    return stripped


def _float_hint(sql_type: sa_types.TypeEngine) -> sa_types.TypeEngine:
    """Give floating-point columns dlt's `double` hint.

    SQLAlchemy 2.1 no longer derives `Float` from `Numeric`, which is what dlt
    1.32 looks for, so without this a float column is typed from its values and
    a chunk that is all null comes back as a column of nulls.
    """
    if isinstance(sql_type, sa_types.Float):
        return sa_types.Numeric(asdecimal=False)
    return sql_type


def _tracked_loader(opened: list[sa.Connection]) -> type[TableLoader]:
    """dlt's table loader, recording the connection it reads on.

    The iterator of a dlt resource offers no way to close it, so a read that
    stops early would keep its server-side cursor open, and with it a
    transaction and its locks, until garbage collection. The connector closes
    the recorded connection itself.
    """

    class TrackedLoader(TableLoader):
        def _load_rows(self, query: Any, backend_kwargs: dict[str, Any]) -> Iterator[Any]:
            with self.engine.connect() as connection:
                opened.append(connection)
                result = connection.execution_options(yield_per=self.chunk_size).execute(query)
                try:
                    yield from self._convert_result(result, backend_kwargs)
                finally:
                    result.close()

    return TrackedLoader


class SqlConnector(BaseConnector):
    kind = "sql"

    def __init__(
        self,
        source_id: str,
        location: str,
        redactor: Redactor,
        settings: SqlOptions,
    ) -> None:
        super().__init__(source_id, location, redactor)
        self._engine_or_none: sa.Engine | None = None
        self._options = settings
        self._metadata = sa.MetaData(schema=settings.schema)
        self._tables: list[str] | None = None

    @property
    def engine(self) -> sa.Engine:
        """The read-only engine every statement of this connector goes through."""
        if self._engine_or_none is None:
            raise self._error(SourceError, "the connector is not connected")
        return self._engine_or_none

    @property
    def _engine(self) -> sa.Engine:
        return self.engine

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    # -- connecting ----------------------------------------------------------

    def _connect(self, resolved: str) -> None:
        try:
            url = sa.make_url(resolved)
        except sa_exc.ArgumentError:
            raise self._error(SourceConfigError, "the location is not a SQLAlchemy URL") from None
        url = self._with_default_driver(url)
        connect_args, url = self._read_only_arguments(url)
        try:
            engine = sa.create_engine(
                url,
                connect_args=connect_args,
                pool_pre_ping=True,
                # Bound values stay out of error messages.
                hide_parameters=True,
            )
        except (sa_exc.NoSuchModuleError, ImportError) as exc:
            raise self._error(
                SourceConfigError,
                f"no driver for SQL dialect {url.drivername!r} is installed",
                cause=exc,
                dialect=url.drivername,
            ) from None
        except sa_exc.ArgumentError as exc:
            raise self._error(
                SourceConfigError, "the location cannot be used as a SQLAlchemy URL", cause=exc
            ) from None
        started = time.perf_counter()
        try:
            with engine.connect():
                pass
        except Exception as exc:
            engine.dispose()
            raise self._translate(exc, None, f"connecting to source {self.source_id!r}") from None
        self._engine_or_none = engine
        self._log.info(
            "connector opened",
            source=self.source_id,
            kind=self.kind,
            location=self.location,
            dialect=engine.dialect.name,
            schema=self._options.schema,
            datasets="declared" if self._options.datasets is not None else "tables",
            elapsed_ms=_elapsed_ms(started),
        )

    def _with_default_driver(self, url: sa.URL) -> sa.URL:
        """`postgresql://` means psycopg 3 to SQLAlchemy 2.1; fall back to an installed psycopg2."""
        if url.drivername != "postgresql":
            return url
        if importlib.util.find_spec("psycopg") is None and importlib.util.find_spec("psycopg2"):
            self._log.debug("using psycopg2 for a postgresql URL without a driver")
            return url.set(drivername="postgresql+psycopg2")
        return url

    def _read_only_arguments(self, url: sa.URL) -> tuple[dict[str, Any], sa.URL]:
        """Connection arguments that make the database itself refuse writes, where it can."""
        backend = url.get_backend_name()
        driver = url.get_driver_name()
        if backend == "postgresql" and driver in {"psycopg2", "psycopg"}:
            # Every transaction starts read-only, and the session's zone is the one
            # zone-less timestamps are read in, so comparisons with them agree.
            given = url.query.get("options", "")
            if isinstance(given, tuple):
                given = " ".join(given)
            flags = f"-c default_transaction_read_only=on -c TimeZone={self._options.timezone}"
            connect_args: dict[str, Any] = {
                "options": f"{given} {flags}".strip(),
                "connect_timeout": max(1, round(self._options.connect_timeout_s)),
                "application_name": "dta-connect",
            }
            return connect_args, url.difference_update_query(["options"])
        if backend == "sqlite":
            database = url.database or ""
            if database and database != ":memory:" and not database.startswith("file:"):
                # Read-only mode also stops SQLite creating an empty file for a wrong path.
                query = {**url.query, "mode": "ro", "uri": "true"}
                return {}, url.set(database=f"file:{database}", query=query)
            return {}, url
        self._log.warn(
            "this database cannot be opened read-only by the connector; only SELECT "
            "statements are sent",
            source=self.source_id,
            dialect=backend,
        )
        return {}, url

    # -- datasets ------------------------------------------------------------

    def _list_datasets(self) -> list[str]:
        if self._options.datasets is not None:
            return list(self._options.datasets)
        started = time.perf_counter()
        tables = sa.inspect(self._engine).get_table_names(schema=self._options.schema)
        self._tables = list(tables)
        self._log.debug(
            "tables listed",
            source=self.source_id,
            schema=self._options.schema,
            count=len(tables),
            elapsed_ms=_elapsed_ms(started),
        )
        return self._tables

    def _has_dataset(self, name: str) -> bool:
        if self._options.datasets is not None:
            return name in self._options.datasets
        # A table created since the listing is found by listing again.
        if self._tables is not None and name in self._tables:
            return True
        return name in self.datasets()

    def _spec(self, name: str) -> SqlDataset:
        if self._options.datasets is not None:
            return self._options.datasets[name]
        return SqlDataset(name, table=name)

    # -- reading -------------------------------------------------------------

    def _pieces(self, request: ReadRequest) -> Iterator[Piece]:
        spec = self._spec(request.dataset)
        if spec.table is not None:
            tables = self._table_tables(spec, request)
        else:
            tables = self._query_tables(spec, request)
        # SQLite keeps timestamps as text, and the columns of a query carry no
        # declared type to convert them by, so they are recognised like CSV text.
        detect = spec.query is not None and self.dialect == "sqlite"
        for table in tables:
            yield Piece(
                normalize_table(table, timezone=self._options.timezone, detect_iso_text=detect)
            )

    def _table_tables(self, spec: SqlDataset, request: ReadRequest) -> Iterator[pa.Table]:
        assert spec.table is not None
        opened: list[sa.Connection] = []

        def adapt(query: Any, table: sa.Table) -> Any:
            names = request.columns or [column.name for column in table.columns]
            statement = sa.select(*[table.c[name] for name in names])
            if request.cursor_column is not None and request.since is not None:
                cursor = table.c[request.cursor_column]
                statement = statement.where(
                    cursor > self._since_value(request.since, cursor.type, request.dataset)
                )
            if request.limit is not None:
                statement = statement.limit(request.limit)
            return statement

        started = time.perf_counter()
        try:
            resource = sql_table(
                credentials=self._engine,
                table=spec.table,
                schema=self._options.schema,
                metadata=self._metadata,
                backend="pyarrow",
                chunk_size=BATCH_ROWS,
                reflection_level="full",
                type_adapter_callback=_float_hint,
                query_adapter_callback=adapt,
                table_loader_class=_tracked_loader(opened),
            )
        except sa_exc.NoSuchTableError:
            raise self._error(
                SourceConfigError,
                f"table {spec.table!r} of dataset {spec.name!r} does not exist",
                dataset=spec.name,
                table=spec.table,
                schema=self._options.schema,
            ) from None
        key = f"{self._options.schema}.{spec.table}" if self._options.schema else spec.table
        table = self._metadata.tables[key]
        self._log.debug(
            "table reflected",
            source=self.source_id,
            dataset=spec.name,
            columns=len(table.columns),
            elapsed_ms=_elapsed_ms(started),
        )
        self._require_columns(spec.name, [column.name for column in table.columns], request)
        yielded = False
        try:
            for item in resource:
                if isinstance(item, pa.Table):
                    yielded = True
                    yield item
                else:
                    self._log.debug(
                        "skipped a non-table item from dlt",
                        source=self.source_id,
                        dataset=spec.name,
                        item_type=type(item).__name__,
                    )
        finally:
            for connection in opened:
                connection.close()
        if not yielded:
            hints = resource.compute_table_schema()["columns"]
            names = request.columns or [column.name for column in table.columns]
            yield row_tuples_to_arrow([], columns={n: hints[n] for n in names}, tz="UTC")

    def _query_tables(self, spec: SqlDataset, request: ReadRequest) -> Iterator[pa.Table]:
        assert spec.query is not None
        quote = self._engine.dialect.identifier_preparer.quote
        subquery = sa.text(spec.query).columns().subquery(_QUERY_ALIAS)

        def column(name: str) -> Any:
            return sa.literal_column(f"{_QUERY_ALIAS}.{quote(name)}")

        with self._engine.connect() as connection:
            if request.columns is not None or request.cursor_column is not None:
                probe = sa.select(sa.literal_column("*")).select_from(subquery).limit(0)
                available = list(connection.execute(probe).keys())
                self._require_columns(spec.name, available, request)
            if request.columns is not None:
                statement = sa.select(*[column(name).label(name) for name in request.columns])
            else:
                statement = sa.select(sa.literal_column("*"))
            statement = statement.select_from(subquery)
            if request.cursor_column is not None and request.since is not None:
                value = self._since_value(request.since, None, request.dataset)
                statement = statement.where(column(request.cursor_column) > value)
            if request.limit is not None:
                statement = statement.limit(request.limit)
            result = connection.execution_options(
                stream_results=True, yield_per=BATCH_ROWS
            ).execute(statement)
            try:
                keys = list(result.keys())
                if len(set(keys)) != len(keys):
                    raise self._error(
                        SourceConfigError,
                        f"the query of dataset {spec.name!r} returns duplicate column names",
                        dataset=spec.name,
                        columns=keys,
                    )
                hints = {key: {"name": key} for key in keys}
                yielded = False
                for partition in result.partitions(BATCH_ROWS):
                    yielded = True
                    rows = [tuple(row) for row in partition]
                    yield row_tuples_to_arrow(rows, columns=hints, tz="UTC")
                if not yielded:
                    # Without rows there is nothing to take a type from.
                    yield pa.table({key: pa.array([], pa.null()) for key in keys})
            finally:
                result.close()

    def _require_columns(self, dataset: str, available: list[str], request: ReadRequest) -> None:
        wanted = list(request.columns or ())
        if request.cursor_column is not None:
            wanted.append(request.cursor_column)
        missing = [name for name in dict.fromkeys(wanted) if name not in available]
        if missing:
            raise self._error(
                SourceConfigError,
                f"dataset {dataset!r} has no column {', '.join(missing)}",
                dataset=dataset,
                missing_columns=missing,
            )

    def _since_value(
        self, since: Any, column_type: sa_types.TypeEngine | None, dataset: str
    ) -> Any:
        """`since` as a bound parameter the database compares correctly with the cursor.

        A zone-less timestamp column holds wall-clock time in `options.timezone`.
        PostgreSQL sessions run in that zone, so an aware value compares
        correctly with either kind of column; elsewhere a column of unknown
        type (a query's) gets the wall-clock value.
        """
        is_timestamp = isinstance(column_type, sa_types.DateTime)
        if not is_timestamp and not (column_type is None and isinstance(since, datetime)):
            return since
        try:
            moment = as_utc_datetime(since)
        except ValueError as exc:
            raise self._error(
                SourceConfigError,
                f"since cannot be compared with a timestamp cursor: {exc}",
                dataset=dataset,
            ) from None
        aware = bool(column_type.timezone) if is_timestamp else self.dialect == "postgresql"
        if aware:
            return sa.bindparam("dta_since", moment, type_=sa_types.DateTime(timezone=True))
        local = local_naive(moment, self._options.timezone)
        return sa.bindparam("dta_since", local, type_=sa_types.DateTime(timezone=False))

    # -- errors and lifecycle ------------------------------------------------

    def _classify(self, exc: BaseException) -> type[SourceError]:
        return classify_error(exc)

    def _release(self) -> None:
        if self._engine_or_none is not None:
            self._engine_or_none.dispose()


def classify_error(exc: BaseException) -> type[SourceError]:
    """The `SourceError` class for an error raised by SQLAlchemy, a driver or dlt."""
    for error in _chain(exc):
        if isinstance(error, sa_exc.NoSuchTableError | sa_exc.NoSuchModuleError):
            return SourceConfigError
        if isinstance(error, sa_exc.TimeoutError):
            return SourceUnavailable
        if isinstance(error, sa_exc.DBAPIError):
            return _classify_dbapi(error)
    for error in _chain(exc):
        if isinstance(error, ConnectionError | TimeoutError):
            return SourceUnavailable
    return SourceReadError


def _classify_dbapi(error: sa_exc.DBAPIError) -> type[SourceError]:
    original = error.orig
    text = str(original)
    sqlite_name = getattr(original, "sqlite_errorname", None)
    if sqlite_name is not None:
        if sqlite_name in {"SQLITE_CANTOPEN", "SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_IOERR"}:
            return SourceUnavailable
        if sqlite_name in {"SQLITE_AUTH", "SQLITE_PERM"}:
            return SourceAuthFailed
        if sqlite_name == "SQLITE_ERROR":
            # no such table or column, a syntax error: the statement as configured is wrong
            return SourceConfigError
        return SourceReadError
    state = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
    if (state and state.startswith("28")) or state == "42501":
        return SourceAuthFailed
    if state and state[:2] in {"08", "53", "57"}:
        return SourceUnavailable
    if state and state.startswith("42"):
        return SourceConfigError
    if state is None and isinstance(error, sa_exc.OperationalError):
        # Drivers report a failed login and a failed connection with no state code.
        if _AUTH_TEXT.search(text):
            return SourceAuthFailed
        return SourceUnavailable
    if error.connection_invalidated:
        return SourceUnavailable
    return SourceReadError


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def open_sql(
    source_id: str,
    location: str,
    resolved: str,
    settings: SqlOptions,
    redactor: Redactor,
) -> SqlConnector:
    """Create the engine, prove the database answers, and return the connector."""
    connector = SqlConnector(source_id, location, redactor, settings)
    connector._connect(resolved)
    return connector
