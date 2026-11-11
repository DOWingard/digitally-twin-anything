"""Checks the manifest's sources against the ontology and decides what a run does.

Nothing is read or written here. Every problem found is collected, so that
one run reports all of them together.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from graphlib import CycleError, TopologicalSorter
from typing import Any

import duckdb
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import Mapping as MappingSpec
from dta_core.manifest.models import PropertyMapping, Source
from dta_core.schema import UID_MAX_LENGTH, FactKind, Field, Kind, ObjectType, Ontology

from .errors import MAPPING_INVALID, UNIT_MISMATCH, Problem, plan_error
from .report import BACKFILL, INCREMENTAL
from .units import Affine, UnitProblem, check_code, conversion

log = Log("sync.plan")

DATASET_SCOPE = "twin_dataset"
# Names DuckDB gives a meaning of its own; a source of that name could not be told apart.
_RESERVED_SOURCE_IDS = {"temp", "memory", "system", "information_schema", "pg_catalog"}
_INTERNAL_PREFIX = "__dta_"
_PROBE_TABLE = "__dta_t"
_PROBE_ALIAS = "__dta_x"
_NUMERIC = (Kind.integer, Kind.float)

RelationKey = tuple[str, str]  # (source id, dataset or view name)


def quote(name: str) -> str:
    """An identifier between double quotes, for DuckDB."""
    return '"' + name.replace('"', '""') + '"'


def relation_sql(source_id: str, name: str) -> str:
    return f"{quote(source_id)}.{quote(name)}"


def dataset_uid(source_id: str, name: str) -> str:
    return f"{DATASET_SCOPE}:{source_id}/{name}"


# -- reading SQL without running it ---------------------------------------------


class SqlProblem(ValueError):
    """A view, expression or filter is not the single statement or expression it must be."""


@dataclass(frozen=True)
class SelectInfo:
    # (schema, table) of every table named, the statement's own CTEs left out.
    tables: tuple[RelationKey, ...]
    columns: frozenset[str]
    star: bool


@dataclass
class _Scan:
    tables: list[tuple[str, str, str]]
    ctes: set[str]
    columns: set[str]
    star: bool = False
    subquery: bool = False
    window: bool = False


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _scan(tree: Any) -> _Scan:
    found = _Scan(tables=[], ctes=set(), columns=set())
    for node in _walk(tree):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            found.ctes.update(entry["key"] for entry in cte_map.get("map") or ())
        if node.get("type") == "BASE_TABLE":
            found.tables.append(
                (
                    node.get("catalog_name") or "",
                    node.get("schema_name") or "",
                    node.get("table_name") or "",
                )
            )
        kind = node.get("class")
        if kind == "COLUMN_REF":
            names = node.get("column_names") or []
            if names:
                # `a.b` is a column of a relation or a field of a column; both are kept.
                found.columns.update((names[0], names[-1]))
        elif kind == "STAR":
            found.star = True
        elif kind == "SUBQUERY":
            found.subquery = True
        elif kind == "WINDOW":
            found.window = True
    return found


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


class SqlInspector:
    """Parses SQL taken from the manifest with DuckDB's own parser, running none of it."""

    def __init__(self) -> None:
        self._con = duckdb.connect(":memory:", config={"enable_external_access": False})
        self._con.execute("SET lock_configuration=true")

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> SqlInspector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _tree(self, sql: str) -> dict[str, Any]:
        try:
            statements = self._con.extract_statements(sql)
        except duckdb.Error as exc:
            raise SqlProblem(_first_line(exc)) from exc
        if not statements:
            raise SqlProblem("is empty")
        if len(statements) != 1:
            raise SqlProblem(f"holds {len(statements)} statements; exactly one is allowed")
        if statements[0].type != duckdb.StatementType.SELECT:
            raise SqlProblem("is not a SELECT statement")
        raw = self._con.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0]
        document = json.loads(raw)
        if document.get("error"):
            raise SqlProblem(str(document.get("error_message") or "cannot be parsed"))
        return document["statements"][0]["node"]

    def select(self, sql: str) -> SelectInfo:
        """What a single SELECT statement refers to."""
        found = _scan(self._tree(sql))
        tables: list[RelationKey] = []
        for catalog, schema, table in found.tables:
            if catalog:
                raise SqlProblem(f'names the catalog {catalog!r}; write "<source>"."<dataset>"')
            if not schema and table in found.ctes:
                continue
            if (schema, table) not in tables:
                tables.append((schema, table))
        return SelectInfo(tuple(tables), frozenset(found.columns), found.star)

    def expression(self, sql: str) -> frozenset[str]:
        """The columns a scalar expression mentions; refuses anything but one expression."""
        if not sql.strip():
            raise SqlProblem("is empty")
        # The line break keeps a trailing `--` comment from swallowing the parenthesis.
        tree = self._tree(f"SELECT (\n{sql}\n) AS {_PROBE_ALIAS} FROM {_PROBE_TABLE}")
        items = tree.get("select_list") or []
        source = tree.get("from_table") or {}
        plain = (
            tree.get("type") == "SELECT_NODE"
            and len(items) == 1
            and items[0].get("alias") == _PROBE_ALIAS
            and source.get("type") == "BASE_TABLE"
            and source.get("table_name") == _PROBE_TABLE
            and not source.get("alias")
            and tree.get("where_clause") is None
            and not tree.get("group_expressions")
            and tree.get("having") is None
            and tree.get("qualify") is None
            and tree.get("sample") is None
            and not tree.get("modifiers")
            and not (tree.get("cte_map") or {}).get("map")
        )
        if not plain:
            raise SqlProblem("is not a single scalar expression")
        found = _scan(items[0])
        if found.subquery or found.tables:
            raise SqlProblem("holds a subquery; join the datasets in a view instead")
        if found.window:
            raise SqlProblem("holds a window function; compute it in a view instead")
        return frozenset(found.columns)


