"""Applying one change set inside an open transaction.

The checks run in a fixed order: the shape of the change set, the rule on
which kinds of fact may be written where, provenance, the values against
their types, the types of referenced objects, and last the constraints held
by the database. Everything found is reported together. The caller commits
the transaction when nothing was found and rolls it back otherwise, so a
refused change set leaves nothing behind.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pymysql

from ..log import Log
from ..schema import (
    UID_FIELD,
    UID_MAX_LENGTH,
    FactKind,
    Kind,
    ObjectType,
    Ontology,
    RowValidator,
    Violation,
)
from . import codec, ddl
from .db import Conn, error_code, error_text
from .model import LIVE_BRANCH, Cause, ChangeSet, Provenance

log = Log("store.write")

RULE_CAUSE = "cause"
RULE_FACT_KIND = "fact_kind"
RULE_PROVENANCE = "provenance"
RULE_UNKNOWN_TYPE = "unknown_type"
RULE_UNKNOWN_PROPERTY = "unknown_property"
RULE_UID = "uid"
RULE_DUPLICATE = "duplicate"
RULE_TYPE = "type"
RULE_UID_TYPE = ddl.RULE_UID_TYPE
RULE_REFERENCE = ddl.RULE_REFERENCE
RULE_NOT_FOUND = "not_found"
RULE_REFERENCED = "referenced"
RULE_DATABASE = "database"

_CHUNK = 2000
_STAR = "*"

ER_BAD_NULL = 1048
ER_ROW_IS_REFERENCED = 1451
ER_NO_REFERENCED_ROW = 1452
ER_TRUNCATED_WRONG_VALUE = 1366
_CONSTRAINT_IN_ERROR = re.compile(r'(?:Check constraint "|fk: `)([A-Za-z0-9_]+)')
_COLUMN_IN_ERROR = re.compile(r"column(?: name)? '([a-z][a-z0-9_]*)'")


@dataclass
class _Row:
    type: ObjectType
    uid: str
    given: dict[str, Any]  # as passed by the caller, for the validator
    encoded: dict[str, Any]  # property -> SQL parameter
    provenance: Provenance
    mode: str = ""  # "create" | "replace" | "update"


@dataclass
class Applied:
    violations: list[Violation] = field(default_factory=list)
    created: int = 0
    replaced: int = 0
    updated: int = 0
    deleted: int = 0


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _marks(count: int) -> str:
    return ", ".join(["%s"] * count)


def fact_kind_problem(kind: FactKind, cause: Cause, branch: str) -> str | None:
    """Why a fact of this kind may not be written by this cause on this branch."""
    if branch != LIVE_BRANCH:
        return None
    if kind == FactKind.simulated:
        return f"simulated facts cannot be written to {LIVE_BRANCH}"
    if kind == FactKind.observed and cause != Cause.sync:
        return f"observed facts reach {LIVE_BRANCH} only through sync, not {cause.value}"
    return None


def _uid_problem(object_type: ObjectType, uid: Any) -> str | None:
    if not isinstance(uid, str) or not uid:
        return "row has no uid"
    prefix = f"{object_type.scope}:"
    if not uid.startswith(prefix) or len(uid) == len(prefix):
        return f"uid must be '{prefix}<key>'"
    if len(uid) > UID_MAX_LENGTH:
        return f"uid is longer than {UID_MAX_LENGTH} characters"
    return None


class _Application:
    def __init__(
        self,
        conn: Conn,
        ontology: Ontology,
        validator: RowValidator,
        changes: ChangeSet,
        cause: Cause,
        branch: str,
    ) -> None:
        self.conn = conn
        self.ontology = ontology
        self.validator = validator
        self.changes = changes
        self.cause = cause
        self.branch = branch
        self.result = Applied()
        self._reported: set[tuple[str, str | None, str | None, str]] = set()
        self.rows: list[_Row] = []
        self.deletes: list[tuple[ObjectType, str]] = []
        self.existing: dict[str, str] = {}  # uid -> type name, as stored before this change

    def report(
        self, type_name: str, uid: str | None, prop: str | None, rule: str, message: str
    ) -> None:
        key = (type_name, uid, prop, rule)
        if key not in self._reported:
            self._reported.add(key)
            self.result.violations.append(Violation(type_name, uid, prop, rule, message))

    # -- shape, fact kinds, provenance ---------------------------------------

    def read_change_set(self) -> None:
        changes = self.changes
        if self.cause == Cause.sync and self.branch != LIVE_BRANCH:
            self.report(
                "", None, None, RULE_CAUSE, f"sync writes only to {LIVE_BRANCH}, not {self.branch}"
            )
        seen: set[str] = set()
        for type_name, rows in changes.upserts.items():
            if not self.ontology.has_type(type_name):
                self.report(type_name, None, None, RULE_UNKNOWN_TYPE, f"unknown type {type_name}")
                continue
            object_type = self.ontology.type(type_name)
            for row in rows:
                self._read_row(object_type, row, seen)
        for uid in changes.row_provenance:
            if uid not in seen:
                self.report(
                    "",
                    uid if isinstance(uid, str) else None,
                    None,
                    RULE_PROVENANCE,
                    "provenance is given for a uid that is not written",
                )
        gone: set[str] = set()
        for type_name, uids in changes.deletes.items():
            if not self.ontology.has_type(type_name):
                self.report(type_name, None, None, RULE_UNKNOWN_TYPE, f"unknown type {type_name}")
                continue
            object_type = self.ontology.type(type_name)
            for uid in uids:
                if not isinstance(uid, str) or not uid:
                    self.report(type_name, None, None, RULE_UID, "delete names no uid")
                elif uid in seen or uid in gone:
                    self.report(
                        type_name, uid, None, RULE_DUPLICATE, "uid appears twice in the change set"
                    )
                else:
                    gone.add(uid)
                    self.deletes.append((object_type, uid))

    def _read_row(self, object_type: ObjectType, row: Any, seen: set[str]) -> None:
        name = object_type.name
        if not isinstance(row, Mapping):
            self.report(name, None, None, RULE_UID, "row is not a mapping")
            return
        uid = row.get(UID_FIELD)
        problem = _uid_problem(object_type, uid)
        if problem:
            self.report(name, uid if isinstance(uid, str) else None, UID_FIELD, RULE_UID, problem)
            return
        if uid in seen:
            self.report(name, uid, None, RULE_DUPLICATE, "uid appears twice in the change set")
            return
        seen.add(uid)
        sound = True
        provenance = self.changes.row_provenance.get(uid, self.changes.provenance)
        if not isinstance(provenance, Provenance):
            self.report(name, uid, None, RULE_PROVENANCE, "row is written without provenance")
            sound = False
        elif not object_type.meta:
            problem = fact_kind_problem(provenance.kind, self.cause, self.branch)
            if problem:
                self.report(name, uid, None, RULE_FACT_KIND, problem)
                sound = False
        given: dict[str, Any] = {}
        encoded: dict[str, Any] = {}
        for key, value in row.items():
            if key == UID_FIELD:
                continue
            if not isinstance(key, str) or not object_type.has_field(key):
                self.report(
                    name, uid, str(key), RULE_UNKNOWN_PROPERTY, f"{name} has no property {key}"
                )
                sound = False
                continue
            given[key] = value
            try:
                encoded[key] = codec.encode(object_type.field(key), value)
            except ValueError as exc:
                self.report(name, uid, key, RULE_TYPE, f"{key}: {exc}")
                sound = False
        if sound:
            self.rows.append(_Row(object_type, uid, given, encoded, provenance))

    # -- what is stored now ---------------------------------------------------

    def read_existing(self) -> None:
        uids = {row.uid for row in self.rows} | {uid for _, uid in self.deletes}
        for row in self.rows:
            for reference in row.type.references():
                target = row.encoded.get(reference.name)
                if target is not None:
                    uids.add(target)
        for chunk in _chunks(sorted(uids)):
            found = self.conn.query(
                f"SELECT `{UID_FIELD}`, `type` FROM `{ddl.OBJECT_TABLE}` "
                f"WHERE `{UID_FIELD}` IN ({_marks(len(chunk))})",
                chunk,
            )
            self.existing.update(found)

    def classify(self) -> None:
        kept = []
        for row in self.rows:
            stored = self.existing.get(row.uid)
            if stored is None:
                row.mode = "create"
            elif stored != row.type.name:
                self.report(
                    row.type.name,
                    row.uid,
                    None,
                    RULE_UID_TYPE,
                    f"uid already belongs to a {stored}; one uid holds one type",
                )
                continue
            elif len(row.encoded) == len(row.type.fields):
                row.mode = "replace"
            else:
                row.mode = "update"
            kept.append(row)
        self.rows = kept

    # -- values, then references ----------------------------------------------

    def validate_values(self) -> None:
        batches: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        for row in self.rows:
            partial = row.mode == "update"
            batches.setdefault((row.type.name, partial), []).append(
                {UID_FIELD: row.uid, **row.given}
            )
        for (type_name, partial), rows in batches.items():
            for found in self.validator.validate(type_name, rows, partial=partial):
                self.report(found.type, found.uid, found.property, found.rule, found.message)

    def check_references(self) -> None:
        created = {row.uid: row.type.name for row in self.rows if row.mode == "create"}
        deleted = {uid for _, uid in self.deletes}
        for row in self.rows:
            for reference in row.type.references():
                target = row.encoded.get(reference.name)
                if target is None:
                    continue
                if target in deleted:
                    target_type = None
                else:
                    target_type = created.get(target) or self.existing.get(target)
                allowed = self.ontology.concrete(reference.target or "")
                if target_type is None:
                    self.report(
                        row.type.name,
                        row.uid,
                        reference.name,
                        RULE_REFERENCE,
                        f"{reference.name} refers to {target}, which does not exist",
                    )
                elif target_type not in allowed:
                    self.report(
                        row.type.name,
                        row.uid,
                        reference.name,
                        RULE_REFERENCE,
                        f"{reference.name} refers to {target}, a {target_type}; "
                        f"it must be a {reference.target}",
                    )

    def check_deletes(self) -> None:
        kept = []
        for object_type, uid in self.deletes:
            stored = self.existing.get(uid)
            if stored is None:
                self.report(object_type.name, uid, None, RULE_NOT_FOUND, "object does not exist")
            elif stored != object_type.name:
                self.report(
                    object_type.name,
                    uid,
                    None,
                    RULE_UID_TYPE,
                    f"object is a {stored}, not a {object_type.name}",
                )
            else:
                kept.append((object_type, uid))
        self.deletes = kept
        if not kept:
            return
        deleted = {uid: object_type.name for object_type, uid in kept}
        written = {row.uid: row for row in self.rows}
        for referrer, referrer_uid, prop, target in referencing(
            self.conn, self.ontology, sorted(deleted)
        ):
            if referrer_uid in deleted:
                continue
            row = written.get(referrer_uid)
            if row is not None and prop in row.encoded and row.encoded[prop] != target:
                continue
            self.report(
                deleted[target],
                target,
                None,
                RULE_REFERENCED,
                f"still referenced by {prop} of {referrer} {referrer_uid}",
            )

    # -- writing --------------------------------------------------------------

    def write(self) -> None:
        creates = [(row.uid, row.type.name) for row in self.rows if row.mode == "create"]
        self._write_batch(
            f"INSERT INTO `{ddl.OBJECT_TABLE}` (`{UID_FIELD}`, `type`) VALUES (%s, %s)",
            creates,
            lambda index, exc: self._explain(None, creates[index][1], creates[index][0], exc),
        )
        by_type: dict[str, list[_Row]] = {}
        for row in self.rows:
            by_type.setdefault(row.type.name, []).append(row)
        for rows in by_type.values():
            self._write_type(rows[0].type, rows)
        gone: dict[str, list[str]] = {}
        for object_type, uid in self.deletes:
            gone.setdefault(object_type.name, []).append(uid)
        for type_name, uids in gone.items():
            object_type = self.ontology.type(type_name)
            self._delete(object_type, ddl.quote(object_type.table), uids)
        for type_name, uids in gone.items():
            self._delete(self.ontology.type(type_name), f"`{ddl.OBJECT_TABLE}`", uids)
        result = self.result
        for row in self.rows:
            if row.mode == "create":
                result.created += 1
            elif row.mode == "replace":
                result.replaced += 1
            else:
                result.updated += 1
        result.deleted = len(self.deletes)

    def _stored_rows(self, object_type: ObjectType, uids: list[str]) -> dict[str, tuple[Any, ...]]:
        columns = ", ".join(ddl.quote(f.name) for f in object_type.fields)
        select = f"`{UID_FIELD}`, `_prov`" + (f", {columns}" if columns else "")
        stored: dict[str, tuple[Any, ...]] = {}
        for chunk in _chunks(uids):
            for found in self.conn.query(
                f"SELECT {select} FROM {ddl.quote(object_type.table)} "
                f"WHERE `{UID_FIELD}` IN ({_marks(len(chunk))})",
                chunk,
            ):
                stored[found[0]] = found[1:]
        return stored

    def _write_type(self, object_type: ObjectType, rows: list[_Row]) -> None:
        names = [f.name for f in object_type.fields]
        stored = self._stored_rows(object_type, [row.uid for row in rows if row.mode == "update"])
        parameters = []
        for row in rows:
            entry = row.provenance.to_json()
            if row.mode == "update":
                before = stored.get(row.uid)
                if before is None:
                    self.report(
                        object_type.name,
                        row.uid,
                        None,
                        RULE_DATABASE,
                        "object is registered but its row is missing",
                    )
                    continue
                provenance = json.loads(before[0])
                values = dict(zip(names, before[1:], strict=True))
                for name, value in row.encoded.items():
                    values[name] = value
                    # A property keeps an entry of its own only while it differs from the row's.
                    if entry == provenance[_STAR]:
                        provenance.pop(name, None)
                    else:
                        provenance[name] = entry
                cells = [values[name] for name in names]
            else:
                provenance = {_STAR: entry}
                cells = [row.encoded.get(name) for name in names]
            parameters.append(
                (row.uid, *cells, json.dumps(provenance, sort_keys=True, separators=(",", ":")))
            )
        columns = [f"`{UID_FIELD}`", *(ddl.quote(name) for name in names), "`_prov`"]
        updates = ", ".join(f"{column}=VALUES({column})" for column in columns[1:])
        sql = (
            f"INSERT INTO {ddl.quote(object_type.table)} ({', '.join(columns)}) "
            f"VALUES ({_marks(len(columns))}) ON DUPLICATE KEY UPDATE {updates}"
        )
        self._write_batch(
            sql,
            parameters,
            lambda index, exc: self._explain(
                object_type, object_type.name, parameters[index][0], exc
            ),
        )

    def _delete(self, object_type: ObjectType, table: str, uids: list[str]) -> None:
        for chunk in _chunks(uids):
            try:
                self.conn.execute(
                    f"DELETE FROM {table} WHERE `{UID_FIELD}` IN ({_marks(len(chunk))})", chunk
                )
            except pymysql.MySQLError as exc:
                rule = RULE_REFERENCED if error_code(exc) == ER_ROW_IS_REFERENCED else RULE_DATABASE
                self.report(
                    object_type.name,
                    None,
                    None,
                    rule,
                    f"delete refused by the database: {error_text(exc)}",
                )

    def _write_batch(self, sql: str, parameters: list[tuple[Any, ...]], explain: Any) -> None:
        """Write the rows together; when the database refuses, find every row it refuses."""
        if not parameters:
            return
        conn = self.conn

        def attempt(low: int, high: int, depth: int) -> None:
            savepoint = f"dta_w{depth}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                conn.executemany(sql, parameters[low:high])
            except pymysql.MySQLError as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                if high - low == 1:
                    explain(low, exc)
                    return
                middle = (low + high) // 2
                attempt(low, middle, depth + 1)
                attempt(middle, high, depth + 1)
                return
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")

        attempt(0, len(parameters), 0)

    def _explain(
        self, object_type: ObjectType | None, type_name: str, uid: str, exc: pymysql.MySQLError
    ) -> None:
        """Turn an error raised by the database for one row into a violation."""
        code, text = error_code(exc), error_text(exc)
        prop: str | None = None
        rule = RULE_DATABASE
        constraint = _CONSTRAINT_IN_ERROR.search(text)
        column = _COLUMN_IN_ERROR.search(text)
        if constraint and object_type is not None:
            prop, rule = ddl.constraint_rules(object_type).get(
                constraint.group(1), (None, RULE_DATABASE)
            )
        elif code == ER_BAD_NULL and column:
            prop, rule = column.group(1), "required"
        elif code == ER_TRUNCATED_WRONG_VALUE:
            rule = RULE_TYPE
        elif column and object_type is not None and object_type.has_field(column.group(1)):
            prop = column.group(1)
            if "too large" in text:
                rule = "length"
            elif object_type.field(prop).kind == Kind.enum:
                rule = "enum"
            else:
                rule = RULE_TYPE
        elif code == ER_NO_REFERENCED_ROW:
            rule = RULE_REFERENCE
        self.report(type_name, uid, prop, rule, f"refused by the database: {text}")


def referencing(
    conn: Conn, ontology: Ontology, uids: Sequence[str]
) -> list[tuple[str, str, str, str]]:
    """(type, uid, property, target) of every stored row that refers to one of `uids`."""
    selects = []
    for object_type in ontology.types:
        for reference in object_type.references():
            column = ddl.quote(reference.name)
            selects.append(
                f"SELECT {ddl.literal(object_type.name)}, `{UID_FIELD}`, "
                f"{ddl.literal(reference.name)}, {column} FROM {ddl.quote(object_type.table)} "
                f"WHERE {column} IN ({{marks}})"
            )
    found: list[tuple[str, str, str, str]] = []
    if not selects:
        return found
    for chunk in _chunks(uids, 500):
        marks = _marks(len(chunk))
        sql = " UNION ALL ".join(select.format(marks=marks) for select in selects)
        found.extend(conn.query(sql, list(chunk) * len(selects)))
    return found


def apply_changes(
    conn: Conn,
    ontology: Ontology,
    validator: RowValidator,
    changes: ChangeSet,
    cause: Cause,
    branch: str,
) -> Applied:
    """Check `changes` and write them into the open transaction of `conn`.

    Nothing is written unless every check made before writing passes. When
    the result holds violations the caller must roll the transaction back.
    """
    application = _Application(conn, ontology, validator, changes, cause, branch)
    application.read_change_set()
    application.read_existing()
    application.classify()
    application.validate_values()
    application.check_references()
    application.check_deletes()
    if not application.result.violations:
        application.write()
    return application.result
