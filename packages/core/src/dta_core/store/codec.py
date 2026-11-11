"""Conversion of property values between callers, SQL parameters and Arrow columns.

Times are stored as UTC without an offset and handed back as UTC with one. A
multivalued scalar is stored as a JSON array and returned as a list. A
structured value is stored as JSON and returned as its text in tables.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa

from ..schema import UID_FIELD, Field, Kind, ObjectType
from .errors import InvalidInput
from .model import iso_utc, to_utc

STRING_LENGTH = 255
PROV_COLUMN = "_prov"

_SCALAR_ARROW: dict[Kind, pa.DataType] = {
    Kind.string: pa.string(),
    Kind.integer: pa.int64(),
    Kind.float: pa.float64(),
    Kind.boolean: pa.bool_(),
    Kind.datetime: pa.timestamp("us", tz="UTC"),
    Kind.date: pa.date32(),
    Kind.enum: pa.string(),
    Kind.ref: pa.string(),
    Kind.json: pa.string(),
}


def is_list(field: Field) -> bool:
    """Stored as a JSON array of scalars and returned as a list."""
    return field.multivalued and field.kind != Kind.json


def arrow_type(field: Field) -> pa.DataType:
    scalar = _SCALAR_ARROW[field.kind]
    return pa.list_(scalar) if is_list(field) else scalar


def arrow_fields(object_type: ObjectType, names: Sequence[str]) -> list[pa.Field]:
    fields = []
    for name in names:
        if name == UID_FIELD:
            fields.append(pa.field(UID_FIELD, pa.string()))
        elif name == PROV_COLUMN:
            fields.append(pa.field(PROV_COLUMN, pa.string()))
        else:
            fields.append(pa.field(name, arrow_type(object_type.field(name))))
    return fields


# -- towards the database ------------------------------------------------------


def _scalar(kind: Kind, value: Any) -> Any:
    """Check one non-null scalar against its kind and return the value to bind."""
    if kind in (Kind.string, Kind.enum, Kind.ref):
        if not isinstance(value, str):
            raise ValueError(f"expected text, got {type(value).__name__}")
        return value
    if kind == Kind.integer:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"expected an integer, got {type(value).__name__}")
        if not -(2**63) <= value < 2**63:
            raise ValueError("integer does not fit in 64 bits")
        return value
    if kind == Kind.float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"expected a number, got {type(value).__name__}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("number must be finite")
        return number
    if kind == Kind.boolean:
        if not isinstance(value, bool):
            raise ValueError(f"expected true or false, got {type(value).__name__}")
        return value
    if kind == Kind.datetime:
        try:
            return to_utc(value, "value").replace(tzinfo=None)
        except InvalidInput as exc:
            raise ValueError(exc.message.removeprefix("value ")) from exc
    if kind == Kind.date:
        if isinstance(value, datetime):
            raise ValueError("expected a date, got a datetime")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"not an ISO 8601 date: {value!r}") from exc
        raise ValueError(f"expected a date, got {type(value).__name__}")
    raise ValueError(f"unsupported kind {kind}")


def _json_element(kind: Kind, value: Any) -> Any:
    bound = _scalar(kind, value)
    if kind == Kind.datetime:
        return iso_utc(bound.replace(tzinfo=UTC))
    if kind == Kind.date:
        return bound.isoformat()
    return bound


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot be stored as JSON: {exc}") from exc


def encode(field: Field, value: Any) -> Any:
    """The SQL parameter for `value`; raises ValueError saying why it does not fit."""
    if value is None:
        return None
    if field.kind == Kind.json:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"not JSON text: {exc}") from exc
        if not isinstance(value, dict | list):
            raise ValueError(f"expected an object or a list, got {type(value).__name__}")
        return _dump(value)
    if field.multivalued:
        if isinstance(value, str) or not isinstance(value, list | tuple):
            raise ValueError(f"expected a list, got {type(value).__name__}")
        return _dump([None if item is None else _json_element(field.kind, item) for item in value])
    return _scalar(field.kind, value)


# -- back from the database ----------------------------------------------------


def _list_items(kind: Kind, text: Any) -> list[Any] | None:
    if text is None:
        return None
    items = json.loads(text) if isinstance(text, str | bytes) else text
    if kind == Kind.float:
        return [None if item is None else float(item) for item in items]
    if kind == Kind.datetime:
        return [None if item is None else datetime.fromisoformat(item) for item in items]
    if kind == Kind.date:
        return [None if item is None else date.fromisoformat(item) for item in items]
    return items


def decode(field: Field, raw: Any) -> Any:
    """The Python value of one stored cell."""
    if raw is None:
        return None
    if field.kind == Kind.json:
        return json.loads(raw) if isinstance(raw, str | bytes) else raw
    if field.multivalued:
        return _list_items(field.kind, raw)
    if field.kind == Kind.boolean:
        return bool(raw)
    if field.kind == Kind.datetime:
        return raw.replace(tzinfo=UTC)
    if field.kind == Kind.float:
        return float(raw)
    return raw


def column(field: Field, values: Sequence[Any]) -> pa.Array:
    """One Arrow column from the stored cells of one property."""
    if field.kind == Kind.json:
        return pa.array(values, pa.string())
    if field.multivalued:
        return pa.array([_list_items(field.kind, value) for value in values], arrow_type(field))
    if field.kind == Kind.boolean:
        return pa.array(values, pa.int8()).cast(pa.bool_())
    # Stored times carry no offset and are UTC; Arrow reads them as such.
    return pa.array(values, arrow_type(field))


def text_column(values: Sequence[Any]) -> pa.Array:
    return pa.array(values, pa.string())
