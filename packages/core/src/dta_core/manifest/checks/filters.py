"""The form of a built-in's `where` and `edge_where` clauses, judged before the twin runs.

The twin reads a clause as `<property>:<op>:<value>`, split on its first two
colons, and refuses one it cannot read only when the function is called. The
same reading is done here so that `dta manifest validate` names the clause, with
the words the twin would use, while the manifest is still being written.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime

from ...schema import Field, Kind

# The operators a clause may use, as the twin reads them.
OPERATORS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "like", "null", "notnull")
NO_VALUE = ("null", "notnull")
FORM = "<property>:<op>:<value>"
EDGE_FORM = "<LinkType>.<property>:<op>:<value>"
_TRUE = ("true", "1")
_FALSE = ("false", "0")
# The twin reads a space before a time's offset as the `+` a query string lost.
_OFFSET_AFTER_SPACE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.]+) (\d{2}(:?\d{2})?)$")


def split_clause(clause: str) -> tuple[str, str, str | None] | None:
    """(property, operator, value text) of a clause; None when it has fewer than two parts."""
    parts = clause.split(":", 2)
    if len(parts) < 2:
        return None
    return parts[0].strip(), parts[1].strip().lower(), parts[2] if len(parts) == 3 else None


def clause_problem(clause: str, form: str = FORM) -> str | None:
    """Why the twin cannot read the clause, whatever the property is; None when it can."""
    parts = split_clause(clause)
    if parts is None:
        return f"a filter is written {form}, got {clause!r}"
    name, operator, text = parts
    if not name:
        return f"a filter is written {form}; {clause!r} names no property"
    if operator not in OPERATORS:
        known = ", ".join(OPERATORS)
        return f"unknown filter operator {operator!r} in {clause!r}; use one of {known}"
    if operator in NO_VALUE and text:
        return f"filter on {name}: {operator} takes no value"
    if operator not in NO_VALUE and text is None:
        return f"filter on {name}: {operator} needs a value"
    return None


def edge_clause_problem(clause: str) -> str | None:
    head = clause.split(":", 1)[0]
    if "." not in head or not head.split(".", 1)[0].strip():
        return f"an edge filter is written {EDGE_FORM}, got {clause!r}"
    return clause_problem(clause[clause.index(".") + 1 :], EDGE_FORM)


def _value_problem(kind: Kind, text: str) -> str | None:
    try:
        if kind == Kind.integer:
            int(text)
        elif kind == Kind.float:
            if not math.isfinite(float(text)):
                return "not a finite number"
        elif kind == Kind.boolean:
            if text.strip().lower() not in (*_TRUE, *_FALSE):
                return "not true or false"
        elif kind == Kind.datetime:
            moment = datetime.fromisoformat(_OFFSET_AFTER_SPACE.sub(r"\1+\2", text.strip()))
            if moment.tzinfo is None:
                return "a time needs an offset, such as Z or +01:00"
        elif kind == Kind.date:
            date.fromisoformat(text.strip())
        elif kind not in (Kind.string, Kind.enum, Kind.ref):
            return f"a {kind.value} property cannot be compared with a value"
    except ValueError as exc:
        return str(exc)
    return None


def value_problem(clause: str, field: Field) -> str | None:
    """Why the clause cannot be applied to this property; None when it can. The clause is
    one `clause_problem` accepts."""
    parts = split_clause(clause)
    if parts is None:
        return None
    name, operator, text = parts
    if field.stored_as_json:
        return f"{name} holds lists or structured values and cannot be filtered on"
    if operator in NO_VALUE or operator == "like" or text is None:
        return None
    items = text.split(",") if operator == "in" else [text]
    for item in items:
        problem = _value_problem(field.kind, item)
        if problem is not None:
            return f"filter on {name}: {item!r} is not a {field.kind.value}: {problem}"
    return None
