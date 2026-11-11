"""`where=<property>:<op>:<value>` filters, turned into the store's filter triples.

The clause is split on its first two colons only, so values may hold colons
(times do). A value is read according to the kind of the property it is
compared with; `in` takes values separated by commas, `null` and `notnull`
take none, `like` takes an SQL pattern.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from dta_core.schema import UID_FIELD, Field, Kind
from dta_core.store import InvalidInput, UnknownProperty

from .services import repair_plus

OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "in": "in",
    "like": "like",
    "null": "is_null",
    "notnull": "not_null",
}
_NO_VALUE = ("null", "notnull")
_TRUE = ("true", "1")
_FALSE = ("false", "0")

Triple = tuple[str, str, Any]


def _bad(message: str) -> InvalidInput:
    return InvalidInput(message, code="store.bad_filter")


def _value(name: str, field: Field | None, text: str) -> Any:
    """`text` read as a value of the property's kind."""
    if field is None or field.kind in (Kind.string, Kind.enum, Kind.ref):
        return text
    kind = field.kind
    try:
        if kind == Kind.integer:
            return int(text)
        if kind == Kind.float:
            number = float(text)
            if not math.isfinite(number):
                raise ValueError("not a finite number")
            return number
        if kind == Kind.boolean:
            lowered = text.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError("not true or false")
        if kind == Kind.datetime:
            moment = datetime.fromisoformat(repair_plus(text))
            if moment.tzinfo is None:
                raise ValueError("a time needs an offset, such as Z or +01:00")
            return moment
        if kind == Kind.date:
            return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise _bad(f"filter on {name}: {text!r} is not a {kind.value}: {exc}") from exc
    raise _bad(f"filter on {name}: a {kind.value} property cannot be compared with a value")


def parse_where(
    clauses: Sequence[str], fields: Mapping[str, Field], type_name: str
) -> list[Triple]:
    """Store filter triples for `where` clauses over the given properties of a type."""
    triples: list[Triple] = []
    for clause in clauses:
        parts = clause.split(":", 2)
        if len(parts) < 2:
            raise _bad(f"a filter is written <property>:<op>:<value>, got {clause!r}")
        name, operator = parts[0].strip(), parts[1].strip().lower()
        text = parts[2] if len(parts) == 3 else None
        if operator not in OPERATORS:
            raise _bad(f"unknown filter operator {operator!r}; use one of {', '.join(OPERATORS)}")
        if name != UID_FIELD and name not in fields:
            raise UnknownProperty(
                f"{type_name} has no property {name!r}",
                detail={"type": type_name, "property": name},
            )
        field = None if name == UID_FIELD else fields[name]
        if field is not None and field.stored_as_json:
            raise _bad(f"{name} holds lists or structured values and cannot be filtered on")
        if operator in _NO_VALUE:
            if text:
                raise _bad(f"filter on {name}: {operator} takes no value")
            triples.append((name, OPERATORS[operator], None))
            continue
        if text is None:
            raise _bad(f"filter on {name}: {operator} needs a value")
        if operator == "in":
            triples.append((name, "in", [_value(name, field, item) for item in text.split(",")]))
        elif operator == "like":
            triples.append((name, "like", text))
        else:
            triples.append((name, OPERATORS[operator], _value(name, field, text)))
    return triples