# -- the plan --------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyPlan:
    name: str
    field: Field
    # SQL giving the source value, over the columns of the mapping's dataset.
    sql: str
    affine: Affine | None = None
    ref_scope: str | None = None


@dataclass(frozen=True)
class MappingPlan:
    index: int
    source_id: str
    mapping_id: str
    relation: str
    is_view: bool
    object_type: ObjectType
    identity: tuple[str, ...]
    properties: tuple[PropertyPlan, ...]
    valid_time: str | None
    kind: FactKind
    snapshot: bool
    filter_sql: str | None
    # Columns of the dataset the mapping mentions anywhere.
    mentioned: frozenset[str]

    @property
    def label(self) -> str:
        return f"{self.source_id}.{self.mapping_id}"

    @property
    def where(self) -> str:
        return f"sources.{self.source_id}.mappings.{self.mapping_id}"

    @property
    def timeseries(self) -> bool:
        return self.valid_time is not None

    @property
    def key(self) -> RelationKey:
        return (self.source_id, self.relation)


@dataclass(frozen=True)
class ViewPlan:
    source_id: str
    name: str
    sql: str
    references: tuple[RelationKey, ...]
    columns: frozenset[str]
    star: bool
    # Read by a mapping: evaluated once and kept, rather than once per reader.
    materialised: bool

    @property
    def key(self) -> RelationKey:
        return (self.source_id, self.name)

    @property
    def where(self) -> str:
        return f"sources.{self.source_id}.views.{self.name}"


@dataclass(frozen=True)
class DatasetPlan:
    source_id: str
    name: str
    # Set when every reader of the dataset is a time series over this column, so
    # that a later run can ask the source for newer rows only.
    cursor_column: str | None = None

    @property
    def key(self) -> RelationKey:
        return (self.source_id, self.name)


@dataclass(frozen=True)
class Plan:
    mappings: tuple[MappingPlan, ...]
    # In an order in which each can be created.
    views: tuple[ViewPlan, ...]
    datasets: tuple[DatasetPlan, ...]
    # Width of a backfill interval in microseconds; None loads everything as one version.
    interval_us: int | None
    aggregation: str

    @property
    def kind(self) -> str:
        return BACKFILL if self.interval_us else INCREMENTAL

    def source_ids(self) -> tuple[str, ...]:
        """Sources the run reads, in the order their datasets are read."""
        seen: dict[str, None] = {}
        for dataset in self.datasets:
            seen.setdefault(dataset.source_id)
        return tuple(seen)

    def series_relations(self) -> dict[RelationKey, str]:
        """Dataset or view -> its valid-time column, for everything read as a time series."""
        return {m.key: m.valid_time for m in self.mappings if m.valid_time is not None}


