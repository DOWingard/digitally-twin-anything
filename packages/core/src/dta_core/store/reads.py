"""Reads of the graph at a version, and of what changed between versions.

Every statement names the commit it reads, so a read is unaffected by commits
made while it runs and never waits for a writer. A version is read under the
ontology that was in force when it was written; the result is shaped by the
ontology the caller asks for, with nulls where a property did not exist yet.
Each bulk read is one statement per table.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from ..log import Log
from ..schema import UID_FIELD, Kind, ObjectType, Ontology
from . import codec, ddl
from .boundary import public
from .db import Conn, Database
from .errors import UnknownProperty, UnknownType, invalid
from .index import Chain
from .model import LIVE_BRANCH, Cause, Provenance, StoredObject, Version, VersionRef
from .selector import Selector
from .settings import StoreSettings

log = Log("store.read")

VERSION_COLUMN = "_version"
SEQ_COLUMN = "_seq"
TIME_COLUMN = "_t"
CHANGE_COLUMN = "_change"
BEFORE_COLUMN = "_before"
PROV_COLUMN = codec.PROV_COLUMN

ADDED, MODIFIED, REMOVED = "added", "modified", "removed"
ALL_VERSIONS = "#0..head"

_TIME_TYPE = pa.timestamp("us", tz="UTC")
_COMPARISONS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
# Above this many versions a range read scans the history once instead of naming each commit.
_NAMED_COMMITS = 1000
_NO_LIMIT = 2**63 - 1

At = str | Selector | VersionRef
Filter = Mapping[str, Any] | Sequence[tuple[str, str, Any]]


def _transpose(rows: Sequence[Sequence[Any]], width: int) -> list[Sequence[Any]]:
    return list(zip(*rows, strict=True)) if rows else [()] * width


def _effective(provenance: Mapping[str, Any], name: str) -> Any:
    return provenance.get(name) or provenance.get("*")


class Reads:
    _db: Database
    _ontology: Ontology
    _settings: StoreSettings

    # Provided by the store.
    def _ready(self) -> None:
        raise NotImplementedError

    def _resolve(self, conn: Conn, selector: At, branch: str) -> list[Version]:
        raise NotImplementedError

    def _one(self, conn: Conn, selector: At, branch: str) -> Version:
        raise NotImplementedError

    def _ontology_of(self, conn: Conn, version: Version) -> Ontology:
        raise NotImplementedError

    def _chain(self, conn: Conn, branch: str) -> Chain:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------

    @contextmanager
    def _reading(self, operation: str, **context: Any) -> Iterator[Conn]:
        self._ready()
        started = time.perf_counter()
        with self._db.connect() as conn:
            yield conn
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if elapsed_ms > self._settings.slow_ms:
            log.warn("slow read", operation=operation, elapsed_ms=elapsed_ms, **context)
        else:
            log.debug("read", operation=operation, elapsed_ms=elapsed_ms, **context)

    def _requested_type(self, ontology: Ontology | None, name: str) -> ObjectType:
        requested = ontology or self._ontology
        if not isinstance(name, str) or not requested.has_type(name):
            raise UnknownType(f"unknown type {name!r}", detail={"type": name})
        return requested.type(name)

    @staticmethod
    def _names(object_type: ObjectType, columns: Sequence[str] | None) -> list[str]:
        if columns is None:
            return [f.name for f in object_type.fields]
        names: list[str] = []
        for name in columns:
            if name == UID_FIELD or name in names:
                continue
            if not isinstance(name, str) or not object_type.has_field(name):
                raise UnknownProperty(
                    f"{object_type.name} has no property {name!r}",
                    detail={"type": object_type.name, "property": name},
                )
            names.append(name)
        return names

    @staticmethod
    def _value_columns(
        object_type: ObjectType, names: Sequence[str], present: Sequence[str], cells: Any, rows: int
    ) -> list[pa.Array]:
        """Arrow columns for `names`; `cells` holds one sequence per name in `present`."""
        position = {name: index for index, name in enumerate(present)}
        arrays = []
        for name in names:
            field = object_type.field(name)
            if name in position:
                arrays.append(codec.column(field, cells[position[name]]))
            else:
                arrays.append(pa.nulls(rows, codec.arrow_type(field)))
        return arrays

    def _where(
        self, requested: ObjectType, stored: ObjectType, conditions: Filter | None
    ) -> tuple[str, list[Any], bool]:
        """(SQL, parameters, whether the filter can match anything at this version)."""
        if not conditions:
            return "", [], True
        if isinstance(conditions, Mapping):
            triples = []
            for name, value in conditions.items():
                if value is None:
                    triples.append((name, "is_null", None))
                elif isinstance(value, list | tuple | set | frozenset):
                    triples.append((name, "in", list(value)))
                else:
                    triples.append((name, "=", value))
        else:
            triples = list(conditions)
        clauses: list[str] = []
        parameters: list[Any] = []
        for condition in triples:
            if not isinstance(condition, tuple | list) or len(condition) != 3:
                raise invalid("bad_filter", "a filter condition is (property, operator, value)")
            name, operator, value = condition
            if name == UID_FIELD:
                field = None
            elif isinstance(name, str) and requested.has_field(name):
                field = requested.field(name)
                if field.stored_as_json:
                    raise invalid("bad_filter", f"{name} holds structured values; cannot filter")
            else:
                raise UnknownProperty(
                    f"{requested.name} has no property {name!r}",
                    detail={"type": requested.name, "property": name},
                )

            def bind(item: Any, field: Any = field, name: Any = name) -> Any:
                if field is None:
                    if not isinstance(item, str):
                        raise invalid("bad_filter", "a uid filter takes text")
                    return item
                try:
                    return codec.encode(field, item)
                except ValueError as exc:
                    raise invalid("bad_filter", f"filter on {name}: {exc}") from exc

            exists = field is None or stored.has_field(name)
            column = f"`{UID_FIELD}`" if field is None else ddl.quote(name)
            if operator == "is_null":
                if exists:
                    clauses.append(f"{column} IS NULL")
            elif not exists:
                # The property did not exist at this version, so it is null in every row.
                return "", [], False
            elif operator == "not_null":
                clauses.append(f"{column} IS NOT NULL")
            elif operator == "in":
                if isinstance(value, str) or not isinstance(value, list | tuple | set | frozenset):
                    raise invalid("bad_filter", f"filter on {name}: 'in' takes a list")
                items = [bind(item) for item in value]
                if not items:
                    return "", [], False
                clauses.append(f"{column} IN ({', '.join(['%s'] * len(items))})")
                parameters.extend(items)
            elif operator in _COMPARISONS:
                if value is None:
                    raise invalid("bad_filter", f"filter on {name}: compare with is_null instead")
                clauses.append(f"{column} {_COMPARISONS[operator]} %s")
                parameters.append(bind(value))
            else:
                raise invalid("bad_filter", f"unknown filter operator {operator!r}")
        if not clauses:
            return "", [], True
        return " WHERE " + " AND ".join(clauses), parameters, True

    # -- one object ---------------------------------------------------------

    @public
    def get(
        self,
        uid: str,
        at: At = "head",
        branch: str = LIVE_BRANCH,
        ontology: Ontology | None = None,
    ) -> StoredObject | None:
        """The object with this uid at a version, or None when it did not exist then."""
        with self._reading("get") as conn:
            version = self._one(conn, at, branch)
            type_name = conn.scalar(
                f"SELECT `type` FROM `{ddl.OBJECT_TABLE}` AS OF %s WHERE `{UID_FIELD}` = %s",
                (version.id, uid),
            )
            if type_name is None:
                return None
            stored = self._ontology_of(conn, version).type(type_name)
            requested = ontology or self._ontology
            shape = requested.type(type_name) if requested.has_type(type_name) else stored
            present = [f.name for f in shape.fields if stored.has_field(f.name)]
            select = ", ".join(["`_prov`", *(ddl.quote(name) for name in present)])
            rows = conn.query(
                f"SELECT {select} FROM {ddl.quote(stored.table)} AS OF %s WHERE `{UID_FIELD}` = %s",
                (version.id, uid),
            )
        if not rows:
            return None
        row = rows[0]
        values: dict[str, Any] = {f.name: None for f in shape.fields}
        for name, cell in zip(present, row[1:], strict=True):
            values[name] = codec.decode(shape.field(name), cell)
        provenance = {
            name: Provenance.from_json(entry) for name, entry in json.loads(row[0]).items()
        }
        return StoredObject(uid, type_name, values, provenance, version.ref)

    # -- one type -----------------------------------------------------------

    @public
    def table(
        self,
        type: str,
        columns: Sequence[str] | None = None,
        filter: Filter | None = None,
        limit: int | None = None,
        offset: int | None = None,
        at: At = "head",
        branch: str = LIVE_BRANCH,
        prov: bool = False,
        ontology: Ontology | None = None,
    ) -> pa.Table:
        """Every object of a type at a version: `uid`, then the properties asked for."""
        requested = self._requested_type(ontology, type)
        names = self._names(requested, columns)
        for label, number in (("limit", limit), ("offset", offset)):
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int) or number < 0
            ):
                raise invalid("bad_filter", f"{label} must be a non-negative integer")
        fields = codec.arrow_fields(requested, [UID_FIELD, *names, *([PROV_COLUMN] * prov)])
        schema = pa.schema(fields)
        with self._reading("table", type=type) as conn:
            version = self._one(conn, at, branch)
            stored_ontology = self._ontology_of(conn, version)
            if not stored_ontology.has_type(type):
                return schema.empty_table()
            stored = stored_ontology.type(type)
            present = [name for name in names if stored.has_field(name)]
            where, parameters, possible = self._where(requested, stored, filter)
            if not possible:
                return schema.empty_table()
            select = [f"`{UID_FIELD}`", *(ddl.quote(name) for name in present)]
            if prov:
                select.append("`_prov`")
            tail = ""
            if limit is not None or offset:
                # Paging needs a fixed order; the primary key gives one.
                tail = f" ORDER BY `{UID_FIELD}` LIMIT %s OFFSET %s"
                parameters = [*parameters, _NO_LIMIT if limit is None else limit, offset or 0]
            rows = conn.query(
                f"SELECT {', '.join(select)} FROM {ddl.quote(stored.table)} AS OF %s{where}{tail}",
                [version.id, *parameters],
            )
        cells = _transpose(rows, len(select))
        arrays = [codec.text_column(cells[0])]
        arrays.extend(self._value_columns(requested, names, present, cells[1:], len(rows)))
        if prov:
            arrays.append(codec.text_column(cells[-1]))
        return pa.Table.from_arrays(arrays, schema=schema)

    # -- changes over a run of versions ---------------------------------------

    def _change_schema(
        self, object_type: ObjectType | None, names: Sequence[str], prov: bool
    ) -> pa.Schema:
        fields = [
            pa.field(VERSION_COLUMN, pa.string()),
            pa.field(SEQ_COLUMN, pa.int64()),
            pa.field(TIME_COLUMN, _TIME_TYPE),
            pa.field(CHANGE_COLUMN, pa.string()),
            pa.field(UID_FIELD, pa.string()),
        ]
        if object_type is not None:
            fields.extend(codec.arrow_fields(object_type, [*names, *([PROV_COLUMN] * prov)]))
        return pa.schema(fields)

    def _change_table(
        self,
        schema: pa.Schema,
        object_type: ObjectType,
        names: Sequence[str],
        present: Sequence[str],
        prov: bool,
        records: list[tuple[Any, ...]],
    ) -> pa.Table:
        """Records are (version, change, uid, cells of `present`..., provenance text)."""
        if not records:
            return schema.empty_table()
        records.sort(key=lambda record: (record[0].seq, record[2]))
        cells = _transpose(records, 3 + len(present) + 1)
        versions = cells[0]
        arrays = [
            pa.array([version.id for version in versions], pa.string()),
            pa.array([version.seq for version in versions], pa.int64()),
            pa.array([version.t for version in versions], _TIME_TYPE),
            pa.array(cells[1], pa.string()),
            pa.array(cells[2], pa.string()),
        ]
        arrays.extend(
            self._value_columns(
                object_type, names, present, cells[3 : 3 + len(present)], len(records)
            )
        )
        if prov:
            arrays.append(codec.text_column(cells[-1]))
        return pa.Table.from_arrays(arrays, schema=schema)

    def _history_table(self, last: Version, object_type: ObjectType) -> str:
        """The change log of a table as seen from one version, whatever branch is current."""
        return f"`{self._db.name}/{last.id}`.`dolt_diff_{object_type.table}`"

    def _type_of(self, conn: Conn, uid: str, last: Version, stored: Ontology) -> str | None:
        """The type to read the history of an object from when it is gone at `last`."""
        candidates = [t.name for t in stored.types if uid.startswith(f"{t.scope}:")]
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        chain = self._chain(conn, last.branch)
        rows = conn.query(
            "SELECT `to_commit`, `from_type` FROM "
            f"`{self._db.name}/{last.id}`.`dolt_diff_{ddl.OBJECT_TABLE}` "
            f"WHERE `from_{UID_FIELD}` = %s AND `diff_type` = %s",
            (uid, REMOVED),
        )
        latest: tuple[int, str] | None = None
        for commit, type_name in rows:
            seq = chain.by_id.get(commit)
            if seq is not None and (latest is None or seq > latest[0]):
                latest = (seq, type_name)
        return latest[1] if latest else None

    @public
    def history(
        self,
        uid: str,
        properties: Sequence[str] | None = None,
        range: At = ALL_VERSIONS,
        branch: str = LIVE_BRANCH,
        type: str | None = None,
        prov: bool = False,
        ontology: Ontology | None = None,
    ) -> pa.Table:
        """One row per version of the range in which the object changed.

        The row holds the object as it was after the change. With `properties`
        given, only versions in which one of them changed value are returned
        (or changed provenance, when `prov` is set). The history is that of
        one type: the object's type at the end of the range, its last type if
        it is gone by then, or `type` when given.
        """
        with self._reading("history") as conn:
            versions = self._resolve(conn, range, branch)
            last = versions[-1]
            chain = self._chain(conn, last.branch)
            stored_ontology = self._ontology_of(conn, last)
            type_at_end = conn.scalar(
                f"SELECT `type` FROM `{ddl.OBJECT_TABLE}` AS OF %s WHERE `{UID_FIELD}` = %s",
                (last.id, uid),
            )
            type_name = type or type_at_end or self._type_of(conn, uid, last, stored_ontology)
            if type_name is None:
                return self._change_schema(None, (), False).empty_table()
            requested = self._requested_type(ontology, type_name)
            names = self._names(requested, properties)
            schema = self._change_schema(requested, names, prov)
            if not stored_ontology.has_type(type_name):
                return schema.empty_table()
            stored = stored_ontology.type(type_name)
            present = [name for name in names if stored.has_field(name)]
            source = self._history_table(last, stored)
            select = ["`to_commit`", "`from_commit`", "`diff_type`", "`to__prov`", "`from__prov`"]
            select.extend(f"`to_{name}`" for name in present)
            select.extend(f"`from_{name}`" for name in present)
            rows = conn.query(
                f"SELECT {', '.join(select)} FROM {source} WHERE `to_{UID_FIELD}` = %s", (uid,)
            )
            wanted = {version.seq for version in versions}
            width = len(present)
            records: list[tuple[Any, ...]] = []
            additions = 0
            for row in rows:
                seq = chain.by_id.get(row[0])
                if seq is None or row[1] != chain.parent_of(seq):
                    continue
                additions += row[2] == ADDED
                if seq not in wanted:
                    continue
                after, before = row[5 : 5 + width], row[5 + width :]
                if row[2] == MODIFIED and not self._changed(
                    present, after, before, row[3], row[4], properties is not None, prov
                ):
                    continue
                records.append((chain.entries[seq], row[2], uid, *after, row[3]))
            # The object was removed at some point if it was added more than
            # once, or if it has a past and is absent at the end of the range.
            if additions > 1 or (rows and type_at_end != type_name):
                removals = conn.query(
                    f"SELECT `to_commit`, `from_commit` FROM {source} "
                    f"WHERE `from_{UID_FIELD}` = %s AND `diff_type` = %s",
                    (uid, REMOVED),
                )
                for commit, parent in removals:
                    seq = chain.by_id.get(commit)
                    if seq is not None and seq in wanted and parent == chain.parent_of(seq):
                        records.append((chain.entries[seq], REMOVED, uid, *([None] * width), None))
        return self._change_table(schema, requested, names, present, prov, records)

    @staticmethod
    def _changed(
        present: Sequence[str],
        after: Sequence[Any],
        before: Sequence[Any],
        prov_after: Any,
        prov_before: Any,
        selected: bool,
        prov: bool,
    ) -> bool:
        """Whether a row reported as modified differs in what the caller asked to see.

        A change of table definition rewrites every row without changing any
        value; such rows are reported by the database and dropped here.
        """
        if any(a != b for a, b in zip(after, before, strict=True)):
            return True
        if prov_after == prov_before:
            return False
        if not selected:
            return True
        if not prov:
            return False
        new, old = json.loads(prov_after), json.loads(prov_before)
        return any(_effective(new, name) != _effective(old, name) for name in present)

    @public
    def changes(
        self,
        type: str,
        range: At = ALL_VERSIONS,
        branch: str = LIVE_BRANCH,
        columns: Sequence[str] | None = None,
        prov: bool = False,
        ontology: Ontology | None = None,
    ) -> pa.Table:
        """Every row of a type that was added, modified or removed in the range.

        Each row carries the version, its position and time, the kind of
        change, and the object as it was after it (nulls when removed).
        """
        requested = self._requested_type(ontology, type)
        names = self._names(requested, columns)
        schema = self._change_schema(requested, names, prov)
        with self._reading("changes", type=type) as conn:
            versions = self._resolve(conn, range, branch)
            last = versions[-1]
            chain = self._chain(conn, last.branch)
            stored_ontology = self._ontology_of(conn, last)
            if not stored_ontology.has_type(type):
                return schema.empty_table()
            stored = stored_ontology.type(type)
            present = [name for name in names if stored.has_field(name)]
            source = self._history_table(last, stored)
            select = [
                "`to_commit`",
                "`from_commit`",
                "`diff_type`",
                f"`to_{UID_FIELD}`",
                f"`from_{UID_FIELD}`",
                "`to__prov`" if prov else "NULL",
            ]
            select.extend(f"`to_{name}`" for name in present)
            where, parameters = "", []
            if len(versions) <= _NAMED_COMMITS:
                where = f" WHERE `to_commit` IN ({', '.join(['%s'] * len(versions))})"
                parameters = [version.id for version in versions]
            rows = conn.query(f"SELECT {', '.join(select)} FROM {source}{where}", parameters)
            wanted = {version.seq for version in versions}
            kept = []
            merged: set[str] = set()
            for row in rows:
                seq = chain.by_id.get(row[0])
                if seq is None or seq not in wanted or row[1] != chain.parent_of(seq):
                    continue
                if row[2] == MODIFIED:
                    cause = chain.entries[seq].cause
                    if cause == Cause.migration:
                        # A migration rewrites rows without changing a value.
                        continue
                    if cause == Cause.merge:
                        merged.add(row[0])
                kept.append((seq, row))
            genuine = self._modified_by_merge(conn, source, stored, chain, merged)
            records = []
            for seq, row in kept:
                if row[2] == MODIFIED and row[0] in merged and (row[0], row[3]) not in genuine:
                    continue
                uid = row[4] if row[2] == REMOVED else row[3]
                records.append((chain.entries[seq], row[2], uid, *row[6:], row[5]))
        return self._change_table(schema, requested, names, present, prov, records)

    def _modified_by_merge(
        self, conn: Conn, source: str, stored: ObjectType, chain: Chain, commits: set[str]
    ) -> set[tuple[str, str]]:
        """(commit, uid) of the rows a merge really changed.

        A merge that brings in a new table definition reports every row as
        modified, so the two sides of each are compared.
        """
        if not commits:
            return set()
        names = [f.name for f in stored.fields]
        select = ["`to_commit`", "`from_commit`", f"`to_{UID_FIELD}`", "`to__prov`", "`from__prov`"]
        select.extend(f"`to_{name}`" for name in names)
        select.extend(f"`from_{name}`" for name in names)
        ordered = sorted(commits)
        rows = conn.query(
            f"SELECT {', '.join(select)} FROM {source} "
            f"WHERE `to_commit` IN ({', '.join(['%s'] * len(ordered))}) AND `diff_type` = %s",
            [*ordered, MODIFIED],
        )
        width = len(names)
        genuine = set()
        for row in rows:
            seq = chain.by_id.get(row[0])
            if seq is None or row[1] != chain.parent_of(seq):
                continue
            if row[3] != row[4] or row[5 : 5 + width] != row[5 + width :]:
                genuine.add((row[0], row[2]))
        return genuine

    # -- two versions -------------------------------------------------------

    @public
    def diff(
        self,
        a: At,
        b: At,
        types: Sequence[str] | None = None,
        branch: str = LIVE_BRANCH,
        prov: bool = False,
        ontology: Ontology | None = None,
    ) -> dict[str, pa.Table]:
        """Rows added, removed and modified going from version `a` to version `b`.

        One table per type with any difference. Its columns are `_change`,
        `uid`, the properties as they are at `b`, and `_before`, a struct of
        the properties as they were at `a` (null for added rows).
        """
        requested = ontology or self._ontology
        if types is None:
            wanted = list(requested.types)
        else:
            wanted = [self._requested_type(ontology, name) for name in types]
        result: dict[str, pa.Table] = {}
        with self._reading("diff") as conn:
            first, second = self._one(conn, a, branch), self._one(conn, b, branch)
            before_ontology = self._ontology_of(conn, first)
            after_ontology = self._ontology_of(conn, second)
            for object_type in wanted:
                table = self._diff_type(
                    conn, object_type, first, second, before_ontology, after_ontology, prov
                )
                if table is not None and table.num_rows:
                    result[object_type.name] = table
        return result

    def _diff_type(
        self,
        conn: Conn,
        requested: ObjectType,
        first: Version,
        second: Version,
        before_ontology: Ontology,
        after_ontology: Ontology,
        prov: bool,
    ) -> pa.Table | None:
        name = requested.name
        in_before, in_after = before_ontology.has_type(name), after_ontology.has_type(name)
        if not in_before and not in_after:
            return None
        names = [f.name for f in requested.fields]
        after_names = [n for n in names if in_after and after_ontology.type(name).has_field(n)]
        before_names = [n for n in names if in_before and before_ontology.type(name).has_field(n)]
        # A side on which the table does not exist has no columns to name.
        select = ["`diff_type`"]
        select.append(f"`to_{UID_FIELD}`" if in_after else "NULL")
        select.append(f"`from_{UID_FIELD}`" if in_before else "NULL")
        select.append("`to__prov`" if in_after else "NULL")
        select.append("`from__prov`" if in_before else "NULL")
        select.extend(f"`to_{n}`" for n in after_names)
        select.extend(f"`from_{n}`" for n in before_names)
        rows = conn.query(
            f"SELECT {', '.join(select)} FROM DOLT_DIFF(%s, %s, %s)",
            (first.id, second.id, requested.table),
        )
        split = 5 + len(after_names)
        shared = [n for n in after_names if n in before_names]
        after_at = {n: 5 + i for i, n in enumerate(after_names)}
        before_at = {n: split + i for i, n in enumerate(before_names)}
        added_only = [after_at[n] for n in after_names if n not in before_at]
        kept = []
        for row in rows:
            if (
                row[0] == MODIFIED
                and row[3] == row[4]
                and all(row[after_at[n]] == row[before_at[n]] for n in shared)
                and all(row[i] is None for i in added_only)
            ):
                # Rewritten by a change of table definition, with nothing changed.
                continue
            kept.append(row)
        if not kept:
            return None
        kept.sort(key=lambda row: row[1] or row[2])
        cells = _transpose(kept, len(select))
        count = len(kept)
        uids = [row[1] or row[2] for row in kept]
        arrays = [pa.array(cells[0], pa.string()), pa.array(uids, pa.string())]
        fields = [pa.field(CHANGE_COLUMN, pa.string()), pa.field(UID_FIELD, pa.string())]
        fields.extend(codec.arrow_fields(requested, names))
        arrays.extend(self._value_columns(requested, names, after_names, cells[5:split], count))
        before_fields = codec.arrow_fields(requested, names)
        before_arrays = self._value_columns(requested, names, before_names, cells[split:], count)
        if prov:
            fields.append(pa.field(PROV_COLUMN, pa.string()))
            arrays.append(codec.text_column(cells[3]))
            before_fields.append(pa.field(PROV_COLUMN, pa.string()))
            before_arrays.append(codec.text_column(cells[4]))
        absent = pa.array([change == ADDED for change in cells[0]], pa.bool_())
        fields.append(pa.field(BEFORE_COLUMN, pa.struct(before_fields)))
        arrays.append(pa.StructArray.from_arrays(before_arrays, fields=before_fields, mask=absent))
        return pa.Table.from_arrays(arrays, schema=pa.schema(fields))

    # -- links --------------------------------------------------------------

    @public
    def edges(
        self,
        edge_types: Sequence[str] | None = None,
        at: At = "head",
        branch: str = LIVE_BRANCH,
    ) -> pa.Table:
        """`(src, dst, edge_type, link_uid)` for every link and reference at a version.

        A link type gives one edge per link, named after the type, from its
        source to its target. Every other reference property gives an edge
        from the object to what it refers to, named `<Type>.<property>`, with
        no link uid.
        """
        schema = pa.schema(
            [
                pa.field("src", pa.string()),
                pa.field("dst", pa.string()),
                pa.field("edge_type", pa.string()),
                pa.field("link_uid", pa.string()),
            ]
        )
        wanted = None if edge_types is None else set(edge_types)
        tables = []
        with self._reading("edges") as conn:
            version = self._one(conn, at, branch)
            stored = self._ontology_of(conn, version)
            if wanted is not None:
                known = self._edge_names(stored) | self._edge_names(self._ontology)
                unknown = sorted(wanted - known)
                if unknown:
                    raise UnknownType(
                        f"unknown edge type {unknown[0]!r}", detail={"edge_types": unknown}
                    )
            for object_type in stored.types:
                ends = object_type.link or ()
                as_link = bool(ends) and (wanted is None or object_type.name in wanted)
                references = [
                    f.name
                    for f in object_type.fields
                    if f.kind == Kind.ref
                    and f.name not in ends
                    and (wanted is None or f"{object_type.name}.{f.name}" in wanted)
                ]
                needed = [*(ends if as_link else ()), *references]
                if not needed:
                    continue
                select = ", ".join([f"`{UID_FIELD}`", *(ddl.quote(name) for name in needed)])
                rows = conn.query(
                    f"SELECT {select} FROM {ddl.quote(object_type.table)} AS OF %s", (version.id,)
                )
                if not rows:
                    continue
                cells = _transpose(rows, 1 + len(needed))
                uids = pa.array(cells[0], pa.string())
                at_column = {name: 1 + index for index, name in enumerate(needed)}
                count = len(rows)
                if as_link:
                    tables.append(
                        self._edge_table(
                            schema,
                            pa.array(cells[at_column[ends[0]]], pa.string()),
                            pa.array(cells[at_column[ends[1]]], pa.string()),
                            object_type.name,
                            uids,
                        )
                    )
                for name in references:
                    tables.append(
                        self._edge_table(
                            schema,
                            uids,
                            pa.array(cells[at_column[name]], pa.string()),
                            f"{object_type.name}.{name}",
                            pa.nulls(count, pa.string()),
                        )
                    )
        if not tables:
            return schema.empty_table()
        return pa.concat_tables(tables).combine_chunks()

    @staticmethod
    def _edge_names(ontology: Ontology) -> set[str]:
        names = set()
        for object_type in ontology.types:
            ends = object_type.link or ()
            if ends:
                names.add(object_type.name)
            names.update(
                f"{object_type.name}.{f.name}"
                for f in object_type.fields
                if f.kind == Kind.ref and f.name not in ends
            )
        return names

    @staticmethod
    def _edge_table(
        schema: pa.Schema, src: pa.Array, dst: pa.Array, edge_type: str, link_uid: pa.Array
    ) -> pa.Table:
        table = pa.Table.from_arrays(
            [src, dst, pa.repeat(pa.scalar(edge_type, pa.string()), len(src)), link_uid],
            schema=schema,
        )
        # An optional reference left empty is not an edge.
        return table.filter(pc.and_(pc.is_valid(src), pc.is_valid(dst)))
