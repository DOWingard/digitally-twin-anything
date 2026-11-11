"""Checks of a source's `options`, so that a malformed entry is reported by its
dotted path before anything is read."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow as pa

from ..sources import SourceConfigError

_TIMESTAMP_TYPE = re.compile(r"^timestamp\[(s|ms|us|ns)(?:,\s*tz=([^\]]+))?\]$")
_DECIMAL_TYPE = re.compile(r"^decimal(128|256)?\(\s*(\d+)\s*,\s*(\d+)\s*\)$")


def invalid(path: str, problem: str) -> SourceConfigError:
    return SourceConfigError(f"option {path}: {problem}", detail={"option": path})


def mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(path, f"must be a mapping, not {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise invalid(path, f"keys must be text, not {type(key).__name__}")
    return value


def only_keys(value: Mapping[str, Any], allowed: Collection[str], path: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        where = f"{path}.{unknown[0]}" if path else unknown[0]
        raise invalid(where, f"unknown option; allowed: {', '.join(sorted(allowed))}")


def text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid(path, "must be non-empty text")
    return value


def number(value: Any, path: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= minimum:
        raise invalid(path, f"must be a number greater than {minimum:g}")
    return float(value)


def timezone(value: Any, path: str) -> str:
    name = text(value, path)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise invalid(path, f"unknown IANA time zone {name!r}") from exc
    return name


# Plain names for the types a manifest's author usually means. Arrow's own
# `float` is 32 bits wide, which loses digits of an ordinary measurement.
_PLAIN_TYPES: dict[str, pa.DataType] = {
    "timestamp": pa.timestamp("us", tz="UTC"),
    "float": pa.float64(),
    "integer": pa.int64(),
    "boolean": pa.bool_(),
    "text": pa.string(),
}


def arrow_type(value: Any, path: str) -> pa.DataType:
    """A column type from its name.

    Plain names (`timestamp`, `float`, `integer`, `boolean`, `text`, `string`,
    `date32`) or any Arrow type name: `int64`, `float32`,
    `timestamp[us, tz=UTC]`, `decimal128(10, 2)`...
    """
    name = text(value, path).strip()
    if name in _PLAIN_TYPES:
        return _PLAIN_TYPES[name]
    if match := _TIMESTAMP_TYPE.match(name):
        zone = match.group(2).strip() if match.group(2) else None
        if zone is not None:
            timezone(zone, path)
        return pa.timestamp(match.group(1), tz=zone)
    if match := _DECIMAL_TYPE.match(name):
        precision, scale = int(match.group(2)), int(match.group(3))
        make = pa.decimal256 if match.group(1) == "256" else pa.decimal128
        try:
            return make(precision, scale)
        except ValueError as exc:
            raise invalid(path, str(exc)) from exc
    try:
        return pa.type_for_alias(name)
    except ValueError as exc:
        raise invalid(path, f"unknown Arrow type name {name!r}") from exc