class _Builder:
    def __init__(self, manifest: Manifest, ontology: Ontology, sql: SqlInspector) -> None:
        self.sources: Mapping[str, Source] = manifest.sources or {}
        self.sync = manifest.sync
        self.ontology = ontology
        self.sql = sql
        self.problems: list[Problem] = []

    def problem(self, where: str, message: str, code: str = MAPPING_INVALID) -> None:
        self.problems.append(Problem(code, where, message))

    # -- sync settings --------------------------------------------------------

    def interval(self) -> int | None:
        width = self.sync.backfill_interval if self.sync else None
        if width is None:
            return None
        micros = width // timedelta(microseconds=1)
        if micros <= 0:
            self.problem("sync.backfill_interval", "must be longer than zero")
            return None
        if micros % 1000:
            # Version times keep milliseconds; a finer interval could not be told apart.
            self.problem("sync.backfill_interval", "must be a whole number of milliseconds")
            return None
        return micros

    # -- mappings -------------------------------------------------------------

    def _reference_scope(self, where: str, field: Field) -> str | None:
        try:
            targets = self.ontology.concrete(field.target or "")
        except KeyError:
            self.problem(where, f"refers to {field.target!r}, which the ontology does not define")
            return None
        scopes = {self.ontology.type(name).scope for name in targets}
        if len(scopes) != 1:
            self.problem(
                where,
                f"refers to {field.target}, whose objects do not share one uid scope "
                f"({sorted(scopes) or 'no concrete type'})",
            )
            return None
        return scopes.pop()

    def _unit(self, where: str, field: Field, given: str) -> Affine | None:
        if field.kind not in _NUMERIC:
            self.problem(where, f"a unit is given for a property of kind {field.kind.value}")
            return None
        try:
            if field.unit is None:
                check_code(given)
                log.warn(
                    "a unit is given for a property the ontology gives none; value kept as read",
                    where=where,
                    unit=given,
                )
                return None
            affine = conversion(given, field.unit)
        except UnitProblem as exc:
            self.problem(where, str(exc), UNIT_MISMATCH)
            return None
        return None if affine.is_identity else affine

    def _property(
        self, where: str, object_type: ObjectType, name: str, spec: PropertyMapping
    ) -> tuple[PropertyPlan | None, frozenset[str]]:
        if not object_type.has_field(name):
            self.problem(where, f"{object_type.name} has no property {name}")
            return None, frozenset()
        field = object_type.field(name)
        sound = True
        columns: frozenset[str] = frozenset()
        sql = ""
        if (spec.column is None) == (spec.expression is None):
            self.problem(where, "give exactly one of column and expression")
            sound = False
        elif spec.column is not None:
            sql, columns = quote(spec.column), frozenset((spec.column,))
        else:
            try:
                columns = self.sql.expression(spec.expression or "")
            except SqlProblem as exc:
                self.problem(f"{where}.expression", str(exc))
                sound = False
            sql = f"(\n{spec.expression}\n)"
        before = len(self.problems)
        scope = self._reference_scope(where, field) if field.kind == Kind.ref else None
        affine = self._unit(f"{where}.unit", field, spec.unit) if spec.unit else None
        if not sound or len(self.problems) != before:
            return None, columns
        return PropertyPlan(name, field, sql, affine, scope), columns

    def _object_type(self, where: str, name: str | None) -> ObjectType | None:
        if not name:
            self.problem(f"{where}.object_type", "names no object type")
            return None
        if not self.ontology.has_type(name):
            if name in self.ontology.abstract:
                concrete = ", ".join(self.ontology.abstract[name])
                self.problem(
                    f"{where}.object_type", f"{name} is abstract; map onto one of {concrete}"
                )
            else:
                self.problem(f"{where}.object_type", f"the ontology has no type {name}")
            return None
        object_type = self.ontology.type(name)
        if object_type.meta:
            self.problem(
                f"{where}.object_type", f"{name} is kept by the twin itself and cannot be mapped"
            )
            return None
        return object_type

    def mapping(
        self, index: int, source_id: str, mapping_id: str, spec: MappingSpec
    ) -> MappingPlan | None:
        where = f"sources.{source_id}.mappings.{mapping_id}"
        before = len(self.problems)
        if not spec.dataset:
            self.problem(f"{where}.dataset", "names no dataset")
        object_type = self._object_type(where, spec.object_type)
        identity = tuple(spec.identity or ())
        if not identity or not all(identity):
            self.problem(f"{where}.identity", "names no identity column")
        if spec.kind == FactKind.simulated:
            self.problem(f"{where}.kind", "simulated facts cannot be loaded from a source")
        mentioned: set[str] = set(identity)
        properties: list[PropertyPlan] = []
        if spec.properties is None:
            self.problem(f"{where}.properties", "maps no properties")
        elif object_type is not None:
            for name, property_spec in spec.properties.items():
                planned, columns = self._property(
                    f"{where}.properties.{name}", object_type, name, property_spec
                )
                mentioned |= columns
                if planned is not None:
                    properties.append(planned)
        filter_sql = None
        if spec.filter is not None:
            try:
                mentioned |= self.sql.expression(spec.filter)
                filter_sql = f"(\n{spec.filter}\n)"
            except SqlProblem as exc:
                self.problem(f"{where}.filter", str(exc))
        if spec.valid_time is None:
            if spec.cursor:
                log.warn(
                    "cursor is used only by a mapping with valid_time; the dataset is read whole",
                    where=where,
                )
        else:
            mentioned.add(spec.valid_time)
            if spec.cursor and spec.cursor != spec.valid_time:
                self.problem(
                    f"{where}.cursor",
                    "must be the valid_time column: reading a time series by another "
                    "column cannot be continued after an interrupted run",
                )
            if spec.properties is not None and not spec.properties:
                self.problem(f"{where}.properties", "a time series maps at least one property")
            if spec.mode == "snapshot":
                self.problem(
                    f"{where}.mode",
                    "snapshot cannot be used with valid_time: rows absent from a time "
                    "series are not deletions",
                )
        if len(self.problems) != before or object_type is None:
            return None
        return MappingPlan(
            index=index,
            source_id=source_id,
            mapping_id=mapping_id,
            relation=spec.dataset or "",
            is_view=(spec.dataset or "") in (self.sources[source_id].views or {}),
            object_type=object_type,
            identity=identity,
            properties=tuple(properties),
            valid_time=spec.valid_time,
            kind=spec.kind,
            snapshot=spec.mode == "snapshot",
            filter_sql=filter_sql,
            mentioned=frozenset(mentioned),
        )

    def mappings(self) -> list[MappingPlan]:
        planned: list[MappingPlan] = []
        feeding: dict[str, int] = {}
        snapshots: list[tuple[str, str]] = []
        for source_id, source in self.sources.items():
            for mapping_id, spec in (source.mappings or {}).items():
                if spec.object_type:
                    feeding[spec.object_type] = feeding.get(spec.object_type, 0) + 1
                if spec.mode == "snapshot" and spec.object_type:
                    snapshots.append(
                        (f"sources.{source_id}.mappings.{mapping_id}.mode", spec.object_type)
                    )
                found = self.mapping(len(planned), source_id, mapping_id, spec)
                if found is not None:
                    planned.append(found)
        for where, type_name in snapshots:
            if feeding[type_name] != 1:
                self.problem(
                    where,
                    f"snapshot needs to be the only mapping of {type_name}, which "
                    f"{feeding[type_name]} mappings feed: an object absent from one of them "
                    "may come from another",
                )
        series: dict[RelationKey, set[str]] = {}
        for mapping in planned:
            if mapping.valid_time is not None:
                series.setdefault(mapping.key, set()).add(mapping.valid_time)
        for (source_id, relation), columns in series.items():
            if len(columns) > 1:
                self.problem(
                    f"sources.{source_id}",
                    f"the time-series mappings of {relation} name different valid_time "
                    f"columns ({', '.join(sorted(columns))}); a dataset is read up to one time",
                )
        return planned

    # -- views ----------------------------------------------------------------

    def views(self, mappings: Iterable[MappingPlan]) -> list[ViewPlan]:
        parsed: dict[RelationKey, tuple[str, SelectInfo, list[RelationKey]]] = {}
        for source_id, source in self.sources.items():
            for name, text in (source.views or {}).items():
                where = f"sources.{source_id}.views.{name}"
                try:
                    info = self.sql.select(text or "")
                except SqlProblem as exc:
                    self.problem(where, str(exc))
                    continue
                references: list[RelationKey] = []
                for schema, table in info.tables:
                    if not schema:
                        self.problem(
                            where,
                            f'refers to {table} without its source; write "<source>"."{table}"',
                        )
                    elif schema not in self.sources:
                        self.problem(
                            where, f"refers to source {schema!r}, which the manifest does not have"
                        )
                    else:
                        references.append((schema, table))
                parsed[(source_id, name)] = (text.strip().rstrip(";"), info, references)
        read_directly = {mapping.key for mapping in mappings if mapping.is_view}
        needed: set[RelationKey] = set()
        pending = [key for key in read_directly if key in parsed]
        while pending:
            key = pending.pop()
            if key in needed:
                continue
            needed.add(key)
            pending.extend(ref for ref in parsed[key][2] if ref in parsed)
        sorter: TopologicalSorter[RelationKey] = TopologicalSorter()
        for key in sorted(needed):
            sorter.add(key, *(ref for ref in parsed[key][2] if ref in needed))
        try:
            order = list(sorter.static_order())
        except CycleError as exc:
            cycle = " -> ".join(f"{source}.{name}" for source, name in exc.args[1])
            self.problem("sources", f"views refer to each other in a circle: {cycle}")
            return []
        return [
            ViewPlan(
                source_id=key[0],
                name=key[1],
                sql=parsed[key][0],
                references=tuple(parsed[key][2]),
                columns=parsed[key][1].columns,
                star=parsed[key][1].star,
                materialised=key in read_directly,
            )
            for key in order
        ]

    # -- datasets -------------------------------------------------------------

    def datasets(self, mappings: list[MappingPlan], views: list[ViewPlan]) -> list[DatasetPlan]:
        view_keys = {view.key for view in views}
        under_views = {ref for view in views for ref in view.references if ref not in view_keys}
        direct: dict[RelationKey, list[MappingPlan]] = {}
        for mapping in mappings:
            if not mapping.is_view:
                direct.setdefault(mapping.key, []).append(mapping)
        order = {source_id: position for position, source_id in enumerate(self.sources)}
        planned = []
        for key in sorted({*direct, *under_views}, key=lambda k: (order[k[0]], k[1])):
            readers = direct.get(key, [])
            times = {mapping.valid_time for mapping in readers}
            # A view may need rows a time series has already passed, so a dataset
            # under a view is always read whole.
            incremental = key not in under_views and len(times) == 1 and None not in times
            planned.append(DatasetPlan(key[0], key[1], times.pop() if incremental else None))
        return planned

    def check_names(self, datasets: list[DatasetPlan], mappings: list[MappingPlan]) -> None:
        for source_id in {dataset.source_id for dataset in datasets}:
            if source_id in _RESERVED_SOURCE_IDS or source_id.startswith(_INTERNAL_PREFIX):
                self.problem(
                    f"sources.{source_id}", "the name is reserved; give the source another"
                )
        recorded = {dataset.key for dataset in datasets} | {m.key for m in mappings if m.timeseries}
        for source_id, name in sorted(recorded):
            if len(dataset_uid(source_id, name)) > UID_MAX_LENGTH:
                self.problem(
                    f"sources.{source_id}",
                    f"the name of {name} is too long to be recorded "
                    f"(more than {UID_MAX_LENGTH} characters with its source)",
                )


