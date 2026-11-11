"""Maps the datasets of a run to ontology objects and works out what is new.

Everything happens in one in-memory DuckDB session that can reach nothing
outside the process. Each mapping is one query; comparing with what is stored
and reducing a time series to one value per interval are queries too. Rows
become Python values only when a version's change set is handed to the store.

The session works in UTC whatever the host's time zone is, so that a key or a
date derived from a timestamp is the same wherever the twin runs.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import UID_FIELD, UID_MAX_LENGTH, Field, Kind, ObjectType
from dta_core.store import Store, VersionRef

from .errors import MAPPING_INVALID, MappingInvalid, Problem
from .extract import DatasetRead
from .plan import MappingPlan, Plan, PropertyPlan, RelationKey, quote, relation_sql

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
# Kinds whose values can be averaged, and those that have an order.
_ORDERED = (Kind.float, Kind.integer, Kind.datetime, Kind.date)
_INTEGRAL_TOLERANCE = 1e-9
_LAMBDA = "__dta_e"
_EXAMPLES = 5


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _double(value: float) -> str:
    # With an exponent DuckDB reads the literal as a double, digit for digit.
    return f"{value:.16e}"


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
        found = self._con.execute(sql, params or []).fetchone()
        return found[0] if found else None

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
    # Objects (static) or objects per interval (time series) that could be written.
    candidates: int = 0


@dataclass
class Prepared:
    """A run mapped and compared, ready to be written."""

    session: Session
    plan: Plan
    batches: dict[str, TypeBatch]
    # Interval keys with something to write, ascending. Microseconds since the
    # epoch when the run has a time series and a backfill interval; else just 0.
    keys: list[int]
    # Whether keys are times. False when everything goes into one version.
    timed: bool
    # Key of the version that takes the static rows and the deletions: the first interval.
    static_key: int
    tallies: dict[int, Tally]
    # Per mapping: rows set aside while mapping (`uid`, `vt` and one flag per reason).
    set_aside: dict[int, pa.Table]
    # Per mapping: uids that several differing rows claim, with the number of rows.
    duplicates: dict[int, pa.Table]
    # Latest valid time read from each dataset or view that feeds a time series.
    cursors: dict[RelationKey, datetime]
    # Rows of each dataset read and of each view a mapping reads.
    relation_rows: dict[RelationKey, int]
    columns: dict[RelationKey, list[str]]

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
        stored_cursors: Mapping[RelationKey, datetime],
    ) -> None:
        self.session = session
        self.plan = plan
        self.reads = reads
        self.store = store
        self.head = head
        self.stored_cursors = stored_cursors
        self.problems: list[Problem] = []
        self.failed: set[RelationKey] = set()
        self.raw_types: dict[int, list[str]] = {}
        self.tallies: dict[int, Tally] = {m.index: Tally() for m in plan.mappings}
        self.set_aside: dict[int, pa.Table] = {}
        self.duplicates: dict[int, pa.Table] = {}
        self.cursors: dict[RelationKey, datetime] = {}
        self.relation_rows: dict[RelationKey, int] = {}
        self.stored: dict[str, str] = {}
        self.slots: dict[str, list[Slot]] = {}

    def problem(self, where: str, message: str) -> None:
        self.problems.append(Problem(MAPPING_INVALID, where, message))

    def check(self) -> None:
        if self.problems:
            log.error(
                "mappings cannot be evaluated against the data read",
                problems=[(p.where, p.message) for p in self.problems],
            )
            raise MappingInvalid(self.problems)

    # -- datasets and views -----------------------------------------------------

    def register(self) -> None:
        schemas = {dataset.source_id for dataset in self.plan.datasets}
        schemas |= {view.source_id for view in self.plan.views}
        for schema in sorted(schemas):
            self.session.run(f"CREATE SCHEMA IF NOT EXISTS {quote(schema)}")
        for key, read in self.reads.items():
            name = self.session.register(read.table)
            self.session.run(f"CREATE VIEW {relation_sql(*key)} AS SELECT * FROM {name}")
            self.relation_rows[key] = read.table.num_rows
        for view in self.plan.views:
            if any(reference in self.failed for reference in view.references):
                # Already reported at the view it depends on.
                self.failed.add(view.key)
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
                self.problem(view.where, explain(exc))
                continue
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
            self.problem(mapping.where, explain(exc))
            return
        self.raw_types[mapping.index] = [
            described[f"__dta_r{j}"] for j in range(len(mapping.properties))
        ]
        time_type = described.get("__dta_rvt")
        if time_type is not None and not time_type.startswith(_TIME_TYPES):
            self.problem(
                f"{mapping.where}.valid_time",
                f"{mapping.valid_time} holds {time_type}, which is not a time",
            )

    def _mapping_sql(self, mapping: MappingPlan, since: datetime | None) -> str:
        where = []
        if mapping.filter_sql:
            where.append(mapping.filter_sql)
        if since is not None and mapping.valid_time is not None:
            where.append(f"TRY_CAST({quote(mapping.valid_time)} AS TIMESTAMPTZ) > ?")
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
        bad = ["bk"] + (["bvt"] if mapping.valid_time is not None else [])
        types = self.raw_types[mapping.index]
        for j, prop in enumerate(mapping.properties):
            value, wrong = _value(prop, f"__dta_r{j}", types[j])
            cast.append(f"{value} AS v{j}")
            flags.append(f"coalesce({wrong.replace('{v}', f'v{j}')}, FALSE) AS b{j}")
            final.append(f"v{j}")
            bad.append(f"b{j}")
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
        since = self.stored_cursors.get(mapping.key) if mapping.timeseries else None
        table = f"__dta_m{mapping.index}"
        started = time.perf_counter()
        try:
            self.session.run(
                f"CREATE TEMP TABLE {table} AS {self._mapping_sql(mapping, since)}",
                [since] if since is not None else None,
            )
        except duckdb.Error as exc:
            log.debug("mapping failed", mapping=mapping.label, error=str(exc))
            self.problem(mapping.where, explain(exc))
            return
        tally = self.tallies[mapping.index]
        tally.rows, tally.rejected = self.session.rows(
            f"SELECT count(*), count(*) FILTER (WHERE bad) FROM {table}"
        )[0]
        if tally.rejected:
            flags = ["bk"] + (["bvt"] if mapping.timeseries else [])
            flags += [f"b{j}" for j in range(len(mapping.properties))]
            moment = "vt" if mapping.timeseries else "CAST(NULL AS TIMESTAMPTZ) AS vt"
            self.set_aside[mapping.index] = self.session.arrow(
                f"SELECT uid, {moment}, {', '.join(flags)} FROM {table} WHERE bad"
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

    def read_cursors(self) -> None:
        """Latest valid time of each dataset or view a time series is read from."""
        for key, column in self.plan.series_relations().items():
            if key in self.failed:
                continue
            moment = f"TRY_CAST({quote(column)} AS TIMESTAMPTZ)"
            since = self.stored_cursors.get(key)
            sql = f"SELECT max({moment}) AS latest FROM {relation_sql(*key)}"
            try:
                if since is not None:
                    found = self.session.arrow(f"{sql} WHERE {moment} > ?", [since])
                else:
                    found = self.session.arrow(sql)
            except duckdb.Error as exc:
                # Reported where the mapping itself fails to bind.
                log.debug("latest valid time cannot be read", relation=key, error=str(exc))
                continue
            latest = found.column("latest")[0].as_py()
            if latest is not None:
                self.cursors[key] = latest.astimezone(UTC)

    # -- what is stored ---------------------------------------------------------

    def read_stored(self) -> None:
        """The head state of every type a mapping writes: one read per type."""
        names: dict[str, set[str]] = {}
        types: dict[str, ObjectType] = {}
        for mapping in self.plan.mappings:
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
            for mapping in self.plan.mappings
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

    # -- static mappings ----------------------------------------------------------

    def static(self, mapping: MappingPlan) -> None:
        """Rows of a static mapping that are new or differ from what is stored."""
        index = mapping.index
        values = [f"v{j}" for j in range(len(mapping.properties))]
        columns = ", ".join([UID_FIELD, *values])
        self.session.run(
            f"CREATE TEMP TABLE __dta_g{index} AS SELECT {columns} FROM ("
            f"SELECT *, count(*) OVER (PARTITION BY uid) AS n FROM ("
            f"SELECT DISTINCT {columns} FROM __dta_m{index} WHERE NOT bad)) WHERE n = 1"
        )
        duplicates = self.session.arrow(
            f"SELECT uid, count(*) AS n FROM __dta_m{index} WHERE NOT bad AND uid NOT IN "
            f"(SELECT uid FROM __dta_g{index}) GROUP BY uid ORDER BY uid"
        )
        tally = self.tallies[index]
        if duplicates.num_rows:
            self.duplicates[index] = duplicates
            rows = sum(duplicates.column("n").to_pylist())
            tally.rejected += rows
            log.warn(
                "rows set aside: several rows that differ claim one identity",
                mapping=mapping.label,
                identities=duplicates.num_rows,
                rows=rows,
            )
        tally.candidates = self.session.scalar(f"SELECT count(*) FROM __dta_g{index}")
        stored = self.stored[mapping.object_type.name]
        same = ["s.uid = g.uid"]
        for j, prop in enumerate(mapping.properties):
            slot = self._slot(mapping, prop)
            same.append(
                f"{slot.comparable('s.' + quote(prop.name))} IS NOT DISTINCT FROM "
                f"{slot.comparable(f'g.v{j}')}"
            )
        self.session.run(
            f"CREATE TEMP TABLE __dta_n{index} AS SELECT g.* FROM __dta_g{index} g "
            f"WHERE NOT EXISTS (SELECT 1 FROM {stored} s WHERE {' AND '.join(same)})"
        )

    def deletes(self, mapping: MappingPlan) -> list[str]:
        """Stored objects of a snapshot mapping's type that its dataset no longer holds."""
        stored = self.stored[mapping.object_type.name]
        found = self.session.arrow(
            f"SELECT s.uid FROM {stored} s WHERE NOT EXISTS "
            f"(SELECT 1 FROM __dta_m{mapping.index} m WHERE m.uid = s.uid) ORDER BY s.uid"
        )
        return found.column("uid").to_pylist()

    # -- time series ------------------------------------------------------------

    def series(self, type_name: str, slot: Slot, position: int) -> None:
        """One row per object and interval of a property, with what it is compared to."""
        feeders = [(m, j) for m, j in self._feeders(type_name, slot.field.name) if m.timeseries]
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
        """A property a static mapping writes must come from that mapping alone.

        The static value is compared with the stored one on every run, so a
        second writer would have each run undo what the other wrote.
        """
        feeders = self._feeders(type_name, slot.field.name)
        static = [mapping for mapping, _ in feeders if not mapping.timeseries]
        if not static or len(feeders) < 2:
            return
        parts = [
            f"SELECT uid, {mapping.index} AS m FROM __dta_g{mapping.index}" for mapping in static
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

    # -- one table per type -----------------------------------------------------

    def batch(self, type_name: str, object_type: ObjectType, static_key: int) -> TypeBatch:
        slots = self.slots[type_name]
        parts = []
        for mapping in self.plan.mappings:
            if mapping.object_type.name != type_name or mapping.timeseries:
                continue
            columns = [
                f"CAST({static_key} AS BIGINT) AS tk",
                UID_FIELD,
                f"CAST({mapping.index} AS INTEGER) AS m",
            ]
            for j, prop in enumerate(mapping.properties):
                k = self._slot(mapping, prop).index
                columns += [
                    f"v{j} AS v_{k}",
                    f"TRUE AS w_{k}",
                    f"CAST(NULL AS TIMESTAMPTZ) AS tv_{k}",
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
    stored_cursors: Mapping[RelationKey, datetime],
) -> Prepared:
    """Map every dataset read and compare the result with the stored head state.

    Raises `sync.mapping_invalid`, with every problem found, when a view or a
    mapping cannot be evaluated against the data, or when two mappings claim
    the same property of the same object.
    """
    session = Session()
    try:
        return _prepare(session, plan, reads, store, head, stored_cursors)
    except BaseException:
        session.close()
        raise


def _prepare(
    session: Session,
    plan: Plan,
    reads: Mapping[RelationKey, DatasetRead],
    store: Store,
    head: VersionRef,
    stored_cursors: Mapping[RelationKey, datetime],
) -> Prepared:
    started = time.perf_counter()
    mapper = _Mapper(session, plan, reads, store, head, stored_cursors)
    mapper.register()
    for mapping in plan.mappings:
        mapper.describe(mapping)
    mapper.check()
    mapper.read_cursors()
    for mapping in plan.mappings:
        mapper.map(mapping)
    mapper.check()
    mapped = time.perf_counter()

    mapper.read_stored()
    mapper.structures()
    for mapping in plan.mappings:
        if mapping.timeseries:
            mapper.series_candidates(mapping)
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
    mapper.check()

    first_key = None
    if reduced:
        first_key = session.scalar("SELECT min(tk) FROM (" + " UNION ALL ".join(reduced) + ")")
    timed = plan.interval_us is not None and first_key is not None
    static_key = int(first_key) if timed else 0
    batches: dict[str, TypeBatch] = {}
    keys: set[int] = set()
    types = {m.object_type.name: m.object_type for m in plan.mappings}
    for type_name, object_type in types.items():
        batch = mapper.batch(type_name, object_type, static_key)
        for mapping in plan.mappings:
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
        batches=batches,
        keys=sorted(keys),
        timed=timed,
        static_key=static_key,
        tallies=mapper.tallies,
        set_aside=mapper.set_aside,
        duplicates=mapper.duplicates,
        cursors=mapper.cursors,
        relation_rows=mapper.relation_rows,
        columns={key: list(read.table.column_names) for key, read in reads.items()},
    )
    log.info(
        "mapped and compared",
        mappings=len(plan.mappings),
        rows_mapped=sum(tally.rows for tally in mapper.tallies.values()),
        rows_set_aside=sum(tally.rejected for tally in mapper.tallies.values()),
        rows_to_write=prepared.rows_to_write(),
        versions_to_write=len(prepared.keys),
        map_ms=round((mapped - started) * 1000.0, 1),
        compare_ms=round((time.perf_counter() - mapped) * 1000.0, 1),
    )
    return prepared
