"""Checks on the result of a merge before it becomes a version.

The database merges cell by cell and reports conflicts and broken keys. Two
rules are not its to keep: which kinds of fact may reach the live branch,
and that a reference points at an object of the type it names. Both are
checked here on the rows the merge changed, inside the transaction that
holds the merge, so a refused merge is simply rolled back.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..schema import UID_FIELD, FactKind, Ontology, Violation
from . import ddl
from .db import Conn
from .model import LIVE_BRANCH, Cause
from .write import RULE_FACT_KIND, RULE_REFERENCE, fact_kind_problem, referencing

_MERGED = "FROM DOLT_DIFF('HEAD', 'WORKING', %s) WHERE `diff_type` <> 'removed'"
_CHUNK = 2000


def _effective(provenance: dict[str, Any], name: str) -> Any:
    return provenance.get(name) or provenance.get("*")


def _kind_problem(entry: Any) -> str | None:
    try:
        kind = FactKind(entry["k"])
    except (KeyError, TypeError, ValueError):
        return "provenance is missing or malformed"
    return fact_kind_problem(kind, Cause.merge, LIVE_BRANCH)


def _types_of(conn: Conn, uids: Sequence[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for start in range(0, len(uids), _CHUNK):
        chunk = uids[start : start + _CHUNK]
        found.update(
            conn.query(
                f"SELECT `{UID_FIELD}`, `type` FROM `{ddl.OBJECT_TABLE}` "
                f"WHERE `{UID_FIELD}` IN ({', '.join(['%s'] * len(chunk))})",
                chunk,
            )
        )
    return found


def restore_absent_values(conn: Conn, ontology: Ontology) -> int:
    """Put back the absent values an uncommitted merge turned into JSON nulls.

    A row changed on both sides is merged cell by cell, and a JSON column
    holding no value on either side comes out of that holding a JSON `null`.
    Nothing ever writes one, and left alone it would count as a change made
    by the merge and slip past a filter on absent values. Returns how many
    cells were put back.
    """
    restored = 0
    for object_type in ontology.types:
        columns = [f.name for f in object_type.fields if ddl.column_type(ontology, f) == "JSON"]
        if not columns:
            continue
        turned = " OR ".join(f"JSON_TYPE(`to_{name}`) = 'NULL'" for name in columns)
        uids = [
            row[0]
            for row in conn.query(
                f"SELECT `to_{UID_FIELD}` FROM DOLT_DIFF('HEAD', 'WORKING', %s) "
                f"WHERE `diff_type` = 'modified' AND ({turned})",
                (object_type.table,),
            )
        ]
        for start in range(0, len(uids), _CHUNK):
            chunk = uids[start : start + _CHUNK]
            marks = ", ".join(["%s"] * len(chunk))
            for name in columns:
                restored += conn.execute(
                    f"UPDATE `{object_type.table}` SET `{name}` = NULL "
                    f"WHERE `{UID_FIELD}` IN ({marks}) AND JSON_TYPE(`{name}`) = 'NULL'",
                    chunk,
                )
    return restored


def check_merged(conn: Conn, ontology: Ontology, into: str) -> list[Violation]:
    """Violations among the rows an uncommitted merge has changed on `into`."""
    violations: list[Violation] = []
    # (type, uid, property, target, type the property names)
    pointing: list[tuple[str, str, str, str, str]] = []
    for object_type in ontology.types:
        references = [f.name for f in object_type.references()]
        facts = into == LIVE_BRANCH and not object_type.meta
        if not references and not facts:
            continue
        names = [f.name for f in object_type.fields] if facts else references
        select = [f"`to_{UID_FIELD}`", "`diff_type`"]
        select.extend(f"`to_{name}`" for name in names)
        if facts:
            select.extend(["`to__prov`", "`from__prov`"])
            select.extend(f"`from_{name}`" for name in names)
        rows = conn.query(f"SELECT {', '.join(select)} {_MERGED}", (object_type.table,))
        width = len(names)
        for row in rows:
            uid, change = row[0], row[1]
            after = row[2 : 2 + width]
            for name in references:
                target = after[names.index(name)]
                if target is not None:
                    pointing.append(
                        (object_type.name, uid, name, target, object_type.field(name).target or "")
                    )
            if not facts:
                continue
            new = json.loads(row[2 + width])
            if change == "added":
                for key, entry in new.items():
                    problem = _kind_problem(entry)
                    if problem:
                        violations.append(
                            Violation(
                                object_type.name,
                                uid,
                                None if key == "*" else key,
                                RULE_FACT_KIND,
                                f"merge would bring in a fact that {problem}",
                            )
                        )
                continue
            old = json.loads(row[3 + width])
            before = row[4 + width :]
            # A fact arrives with the merge when its value or its provenance differs.
            arrived = [
                name
                for index, name in enumerate(names)
                if after[index] != before[index] or _effective(new, name) != _effective(old, name)
            ]
            for name in arrived:
                problem = _kind_problem(_effective(new, name))
                if problem:
                    violations.append(
                        Violation(
                            object_type.name,
                            uid,
                            name,
                            RULE_FACT_KIND,
                            f"merge would bring in a fact that {problem}",
                        )
                    )
            if not names and new.get("*") != old.get("*"):
                problem = _kind_problem(new.get("*"))
                if problem:
                    violations.append(
                        Violation(object_type.name, uid, None, RULE_FACT_KIND, problem)
                    )

    # A reference can also be broken from the other end: its target was
    # deleted and created again as another type on one side of the merge.
    retyped = [
        row[0]
        for row in conn.query(
            f"SELECT `to_{UID_FIELD}` FROM DOLT_DIFF('HEAD', 'WORKING', %s) "
            "WHERE `diff_type` = 'modified'",
            (ddl.OBJECT_TABLE,),
        )
    ]
    if retyped:
        for referrer, uid, name, target in referencing(conn, ontology, retyped):
            pointing.append(
                (referrer, uid, name, target, ontology.type(referrer).field(name).target or "")
            )
    if pointing:
        types = _types_of(conn, sorted({entry[3] for entry in pointing}))
        seen = set()
        for type_name, uid, name, target, wanted in pointing:
            if (uid, name) in seen:
                continue
            seen.add((uid, name))
            found = types.get(target)
            if found is None:
                message = f"{name} refers to {target}, which does not exist after the merge"
            elif found not in ontology.concrete(wanted):
                message = f"{name} refers to {target}, a {found}; it must be a {wanted}"
            else:
                continue
            violations.append(Violation(type_name, uid, name, RULE_REFERENCE, message))
    return violations