def build_plan(manifest: Manifest, ontology: Ontology) -> Plan:
    """What a run reads, maps and writes; raises with every problem of the manifest."""
    with SqlInspector() as sql:
        builder = _Builder(manifest, ontology, sql)
        interval_us = builder.interval()
        mappings = builder.mappings()
        views = builder.views(mappings)
        datasets = builder.datasets(mappings, views)
        builder.check_names(datasets, mappings)
    if builder.problems:
        log.error(
            "the manifest's sources cannot be loaded as written",
            problems=[(p.code, p.where, p.message) for p in builder.problems],
        )
        raise plan_error(builder.problems)
    aggregation = manifest.sync.aggregation if manifest.sync else "last"
    log.debug(
        "plan made",
        mappings=[mapping.label for mapping in mappings],
        views=[f"{view.source_id}.{view.name}" for view in views],
        datasets=[f"{dataset.source_id}.{dataset.name}" for dataset in datasets],
        interval_us=interval_us,
        aggregation=aggregation,
    )
    return Plan(tuple(mappings), tuple(views), tuple(datasets), interval_us, aggregation)


def unmapped_columns(plan: Plan, columns: Mapping[RelationKey, Iterable[str]]) -> list[str]:
    """Columns of the datasets read that no mapping, view or expression mentions.

    A view that selects `*` is taken to use every column of the datasets it names.
    """
    mentioned: dict[RelationKey, set[str]] = {key: set() for key in columns}
    starred: set[RelationKey] = set()
    for mapping in plan.mappings:
        if not mapping.is_view and mapping.key in mentioned:
            mentioned[mapping.key] |= mapping.mentioned
    for view in plan.views:
        for reference in view.references:
            if reference in mentioned:
                mentioned[reference] |= view.columns
                if view.star:
                    starred.add(reference)
    return sorted(
        f"{key[0]}.{key[1]}.{column}"
        for key, names in columns.items()
        if key not in starred
        for column in names
        if column not in mentioned[key]
    )
