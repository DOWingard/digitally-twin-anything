"""Maps the datasets of a run to ontology objects and works out what is new.

Everything happens in one in-memory DuckDB session that can reach nothing
outside the process. Each mapping is one query; comparing with what is stored
and reducing a time series to one value per interval are queries too. Rows
become Python values only when a version's change set is handed to the store.

A mapping is one of three kinds. A static mapping has no valid time: its rows
are compared with the stored objects and written where they differ. A state
series has one and feeds properties that hold the latest value: its rows are
reduced to one value per interval. A mapping of events has one and feeds a
type that records things that happened: each row is an object of its own,
found again by its identity when the source corrects it.

The session works in UTC whatever the host's time zone is, so that a key or a
date derived from a timestamp is the same wherever the twin runs.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import UID_FIELD, UID_MAX_LENGTH, Field, Kind, ObjectType
from dta_core.store import EPOCH, Store, VersionRef
from dta_core.store.model import iso_utc

from .errors import DEPENDENCY_FAILED, MAPPING_INVALID, MappingInvalid, Problem
from .extract import DatasetRead
from .plan import CursorPlan, MappingPlan, Plan, PropertyPlan, RelationKey, quote, relation_sql
from .records import Cursor

log = Log("sync.map")

# Errors DuckDB raises from the text of a statement alone. Their messages name
# columns and types, never the contents of a row.
_STATIC_ERRORS = (
    duckdb.BinderException,
    duckdb.CatalogException,
    duckdb.ParserException,
    duckdb.PermissionException,
    duckdb.NotImplementedException,
    duckdb.TypeMismatchException,
)
_ELEMENT_TYPE = {
    Kind.string: "VARCHAR",
    Kind.enum: "VARCHAR",
    Kind.integer: "BIGINT",
    Kind.float: "DOUBLE",
    Kind.boolean: "BOOLEAN",
    Kind.datetime: "TIMESTAMPTZ",
    Kind.date: "DATE",
}
# What a valid-time column may hold: anything DuckDB reads as a point in time.
_TIME_TYPES = ("TIMESTAMP", "DATE", "VARCHAR")
# What a cursor column that is not a time may hold: a number that only grows.
_WHOLE_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)
# Kinds whose values can be averaged, and those that have an order.
_ORDERED = (Kind.float, Kind.integer, Kind.datetime, Kind.date)
_BOUNDED = (Kind.float, Kind.integer)
_INTEGRAL_TOLERANCE = 1e-9
_LAMBDA = "__dta_e"
_EXAMPLES = 5


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _double(value: float) -> str:
    # With an exponent DuckDB reads the literal as a double, digit for digit.
    return f"{value:.16e}"


def _moment(value: datetime) -> str:
    """A point in time as a literal of the session, which works in UTC."""
    return f"CAST({_literal(value.astimezone(UTC).isoformat())} AS TIMESTAMPTZ)"


def _cursor_literal(value: Cursor) -> str:
    return _moment(value) if isinstance(value, datetime) else str(int(value))


def _plain(value: Any) -> Cursor:
    """A cursor value as it came out of a query, in the form it is kept in."""
    return value.astimezone(UTC) if isinstance(value, datetime) else int(value)


def _beyond(field_: Field, value: str) -> str | None:
    """SQL that is true when a value lies outside the bounds the ontology gives its
    property; None for a property without bounds. The bounds themselves are allowed,
    as they are by the store."""
    if field_.multivalued or field_.kind not in _BOUNDED:
        return None
    outside = []
    if field_.minimum is not None:
        outside.append(f"{value} < {_double(float(field_.minimum))}")
    if field_.maximum is not None:
        outside.append(f"{value} > {_double(float(field_.maximum))}")
    return f"({' OR '.join(outside)})" if outside else None


def explain(exc: BaseException) -> str:
    """A message about a failed statement that is safe to store and to log."""
    if isinstance(exc, _STATIC_ERRORS):
        lines = str(exc).strip().splitlines()
        return lines[0] if lines else type(exc).__name__
    return (
        f"{type(exc).__name__} while it was evaluated; the details are withheld "
        "because they may quote row values"
    )


def bucket_end_us(micros: int, interval_us: int) -> int:
    """End of the interval `(end - width, end]`, aligned to the epoch, that holds `micros`."""
    return -((-micros) // interval_us) * interval_us


def _bucket_sql(micros: str, interval_us: int) -> str:
    remainder = f"((({micros} % {interval_us}) + {interval_us}) % {interval_us})"
    return (
        f"CASE WHEN {remainder} = 0 THEN {micros} ELSE {micros} - {remainder} + {interval_us} END"
    )


class Session:
    """The DuckDB connection of one run."""

    def __init__(self) -> None:
        self._con = duckdb.connect(":memory:", config={"enable_external_access": False})
        self._con.execute("SET TimeZone='UTC'")
        self._con.execute("SET lock_configuration=true")
        self._tables: list[pa.Table] = []

    def register(self, table: pa.Table) -> str:
        """Make an Arrow table readable under a name of the session's own; returns the name."""
        name = f"__dta_arrow_{len(self._tables)}"
        self._tables.append(table)
        self._con.register(name, table)
        return name

    def run(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self._con.execute(sql, params or [])

    def rows(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self._con.execute(sql, params or []).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        # Everything is fetched: a result left half read blocks the connection.
        found = self._con.execute(sql, params or []).fetchall()
        return found[0][0] if found else None

    def arrow(self, sql: str, params: Sequence[Any] | None = None) -> pa.Table:
        return self._con.sql(sql, params=list(params or [])).to_arrow_table()

    def close(self) -> None:
        try:
            self._con.close()
        except Exception as exc:
            log.warn("closing the mapping session failed", error_type=type(exc).__name__)
        self._tables.clear()


# -- one property's value ----------------------------------------------------------


def _scaled(value: str, prop: PropertyPlan) -> str:
    if prop.affine is None:
        return value
    scaled = f"{value} * {_double(prop.affine.factor)}"
    if prop.affine.offset != 0.0:
        scaled += f" + {_double(prop.affine.offset)}"
    return f"({scaled})"


def _list_value(prop: PropertyPlan, raw: str) -> tuple[str, str]:
    kind = prop.field.kind
    element = _ELEMENT_TYPE[kind]
    value = f"TRY_CAST({raw} AS {element}[])"
    if prop.affine is not None and kind == Kind.float:
        value = f"list_transform({value}, lambda {_LAMBDA}: {_scaled(_LAMBDA, prop)})"
    # An element that cannot be cast becomes null inside the list, so the
    # elements that are not null are counted on both sides.
    wrong = [
        "{v} IS NULL",
        f"list_count({{v}}) IS DISTINCT FROM list_count(TRY_CAST({raw} AS VARCHAR[]))",
    ]
    if kind == Kind.float:
        wrong.append(f"len(list_filter({{v}}, lambda {_LAMBDA}: NOT isfinite({_LAMBDA}))) > 0")
    if kind == Kind.integer:
        wrong.append(f"TRY_CAST({raw} AS DOUBLE[]) IS DISTINCT FROM CAST({{v}} AS DOUBLE[])")
    return value, f"{raw} IS NOT NULL AND ({' OR '.join(wrong)})"


def _value(prop: PropertyPlan, raw: str, raw_type: str) -> tuple[str, str]:
    """(SQL of the value cast to the property's kind, SQL that is true when a
    source value that is not null could not be cast). `{v}` in the second
    stands for the column holding the first."""
    kind = prop.field.kind
    if prop.field.multivalued and kind != Kind.json:
        return _list_value(prop, raw)
    if kind in (Kind.string, Kind.enum):
        return f"CAST({raw} AS VARCHAR)", "FALSE"
    if kind == Kind.ref:
        return f"{_literal((prop.ref_scope or '') + ':')} || CAST({raw} AS VARCHAR)", "FALSE"
    if kind == Kind.float:
        value = _scaled(f"TRY_CAST({raw} AS DOUBLE)", prop)
        return value, f"{raw} IS NOT NULL AND ({{v}} IS NULL OR NOT isfinite({{v}}))"
    if kind == Kind.integer:
        if prop.affine is None:
            # A cast to an integer rounds; a value with a fraction is not an integer.
            return (
                f"TRY_CAST({raw} AS BIGINT)",
                f"{raw} IS NOT NULL AND ({{v}} IS NULL OR "
                f"TRY_CAST({raw} AS DOUBLE) IS DISTINCT FROM CAST({{v}} AS DOUBLE))",
            )
        exact = _scaled(f"TRY_CAST({raw} AS DOUBLE)", prop)
        return (
            f"TRY_CAST(round({exact}) AS BIGINT)",
            f"{raw} IS NOT NULL AND ({{v}} IS NULL OR abs({exact} - round({exact})) > "
            f"{_INTEGRAL_TOLERANCE} * greatest(1.0, abs({exact})))",
        )
    if kind == Kind.json:
        if raw_type == "JSON":
            document = raw
        elif raw_type == "VARCHAR":
            document = f"TRY_CAST({raw} AS JSON)"
        else:
            document = f"to_json({raw})"
        return (
            f"CAST({document} AS VARCHAR)",
            f"{raw} IS NOT NULL AND ({{v}} IS NULL OR "
            "json_type(CAST({v} AS JSON)) NOT IN ('OBJECT', 'ARRAY'))",
        )
    target = _ELEMENT_TYPE[kind]
    return f"TRY_CAST({raw} AS {target})", f"{raw} IS NOT NULL AND {{v}} IS NULL"


def _aggregate(field_: Field, aggregation: str) -> tuple[str, bool]:
    """(SQL reducing the values `v` of one interval to one, whether its valid
    time is the end of the interval rather than that of a row)."""
    scalar = not field_.multivalued
    if aggregation == "mean" and scalar and field_.kind == Kind.float:
        return "avg(v)", True
    if aggregation in ("min", "max") and scalar and field_.kind in _ORDERED:
        return f"{aggregation}(v)", True
    # Latest by valid time. Two rows of one time are ordered by value, so the
    # outcome does not depend on the order the source returned them in.
    return "arg_max(v, (vt, v))", False


# -- what a run is about to write ---------------------------------------------------


@dataclass(frozen=True)
class Window:
    """What a run may take of the data it read, and where rows without an interval go."""

    # End of the last interval that has ended, in microseconds since the epoch:
    # rows that were true later wait for a later run. None when everything
    # read lies in the past, as in a load of existing history.
    limit_us: int | None = None
    # Time of the version the run starts on. An interval ending at or before
    # it has been written, so an event that belongs there goes into the run's
    # last version.
    reached_us: int = 0

    @property
    def limit(self) -> datetime | None:
        if self.limit_us is None:
            return None
        return EPOCH + timedelta(microseconds=self.limit_us)


@dataclass(frozen=True)
class Trouble:
    """Why a source that could be read is left out of a run."""

    code: str
    message: str


@dataclass
class Slot:
    """One property of a type that at least one mapping writes."""

    index: int
    field: Field
    # Table of the property's time series: one row per object and interval.
    series: str | None = None
    # JSON structure both sides are read into before they are compared.
    structure: str | None = None

    def comparable(self, expression: str) -> str:
        if self.structure is None:
            return expression
        return f"json_transform(CAST({expression} AS JSON), {_literal(self.structure)})"


@dataclass
class TypeBatch:
    """Everything a run writes to one object type."""

    object_type: ObjectType
    slots: list[Slot]
    # One row per interval and object, sorted by interval then uid: `tk`, `uid`,
    # `ms` (the mappings that contribute), and per slot k `v_k`, `w_k`
    # (written), `tv_k` (valid time; null means the version's), `m_k` (mapping).
    rows: pa.Table
    # Interval key -> (offset, length) into `rows`.
    spans: dict[int, tuple[int, int]]
    deletes: list[str] = field(default_factory=list)
    delete_mapping: int | None = None


@dataclass
class Tally:
    """Counts of one mapping, taken while mapping."""

    rows: int = 0
    # Source rows set aside before anything was written.
    rejected: int = 0
    # Objects (static, events) or objects per interval (state series) that could be written.
    candidates: int = 0


@dataclass
class Prepared:
    """A run mapped and compared, ready to be written."""

    session: Session
    plan: Plan
    # The mappings that were evaluated: all of the plan's, less those of
    # sources left out of the run.
    active: tuple[MappingPlan, ...]
    batches: dict[str, TypeBatch]
    # Interval keys with something to write, ascending. Microseconds since the
    # epoch when the run writes in intervals; else just 0.
    keys: list[int]
    # Whether keys are times. False when everything goes into one version.
    timed: bool
    # Key of the version that takes the static rows and the deletions.
    static_key: int
    # Key of the version the run ends with, written whether or not it holds
    # data; None when the run has no time of its own to end at.
    last_key: int | None
    tallies: dict[int, Tally]
    # Per mapping: rows set aside while mapping (`uid`, `vt` and one flag per reason).
    set_aside: dict[int, pa.Table]
    # Per mapping: uids that several differing rows claim, with the number of rows.
    duplicates: dict[int, pa.Table]
    # Latest valid time taken from each dataset or view read with one.
    latest: dict[RelationKey, datetime]
    # Where the cursor of a dataset or view stands once the run is written,
    # for those whose cursor moves.
    cursors: dict[RelationKey, Cursor]
    # Per state series: rows taken, by interval key, ascending.
    consumed: dict[RelationKey, list[tuple[int, int]]]
    # Per state series: rows found at or behind the cursor that no run had taken.
    late: dict[RelationKey, int]
    # Rows of each dataset read and of each view a mapping reads.
    relation_rows: dict[RelationKey, int]
    columns: dict[RelationKey, list[str]]
    # Sources left out while mapping: something of theirs could not be evaluated.
    troubles: dict[str, Trouble]

    def close(self) -> None:
        self.session.close()

    def rows_to_write(self) -> int:
        return sum(batch.rows.num_rows + len(batch.deletes) for batch in self.batches.values())

    def previous(self, type_name: str, slot: int, uid: str, key: int) -> Any:
        """The value a time-series row was compared with: what the store held before it."""
        table = self.batches[type_name].slots[slot].series
        found = self.session.arrow(
            f"SELECT prev FROM {table} WHERE uid = ? AND tk = ?", [uid, key]
        ).to_pylist()
        return found[0]["prev"] if found else None

    def following(self, type_name: str, slot: int, uid: str, key: int) -> dict[str, Any] | None:
        """The next row of a time series after an interval, written or not."""
        table = self.batches[type_name].slots[slot].series
        found = self.session.arrow(
            f"SELECT tk, v, tv, m FROM {table} WHERE uid = ? AND tk > ? ORDER BY tk LIMIT 1",
            [uid, key],
        ).to_pylist()
        return found[0] if found else None


class _Mapper:
    def __init__(
        self,
        session: Session,
        plan: Plan,
        reads: Mapping[RelationKey, DatasetRead],
        store: Store,
        head: VersionRef,
        stored_cursors: Mapping[RelationKey, Cursor],
        window: Window,
        active: Sequence[MappingPlan],
        alone: bool,
        accounted: Mapping[RelationKey, int],
    ) -> None:
        self.session = session
        self.plan = plan
        self.reads = reads
        self.store = store
        self.head = head
        self.stored_cursors = stored_cursors
        self.window = window
        self.active = list(active)
        # Whether a source that cannot be mapped is left out and the others go on.
        self.alone = alone
        self.accounted = accounted
        self.problems: list[Problem] = []
        self.troubles: dict[str, Trouble] = {}
        self.failed: set[RelationKey] = set()
        self.available: set[RelationKey] = set()
        self.raw_types: dict[int, list[str]] = {}
        # Per mapping of events read by a cursor: whether the cursor is a time.
        self.cursor_time: dict[int, bool] = {}
        self.flags: dict[int, list[str]] = {}
        self.tallies: dict[int, Tally] = {m.index: Tally() for m in plan.mappings}
        self.set_aside: dict[int, pa.Table] = {}
        self.duplicates: dict[int, pa.Table] = {}
        self.latest: dict[RelationKey, datetime] = {}
        self.cursors: dict[RelationKey, Cursor] = {}
        self.consumed: dict[RelationKey, list[tuple[int, int]]] = {}
        self.late: dict[RelationKey, int] = {}
        self.relation_rows: dict[RelationKey, int] = {}
        self.stored: dict[str, str] = {}
        self.slots: dict[str, list[Slot]] = {}

    def problem(self, where: str, message: str) -> None:
        self.problems.append(Problem(MAPPING_INVALID, where, message))

    def fault(self, source_id: str, where: str, message: str, code: str = MAPPING_INVALID) -> None:
        """Something of one source cannot be evaluated against the data read."""
        if not self.alone:
            self.problem(where, message)
            return
        if source_id in self.troubles or not any(m.source_id == source_id for m in self.active):
            return
        self.troubles[source_id] = Trouble(code, f"{where}: {message}")
        log.warn(
            "a source cannot be mapped; it is left as it was and the run goes on without it",
            source=source_id,
            code=code,
            where=where,
            reason=message,
        )

    def settle(self) -> None:
        """Stop with every problem found, and take the sources left out off the list."""
        if self.problems:
            log.error(
                "mappings cannot be evaluated against the data read",
                problems=[(p.where, p.message) for p in self.problems],
            )
            raise MappingInvalid(self.problems)
        for found in (self.latest, self.cursors, self.consumed, self.late):
            for key in [key for key in found if key[0] in self.troubles]:
                del found[key]
        self.active = [m for m in self.active if m.source_id not in self.troubles]

    # -- datasets and views -----------------------------------------------------

    def register(self) -> None:
        needed = self.plan.needed(self.active)
        schemas = {key[0] for key in needed} | {key[0] for key in self.reads}
        for schema in sorted(schemas):
            self.session.run(f"CREATE SCHEMA IF NOT EXISTS {quote(schema)}")
        for key, read in self.reads.items():
            name = self.session.register(read.table)
            self.session.run(f"CREATE VIEW {relation_sql(*key)} AS SELECT * FROM {name}")
            self.relation_rows[key] = read.table.num_rows
            self.available.add(key)
        for view in self.plan.views:
            if view.key not in needed:
                continue
            missing = [ref for ref in view.references if ref not in self.available]
            if missing:
                # A view it depends on failed, and is reported there; or, in a
                # run where sources fail alone, what it reads was not read.
                self.failed.add(view.key)
                if self.alone:
                    self.fault(
                        view.source_id,
                        view.where,
                        f"reads {missing[0][0]}.{missing[0][1]}, which could not be read",
                        DEPENDENCY_FAILED,
                    )
                continue
            kind = "TABLE" if view.materialised else "VIEW"
            started = time.perf_counter()
            try:
                self.session.run(f"CREATE {kind} {relation_sql(*view.key)} AS {view.sql}")
                if view.materialised:
                    self.relation_rows[view.key] = self.session.scalar(
                        f"SELECT count(*) FROM {relation_sql(*view.key)}"
                    )
            except duckdb.Error as exc:
                self.failed.add(view.key)
                log.debug("view failed", view=view.where, error=str(exc))
                self.fault(view.source_id, view.where, explain(exc))
                continue
            self.available.add(view.key)
            log.debug(
                "view created",
                view=view.where,
                kept=view.materialised,
                rows=self.relation_rows.get(view.key),
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )

    # -- mapping ------------------------------------------------------------------

    def _source_columns(self, mapping: MappingPlan) -> list[str]:
        columns = [f"{quote(name)} AS __dta_k{n}" for n, name in enumerate(mapping.identity)]
        if mapping.valid_time is not None:
            columns.append(f"{quote(mapping.valid_time)} AS __dta_rvt")
        if mapping.cursor is not None:
            columns.append(f"{quote(mapping.cursor)} AS __dta_rc")
        columns.extend(f"{prop.sql} AS __dta_r{j}" for j, prop in enumerate(mapping.properties))
        return columns

    def describe(self, mapping: MappingPlan) -> None:
        """Bind the mapping against its dataset: finds a missing column before anything runs."""
        if mapping.key in self.failed:
            return
        sql = (
            f"DESCRIBE SELECT {', '.join(self._source_columns(mapping))} "
            f"FROM {relation_sql(*mapping.key)}"
        )
        if mapping.filter_sql:
            sql += f" WHERE {mapping.filter_sql}"
        try:
            described = {name: type_ for name, type_, *_ in self.session.rows(sql)}
        except duckdb.Error as exc:
            self.failed.add(mapping.key)
            log.debug("mapping does not bind", mapping=mapping.label, error=str(exc))
            self.fault(mapping.source_id, mapping.where, explain(exc))
            return
        self.raw_types[mapping.index] = [
            described[f"__dta_r{j}"] for j in range(len(mapping.properties))
        ]
        time_type = described.get("__dta_rvt")
        if time_type is not None and not time_type.startswith(_TIME_TYPES):
            self.fault(
                mapping.source_id,
                f"{mapping.where}.valid_time",
                f"{mapping.valid_time} holds {time_type}, which is not a time",
            )
        cursor_type = described.get("__dta_rc")
        if cursor_type is not None:
            whole = cursor_type in _WHOLE_TYPES
            # Text is read as a time only where it is already the valid time.
            timed = cursor_type.startswith(("TIMESTAMP", "DATE")) or (
                mapping.cursor == mapping.valid_time and cursor_type.startswith(_TIME_TYPES)
            )
            if not (whole or timed):
                self.fault(
                    mapping.source_id,
                    f"{mapping.where}.cursor",
                    f"{mapping.cursor} holds {cursor_type}; a cursor is a whole number or a time",
                )
            self.cursor_time[mapping.index] = not whole

    def _cursor_value(self, index: int, raw: str) -> str:
        """SQL of a cursor column's value in the form it is compared and stored in."""
        return f"TRY_CAST({raw} AS TIMESTAMPTZ)" if self.cursor_time[index] else raw

    def _since(self, mapping: MappingPlan) -> Cursor | None:
        """The stored cursor a mapping reads on from; None when it takes every row."""
        if mapping.cursor_column is None:
            return None
        return self.stored_cursors.get(mapping.key)

    def _mapping_sql(self, mapping: MappingPlan, since: Cursor | None) -> str:
        where = []
        if mapping.filter_sql:
            where.append(mapping.filter_sql)
        limit = self.window.limit
        if mapping.valid_time is not None:
            moment = f"TRY_CAST({quote(mapping.valid_time)} AS TIMESTAMPTZ)"
            if mapping.state:
                if since is not None:
                    where.append(f"{moment} > {_cursor_literal(since)}")
                if limit is not None:
                    # A row without a readable time is taken once, on the first
                    # read, to be set aside there.
                    closed = f"{moment} <= {_moment(limit)}"
                    where.append(closed if since is not None else f"({closed} OR {moment} IS NULL)")
            else:
                if since is not None:
                    value = self._cursor_value(mapping.index, quote(mapping.cursor or ""))
                    where.append(f"{value} > {_cursor_literal(since)}")
                if limit is not None:
                    where.append(f"({moment} <= {_moment(limit)} OR {moment} IS NULL)")
        key = " || '|' || ".join(
            f"CAST(__dta_k{n} AS VARCHAR)" for n in range(len(mapping.identity))
        )
        scope = _literal(mapping.object_type.scope + ":")
        cast = [f"CASE WHEN {key} <> '' THEN {scope} || {key} END AS uid"]
        flags = [f"(uid IS NULL OR length(uid) > {UID_MAX_LENGTH}) AS bk"]
        final = [UID_FIELD]
        if mapping.valid_time is not None:
            cast.append("TRY_CAST(__dta_rvt AS TIMESTAMPTZ) AS vt")
            flags.append("(vt IS NULL) AS bvt")
            final.extend(("vt", "tk"))
        if mapping.cursor is not None:
            cast.append(f"{self._cursor_value(mapping.index, '__dta_rc')} AS cur")
            final.append("cur")
        bad = ["bk"] + (["bvt"] if mapping.valid_time is not None else [])
        types = self.raw_types[mapping.index]
        outside = []
        for j, prop in enumerate(mapping.properties):
            value, wrong = _value(prop, f"__dta_r{j}", types[j])
            cast.append(f"{value} AS v{j}")
            flags.append(f"coalesce({wrong.replace('{v}', f'v{j}')}, FALSE) AS b{j}")
            final.append(f"v{j}")
            bad.append(f"b{j}")
            beyond = _beyond(prop.field, f"v{j}") if mapping.state else None
            if beyond is not None:
                # Checked here, row by row, so that one reading beyond the bounds
                # does not take the value of its whole interval with it.
                flags.append(f"coalesce({beyond}, FALSE) AS o{j}")
                outside.append(f"o{j}")
        bad += outside
        self.flags[mapping.index] = list(bad)
        if mapping.valid_time is None:
            bucket = ""
        elif self.plan.interval_us is None:
            bucket = ", CAST(0 AS BIGINT) AS tk"
        else:
            bucket = f", {_bucket_sql('epoch_us(vt)', self.plan.interval_us)} AS tk"
        names = ", ".join(final + bad)
        return (
            f"WITH s AS (SELECT {', '.join(self._source_columns(mapping))} "
            f"FROM {relation_sql(*mapping.key)}"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + f"), c AS (SELECT *, {', '.join(cast)} FROM s), "
            f"d AS (SELECT *{bucket}, {', '.join(flags)} FROM c) "
            f"SELECT {names}, ({' OR '.join(bad)}) AS bad FROM d"
        )

    def map(self, mapping: MappingPlan) -> None:
        since = self._since(mapping)
        table = f"__dta_m{mapping.index}"
        started = time.perf_counter()
        try:
            self.session.run(f"CREATE TEMP TABLE {table} AS {self._mapping_sql(mapping, since)}")
        except duckdb.Error as exc:
            log.debug("mapping failed", mapping=mapping.label, error=str(exc))
            self.fault(mapping.source_id, mapping.where, explain(exc))
            return
        tally = self.tallies[mapping.index]
        tally.rows, tally.rejected = self.session.rows(
            f"SELECT count(*), count(*) FILTER (WHERE bad) FROM {table}"
        )[0]
        if tally.rejected:
            moment = "vt" if mapping.timeseries else "CAST(NULL AS TIMESTAMPTZ) AS vt"
            flags = ", ".join(self.flags[mapping.index])
            self.set_aside[mapping.index] = self.session.arrow(
                f"SELECT uid, {moment}, {flags} FROM {table} WHERE bad"
            )
            log.warn(
                "rows set aside while mapping: a value does not fit its property",
                mapping=mapping.label,
                rows=tally.rejected,
                of=tally.rows,
            )
        log.info(
            "mapping evaluated",
            mapping=mapping.label,
            object_type=mapping.object_type.name,
            rows=tally.rows,
            set_aside=tally.rejected,
            incremental=since is not None,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    # -- how far each dataset is read ---------------------------------------------

    def measure(self) -> None:
        """For each dataset or view read with a valid time: the latest time taken
        from it, where its cursor stands afterwards, and what arrived behind it."""
        times = self.plan.valid_times()
        for key in dict.fromkeys(m.key for m in self.active if m.timeseries):
            if key in self.failed:
                continue
            readers = [m for m in self.active if m.key == key and m.timeseries]
            cursor = self.plan.cursors.get(key)
            since = self.stored_cursors.get(key) if cursor is not None else None
            try:
                if cursor is not None and cursor.time:
                    self._measure_series(key, times[key], since, readers)
                else:
                    self._measure_events(key, times[key], cursor, since, readers)
            except duckdb.Error as exc:
                # Reported where the mapping itself fails to evaluate.
                log.debug("a dataset's valid times cannot be read", relation=key, error=str(exc))

    def _measure_series(
        self, key: RelationKey, column: str, since: Cursor | None, readers: list[MappingPlan]
    ) -> None:
        base = f"(SELECT TRY_CAST({quote(column)} AS TIMESTAMPTZ) AS vt FROM {relation_sql(*key)})"
        limit = self.window.limit
        closed = "vt IS NOT NULL" + (f" AND vt <= {_moment(limit)}" if limit is not None else "")
        taken = closed + (f" AND vt > {_cursor_literal(since)}" if since is not None else "")
        behind = (
            f"count(*) FILTER (WHERE vt <= {_cursor_literal(since)})" if since is not None else "0"
        )
        newest, found_behind, newest_of_all = self.session.rows(
            f"SELECT max(vt) FILTER (WHERE {taken}), {behind}, max(vt) FILTER (WHERE {closed}) "
            f"FROM {base}"
        )[0]
        # Events mapped from the same dataset without a cursor take every row of it.
        whole = any(m.cursor_column is None for m in readers)
        latest = newest_of_all if whole else newest
        if latest is not None:
            self.latest[key] = latest.astimezone(UTC)
        interval_us = self.plan.interval_us
        bucket = (
            "CAST(0 AS BIGINT)" if interval_us is None else _bucket_sql("epoch_us(vt)", interval_us)
        )
        self.consumed[key] = [
            (int(tk), int(rows))
            for tk, rows in self.session.rows(
                f"SELECT tk, count(*) FROM (SELECT {bucket} AS tk FROM {base} WHERE {taken}) "
                "GROUP BY tk ORDER BY tk"
            )
        ]
        if since is not None:
            # A dataset asked only for rows after its cursor returned these all the
            # same. One read whole holds every row it ever had: there the rows
            # that earlier runs took are known only by their number.
            read = self.reads.get(key)
            asked_after = read is not None and read.since is not None
            late = (
                found_behind if asked_after else max(0, found_behind - self.accounted.get(key, 0))
            )
            if late:
                self.late[key] = int(late)
                log.warn(
                    "rows arrived behind the cursor of their series; they are counted "
                    "as late and not written",
                    source=key[0],
                    dataset=key[1],
                    rows=int(late),
                    cursor=iso_utc(since) if isinstance(since, datetime) else since,
                )
        if limit is None:
            if newest is not None:
                self.cursors[key] = newest.astimezone(UTC)
        elif (since is not None or newest is not None) and (since is None or limit > since):
            # Every interval up to the limit is closed for this series: its
            # cursor moves there whether or not the rows could be written.
            self.cursors[key] = limit

    def _measure_events(
        self,
        key: RelationKey,
        column: str,
        cursor: CursorPlan | None,
        since: Cursor | None,
        readers: list[MappingPlan],
    ) -> None:
        columns = [f"TRY_CAST({quote(column)} AS TIMESTAMPTZ) AS vt"]
        by_cursor = [m for m in readers if m.cursor is not None]
        if cursor is not None and by_cursor:
            columns.append(f"{self._cursor_value(by_cursor[0].index, quote(cursor.column))} AS cur")
        else:
            cursor = None
        base = f"(SELECT {', '.join(columns)} FROM {relation_sql(*key)})"
        limit = self.window.limit
        after = (
            f"cur > {_cursor_literal(since)}" if cursor is not None and since is not None else ""
        )
        seen = ["vt IS NOT NULL"]
        if limit is not None:
            seen.append(f"vt <= {_moment(limit)}")
        if after and len(by_cursor) == len(readers):
            seen.append(after)
        waiting_sql = "NULL"
        if cursor is not None and limit is not None:
            ahead = [f"vt > {_moment(limit)}"] + ([after] if after else [])
            waiting_sql = f"min(cur) FILTER (WHERE {' AND '.join(ahead)})"
        newest, waiting = self.session.rows(
            f"SELECT max(vt) FILTER (WHERE {' AND '.join(seen)}), {waiting_sql} FROM {base}"
        )[0]
        if newest is not None:
            self.latest[key] = newest.astimezone(UTC)
        if cursor is None:
            return
        passed = ["cur IS NOT NULL"] + ([after] if after else [])
        if waiting is not None:
            # An event of an interval that has not ended waits, and the cursor
            # stays before it. What lies after it is read again by the next run
            # and found equal to what is stored.
            passed.append(f"cur < {_cursor_literal(_plain(waiting))}")
            log.debug(
                "events of an interval that has not ended wait; the cursor stays before them",
                source=key[0],
                dataset=key[1],
            )
        reached = self.session.scalar(f"SELECT max(cur) FROM {base} WHERE {' AND '.join(passed)}")
        if reached is not None:
            reached = _plain(reached)
            if since is None or reached > since:
                self.cursors[key] = reached

    # -- what is stored ---------------------------------------------------------

    def read_stored(self) -> None:
        """The head state of every type a mapping writes: one read per type."""
        names: dict[str, set[str]] = {}
        types: dict[str, ObjectType] = {}
        for mapping in self.active:
            types[mapping.object_type.name] = mapping.object_type
            names.setdefault(mapping.object_type.name, set()).update(
                prop.name for prop in mapping.properties
            )
        for position, (type_name, object_type) in enumerate(sorted(types.items())):
            fields = [f for f in object_type.fields if f.name in names[type_name]]
            started = time.perf_counter()
            table = self.store.table(type_name, columns=[f.name for f in fields], at=self.head)
            self.stored[type_name] = self.session.register(table)
            self.slots[type_name] = [Slot(index, f) for index, f in enumerate(fields)]
            log.debug(
                "stored state read",
                type=type_name,
                objects=table.num_rows,
                properties=len(fields),
                position=position,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )

    def _slot(self, mapping: MappingPlan, prop: PropertyPlan) -> Slot:
        return next(s for s in self.slots[mapping.object_type.name] if s.field.name == prop.name)

    def _feeders(self, type_name: str, name: str) -> list[tuple[MappingPlan, int]]:
        return [
            (mapping, j)
            for mapping in self.active
            if mapping.object_type.name == type_name
            for j, prop in enumerate(mapping.properties)
            if prop.name == name
        ]

    def structures(self) -> None:
        """For each structured property, the shape its mapped and stored values share.

        Both sides are read into that shape before they are compared, so the
        order of an object's keys and the way a number is written do not count
        as a difference.
        """
        for type_name, slots in self.slots.items():
            for slot in slots:
                if slot.field.kind != Kind.json:
                    continue
                parts = [
                    f"SELECT v{j} AS j FROM __dta_m{mapping.index} WHERE NOT bad"
                    for mapping, j in self._feeders(type_name, slot.field.name)
                ]
                parts.append(f"SELECT {quote(slot.field.name)} AS j FROM {self.stored[type_name]}")
                slot.structure = self.session.scalar(
                    "SELECT json_group_structure(CAST(j AS JSON)) FROM ("
                    + " UNION ALL ".join(parts)
                    + ")"
                )

    # -- rows compared with what is stored ----------------------------------------

    def _unique(self, mapping: MappingPlan, columns: str) -> None:
        """One row per identity; identities that several differing rows claim are set aside."""
        index = mapping.index
        self.session.run(
            f"CREATE TEMP TABLE __dta_g{index} AS SELECT {columns} FROM ("
            f"SELECT *, count(*) OVER (PARTITION BY uid) AS n FROM ("
            f"SELECT DISTINCT {columns} FROM __dta_m{index} WHERE NOT bad)) WHERE n = 1"
        )
        duplicates = self.session.arrow(
            f"SELECT uid, count(*) AS n FROM __dta_m{index} WHERE NOT bad AND uid NOT IN "
            f"(SELECT uid FROM __dta_g{index}) GROUP BY uid ORDER BY uid"
        )
        if duplicates.num_rows:
            self.duplicates[index] = duplicates
            rows = sum(duplicates.column("n").to_pylist())
            self.tallies[index].rejected += rows
            log.warn(
                "rows set aside: several rows that differ claim one identity",
                mapping=mapping.label,
                identities=duplicates.num_rows,
                rows=rows,
            )

    def _same(self, mapping: MappingPlan) -> str:
        """SQL that is true when the stored object `s` holds what the mapped row `g` says."""
        same = ["s.uid = g.uid"]
        for j, prop in enumerate(mapping.properties):
            slot = self._slot(mapping, prop)
            same.append(
                f"{slot.comparable('s.' + quote(prop.name))} IS NOT DISTINCT FROM "
                f"{slot.comparable(f'g.v{j}')}"
            )
        return " AND ".join(same)

    def static(self, mapping: MappingPlan) -> None:
        """Rows of a static mapping that are new or differ from what is stored.

        `fresh` marks the objects the store does not hold yet.
        """
        index = mapping.index
        values = [f"v{j}" for j in range(len(mapping.properties))]
        self._unique(mapping, ", ".join([UID_FIELD, *values]))
        self.tallies[index].candidates = self.session.scalar(f"SELECT count(*) FROM __dta_g{index}")
        stored = self.stored[mapping.object_type.name]
        self.session.run(
            f"CREATE TEMP TABLE __dta_n{index} AS SELECT g.*, "
            f"NOT EXISTS (SELECT 1 FROM {stored} s WHERE s.uid = g.uid) AS fresh "
            f"FROM __dta_g{index} g "
            f"WHERE NOT EXISTS (SELECT 1 FROM {stored} s WHERE {self._same(mapping)})"
        )

    def events(self, mapping: MappingPlan) -> None:
        """Events that are new, and those whose values differ from the stored ones.

        An event is one object, found by its identity. `fresh` marks the ones
        the store does not hold; the others are corrections of events it does.
        """
        index = mapping.index
        values = [f"v{j}" for j in range(len(mapping.properties))]
        columns = ", ".join([UID_FIELD, "vt", *values])
        if mapping.cursor is not None:
            # Several rows of one identity are revisions of one event: the one
            # furthest along the cursor stands. The values break a tie, so the
            # outcome does not depend on the order the source returned them in.
            order = ", ".join(["cur DESC NULLS LAST", "vt DESC", *(f"{v} DESC" for v in values)])
            self.session.run(
                f"CREATE TEMP TABLE __dta_g{index} AS SELECT {columns} FROM ("
                f"SELECT *, row_number() OVER (PARTITION BY uid ORDER BY {order}) AS n "
                f"FROM __dta_m{index} WHERE NOT bad) WHERE n = 1"
            )
        else:
            self._unique(mapping, columns)
        self.tallies[index].candidates = self.session.scalar(f"SELECT count(*) FROM __dta_g{index}")
        stored = self.stored[mapping.object_type.name]
        self.session.run(
            f"CREATE TEMP TABLE __dta_n{index} AS SELECT g.*, "
            f"NOT EXISTS (SELECT 1 FROM {stored} s WHERE s.uid = g.uid) AS fresh "
            f"FROM __dta_g{index} g "
            f"WHERE NOT EXISTS (SELECT 1 FROM {stored} s WHERE {self._same(mapping)})"
        )

    def deletes(self, mapping: MappingPlan) -> list[str]:
        """Stored objects of a snapshot mapping's type that its dataset no longer holds."""
        stored = self.stored[mapping.object_type.name]
        found = self.session.arrow(
            f"SELECT s.uid FROM {stored} s WHERE NOT EXISTS "
            f"(SELECT 1 FROM __dta_m{mapping.index} m WHERE m.uid = s.uid) ORDER BY s.uid"
        )
        return found.column("uid").to_pylist()

    def _event_key(self, timed: bool, last_key: int | None) -> str:
        """SQL of the interval key of the version an event row goes into.

        A new event enters in the interval of its valid time when that interval
        has not been written yet. An older one, and every correction, goes into
        the run's last version: nothing is written behind what the twin holds.
        """
        if not timed or self.plan.interval_us is None:
            return "CAST(0 AS BIGINT)"
        bucket = _bucket_sql("epoch_us(vt)", self.plan.interval_us)
        return (
            f"CASE WHEN fresh AND {bucket} > {self.window.reached_us} THEN {bucket} "
            f"ELSE CAST({last_key or 0} AS BIGINT) END"
        )

    def event_keys(self, timed: bool, last_key: int | None) -> tuple[int, int | None]:
        """(Event rows to write, the earliest interval key among them)."""
        parts = [
            f"SELECT {self._event_key(timed, last_key)} AS tk FROM __dta_n{mapping.index}"
            for mapping in self.active
            if mapping.event
        ]
        if not parts:
            return 0, None
        rows, first = self.session.rows(
            "SELECT count(*), min(tk) FROM (" + " UNION ALL ".join(parts) + ")"
        )[0]
        return int(rows), int(first) if first is not None else None

    # -- state series -------------------------------------------------------------

    def series(self, type_name: str, slot: Slot, position: int) -> None:
        """One row per object and interval of a property, with what it is compared to."""
        feeders = [(m, j) for m, j in self._feeders(type_name, slot.field.name) if m.state]
        if not feeders:
            return
        reduce, at_interval_end = _aggregate(slot.field, self.plan.aggregation)
        if not at_interval_end:
            moment = "max(vt)"
        elif self.plan.interval_us is None:
            # One version holds everything; its time stands for the interval's end.
            moment = "CAST(NULL AS TIMESTAMPTZ)"
        else:
            moment = "make_timestamptz(tk)"
        parts = [
            f"SELECT uid, tk, {reduce} AS v, {moment} AS tv, CAST({mapping.index} AS INTEGER) AS m "
            f"FROM (SELECT uid, tk, vt, v{j} AS v FROM __dta_m{mapping.index} "
            f"WHERE NOT bad AND v{j} IS NOT NULL) GROUP BY uid, tk"
            for mapping, j in feeders
        ]
        reduced = f"__dta_a{position}"
        self.session.run(f"CREATE TEMP TABLE {reduced} AS " + " UNION ALL ".join(parts))
        if len(feeders) > 1:
            clashes = self.session.rows(
                f"SELECT uid FROM {reduced} GROUP BY uid, tk HAVING count(*) > 1 "
                f"ORDER BY uid LIMIT {_EXAMPLES}"
            )
            if clashes:
                self._clash(type_name, slot, [m for m, _ in feeders], clashes, "in one interval")
        table = f"__dta_c{position}"
        column = quote(slot.field.name)
        self.session.run(
            f"CREATE TEMP TABLE {table} AS SELECT uid, tk, v, tv, m, prev, "
            f"({slot.comparable('v')} IS DISTINCT FROM {slot.comparable('prev')}) AS w FROM ("
            f"SELECT a.uid, a.tk, a.v, a.tv, a.m, CASE WHEN row_number() OVER run = 1 "
            f"THEN s.{column} ELSE lag(a.v) OVER run END AS prev "
            f"FROM {reduced} a LEFT JOIN {self.stored[type_name]} s ON s.uid = a.uid "
            f"WINDOW run AS (PARTITION BY a.uid ORDER BY a.tk))"
        )
        slot.series = table

    def _clash(
        self,
        type_name: str,
        slot: Slot,
        mappings: list[MappingPlan],
        examples: list[tuple[Any, ...]],
        when: str,
    ) -> None:
        labels = ", ".join(mapping.label for mapping in mappings)
        uids = ", ".join(str(row[0]) for row in examples)
        self.problem(
            f"{mappings[-1].where}.properties.{slot.field.name}",
            f"{type_name}.{slot.field.name} of one object is written by several mappings "
            f"{when} ({labels}); for example {uids}",
        )

    def static_clashes(self, type_name: str, slot: Slot, position: int) -> None:
        """A property written whole, by a static mapping or as an event, must come
        from that mapping alone.

        Such a value is compared with the stored one on every run, so a second
        writer would have each run undo what the other wrote.
        """
        feeders = self._feeders(type_name, slot.field.name)
        whole = [mapping for mapping, _ in feeders if not mapping.state]
        if not whole or len(feeders) < 2:
            return
        parts = [
            f"SELECT uid, {mapping.index} AS m FROM __dta_g{mapping.index}" for mapping in whole
        ]
        if slot.series is not None:
            parts.append(f"SELECT DISTINCT uid, m FROM __dta_a{position}")
        clashes = self.session.rows(
            "SELECT uid FROM (" + " UNION ALL ".join(parts) + ") GROUP BY uid "
            f"HAVING count(DISTINCT m) > 1 ORDER BY uid LIMIT {_EXAMPLES}"
        )
        if clashes:
            self._clash(type_name, slot, [m for m, _ in feeders], clashes, "in one run")

    def series_candidates(self, mapping: MappingPlan) -> None:
        present = " OR ".join(f"v{j} IS NOT NULL" for j in range(len(mapping.properties)))
        self.tallies[mapping.index].candidates = self.session.scalar(
            f"SELECT count(*) FROM (SELECT DISTINCT uid, tk FROM __dta_m{mapping.index} "
            f"WHERE NOT bad AND ({present}))"
        )

    def beyond_bounds(self, mapping: MappingPlan) -> str | None:
        """SQL of the earliest interval holding a reading that is sound but for its bounds.

        Such a reading is not written, yet it says that the history had begun
        by then: a load starts at the first interval the sources speak of.
        """
        flags = self.flags.get(mapping.index, [])
        outside = [flag for flag in flags if flag.startswith("o")]
        if not mapping.state or not outside:
            return None
        sound = " OR ".join(flag for flag in flags if not flag.startswith("o"))
        return (
            f"SELECT min(tk) AS tk FROM __dta_m{mapping.index} "
            f"WHERE NOT ({sound}) AND ({' OR '.join(outside)})"
        )

    # -- one table per type -----------------------------------------------------

    def batch(
        self,
        type_name: str,
        object_type: ObjectType,
        static_key: int,
        new_key: int,
        timed: bool,
        last_key: int | None,
    ) -> TypeBatch:
        slots = self.slots[type_name]
        parts = []
        for mapping in self.active:
            if mapping.object_type.name != type_name or mapping.state:
                continue
            if mapping.event:
                # An event keeps the time it happened at, whichever version it is written in.
                key, valid = self._event_key(timed, last_key), "vt"
            else:
                # An object new to the twin is there before the readings and
                # events of the same run that refer to it.
                key = (
                    f"CASE WHEN fresh THEN CAST({new_key} AS BIGINT) "
                    f"ELSE CAST({static_key} AS BIGINT) END"
                )
                valid = "CAST(NULL AS TIMESTAMPTZ)"
            columns = [f"{key} AS tk", UID_FIELD, f"CAST({mapping.index} AS INTEGER) AS m"]
            for j, prop in enumerate(mapping.properties):
                k = self._slot(mapping, prop).index
                columns += [
                    f"v{j} AS v_{k}",
                    f"TRUE AS w_{k}",
                    f"{valid} AS tv_{k}",
                    f"CAST({mapping.index} AS INTEGER) AS m_{k}",
                ]
            parts.append(f"SELECT {', '.join(columns)} FROM __dta_n{mapping.index}")
        for slot in slots:
            if slot.series is not None:
                k = slot.index
                parts.append(
                    f"SELECT tk, uid, m, v AS v_{k}, TRUE AS w_{k}, tv AS tv_{k}, m AS m_{k} "
                    f"FROM {slot.series} WHERE w"
                )
        merged = ["tk", UID_FIELD, "list_sort(list_distinct(list(m))) AS ms"]
        for slot in slots:
            k = slot.index
            merged += [
                f"any_value(v_{k}) AS v_{k}",
                f"coalesce(bool_or(w_{k}), FALSE) AS w_{k}",
                f"any_value(tv_{k}) AS tv_{k}",
                f"any_value(m_{k}) AS m_{k}",
            ]
        rows = self.session.arrow(
            f"SELECT {', '.join(merged)} FROM ("
            + " UNION ALL BY NAME ".join(parts)
            + ") GROUP BY tk, uid ORDER BY tk, uid"
        )
        keys, starts, counts = np.unique(
            rows.column("tk").to_numpy(), return_index=True, return_counts=True
        )
        spans = {
            int(key): (int(start), int(count))
            for key, start, count in zip(keys, starts, counts, strict=True)
        }
        return TypeBatch(object_type, slots, rows, spans)


def prepare(
    plan: Plan,
    reads: Mapping[RelationKey, DatasetRead],
    store: Store,
    head: VersionRef,
    stored_cursors: Mapping[RelationKey, Cursor],
    window: Window | None = None,
    *,
    active: Sequence[MappingPlan] | None = None,
    alone: bool = False,
    accounted: Mapping[RelationKey, int] | None = None,
) -> Prepared:
    """Map every dataset read and compare the result with the stored head state.

    `active` names the mappings to evaluate, by default all of the plan's.
    `accounted` gives, per dataset or view, the rows earlier runs took from it.

    Raises `sync.mapping_invalid`, with every problem found, when a view or a
    mapping cannot be evaluated against the data, or when two mappings claim
    the same property of the same object. With `alone`, a source whose view or
    mapping cannot be evaluated is instead left out and named in the result's
    `troubles`; only mappings that claim the same property still raise.
    """
    session = Session()
    try:
        return _prepare(
            session,
            plan,
            reads,
            store,
            head,
            stored_cursors,
            window or Window(),
            plan.mappings if active is None else active,
            alone,
            accounted or {},
        )
    except BaseException:
        session.close()
        raise


def _prepare(
    session: Session,
    plan: Plan,
    reads: Mapping[RelationKey, DatasetRead],
    store: Store,
    head: VersionRef,
    stored_cursors: Mapping[RelationKey, Cursor],
    window: Window,
    active: Sequence[MappingPlan],
    alone: bool,
    accounted: Mapping[RelationKey, int],
) -> Prepared:
    started = time.perf_counter()
    mapper = _Mapper(
        session, plan, reads, store, head, stored_cursors, window, active, alone, accounted
    )
    mapper.register()
    for mapping in list(mapper.active):
        mapper.describe(mapping)
    mapper.settle()
    mapper.measure()
    for mapping in list(mapper.active):
        if mapping.source_id not in mapper.troubles:
            mapper.map(mapping)
    mapper.settle()
    mapped = time.perf_counter()

    mapper.read_stored()
    mapper.structures()
    for mapping in mapper.active:
        if mapping.state:
            mapper.series_candidates(mapping)
        elif mapping.event:
            mapper.events(mapping)
        else:
            mapper.static(mapping)
    position = 0
    reduced: list[str] = []
    for type_name, slots in mapper.slots.items():
        for slot in slots:
            mapper.series(type_name, slot, position)
            mapper.static_clashes(type_name, slot, position)
            if slot.series is not None:
                reduced.append(f"SELECT min(tk) AS tk FROM __dta_a{position}")
            position += 1
    reduced.extend(part for part in map(mapper.beyond_bounds, mapper.active) if part is not None)
    mapper.settle()

    latest = max(mapper.latest.values(), default=None)
    first_key = None
    if reduced:
        first_key = session.scalar("SELECT min(tk) FROM (" + " UNION ALL ".join(reduced) + ")")
    if window.limit_us is not None:
        # A run that repeats ends at the last interval that has ended, whatever
        # it read. What changes an object the twin holds is written there: when
        # it changed is not known, only that it has by now.
        timed, last_key = True, window.limit_us
        static_key = last_key
        _, first_event = mapper.event_keys(timed, last_key)
        found = [int(key) for key in (first_key, first_event) if key is not None]
        new_key = min([*found, last_key])
    else:
        events, _ = mapper.event_keys(False, None)
        timed = plan.interval_us is not None and (first_key is not None or events > 0)
        last_key = None
        if timed and latest is not None and plan.interval_us is not None:
            # A load ends at the interval of the latest reading it took.
            micros = (latest - EPOCH) // timedelta(microseconds=1)
            last_key = bucket_end_us(micros, plan.interval_us)
        _, first_event = mapper.event_keys(timed, last_key)
        found = [int(key) for key in (first_key, first_event) if key is not None]
        # Existing history hangs on its first interval: what has no time of its
        # own is there before anything refers to it.
        static_key = new_key = min(found) if timed and found else 0
    batches: dict[str, TypeBatch] = {}
    keys: set[int] = set()
    types = {m.object_type.name: m.object_type for m in mapper.active}
    for type_name, object_type in types.items():
        batch = mapper.batch(type_name, object_type, static_key, new_key, timed, last_key)
        for mapping in mapper.active:
            if mapping.snapshot and mapping.object_type.name == type_name:
                batch.deletes = mapper.deletes(mapping)
                batch.delete_mapping = mapping.index
                if batch.deletes:
                    keys.add(static_key)
        keys.update(batch.spans)
        batches[type_name] = batch
    prepared = Prepared(
        session=session,
        plan=plan,
        active=tuple(mapper.active),
        batches=batches,
        keys=sorted(keys),
        timed=timed,
        static_key=static_key,
        last_key=last_key,
        tallies=mapper.tallies,
        set_aside=mapper.set_aside,
        duplicates=mapper.duplicates,
        latest=mapper.latest,
        cursors=mapper.cursors,
        consumed=mapper.consumed,
        late=mapper.late,
        relation_rows=mapper.relation_rows,
        columns={key: list(read.table.column_names) for key, read in reads.items()},
        troubles=mapper.troubles,
    )
    log.info(
        "mapped and compared",
        mappings=len(mapper.active),
        rows_mapped=sum(tally.rows for tally in mapper.tallies.values()),
        rows_set_aside=sum(tally.rejected for tally in mapper.tallies.values()),
        rows_late=sum(mapper.late.values()),
        rows_to_write=prepared.rows_to_write(),
        versions_to_write=len(prepared.keys),
        sources_left_out=sorted(mapper.troubles),
        map_ms=round((mapped - started) * 1000.0, 1),
        compare_ms=round((time.perf_counter() - mapped) * 1000.0, 1),
    )
    return prepared
