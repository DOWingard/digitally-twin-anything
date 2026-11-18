"""The `options` of an HTTP source, checked and typed before anything is requested.

Option values arrive with their `${NAME}` placeholders already filled in, so a
header or parameter value may be a secret: no message here ever repeats a
value, only the dotted path of the option.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import pyarrow as pa
from dlt.common import jsonpath

from .. import options as opt

OPTION_KEYS = ("headers", "clock", "timeout_s", "max_attempts", "max_wait_s", "datasets")
DATASET_KEYS = ("path", "params", "records", "paging", "since", "columns")
CLOCK_KEYS = ("path", "field")
SINCE_KEYS = ("param", "format")
SINCE_FORMATS = ("iso", "epoch_s", "epoch_ms")
PAGING_KEYS: dict[str, tuple[str, ...]] = {
    "none": ("type",),
    "cursor": ("type", "cursor_path", "cursor_param"),
    "page": ("type", "page_param", "first_page", "total_path"),
    "offset": ("type", "limit", "offset_param", "limit_param", "total_path"),
    "link": ("type", "next_url_path"),
}

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_WAIT_S = 60.0

# RFC 9110 token characters, which is all a header name may hold.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

ParamValue = str | int | float | list[str | int | float]


@dataclass(frozen=True)
class Paging:
    """How the pages of a dataset follow one another; each type maps onto one dlt paginator."""

    type: str = "none"
    cursor_path: str | None = None
    cursor_param: str = "cursor"
    page_param: str = "page"
    first_page: int = 1
    offset_param: str = "offset"
    limit_param: str = "limit"
    limit: int | None = None
    total_path: str | None = None
    next_url_path: str | None = None

    def params(self) -> tuple[str, ...]:
        """Query parameters that the paging sets on its own."""
        if self.type == "cursor":
            return (self.cursor_param,)
        if self.type == "page":
            return (self.page_param,)
        if self.type == "offset":
            return (self.offset_param, self.limit_param)
        return ()


@dataclass(frozen=True)
class Since:
    """The query parameter that asks the source for rows newer than a cursor value."""

    param: str
    format: str = "iso"


@dataclass(frozen=True)
class HttpDataset:
    name: str
    path: str
    # Values may have been filled in from secrets; kept out of the repr.
    params: dict[str, ParamValue] = field(default_factory=dict, repr=False)
    records: str | None = None
    paging: Paging = field(default_factory=Paging)
    since: Since | None = None
    # When declared, the columns kept, with their types: a field of the rows that is not
    # named here is not read at all. When not, every field is kept with an inferred type.
    columns: dict[str, pa.DataType] | None = None


@dataclass(frozen=True)
class Clock:
    path: str
    field: str


@dataclass(frozen=True)
class HttpOptions:
    datasets: dict[str, HttpDataset]
    # Header values are usually credentials; kept out of the repr.
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    clock: Clock | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_wait_s: float = DEFAULT_MAX_WAIT_S


def parse_options(raw: Mapping[str, Any]) -> HttpOptions:
    """Check an HTTP source's options; raises `SourceConfigError` with `detail["option"]`."""
    opt.mapping(raw, "options")
    opt.only_keys(raw, OPTION_KEYS, "")
    headers = _headers(raw.get("headers"))
    clock = _clock(raw.get("clock"))
    timeout_s = DEFAULT_TIMEOUT_S
    if raw.get("timeout_s") is not None:
        timeout_s = opt.number(raw["timeout_s"], "timeout_s")
    max_attempts = DEFAULT_MAX_ATTEMPTS
    if raw.get("max_attempts") is not None:
        max_attempts = _whole(raw["max_attempts"], "max_attempts", minimum=1)
    max_wait_s = DEFAULT_MAX_WAIT_S
    if raw.get("max_wait_s") is not None:
        max_wait_s = opt.number(raw["max_wait_s"], "max_wait_s")
    if raw.get("datasets") is None:
        raise opt.invalid("datasets", "an http source must declare its datasets")
    declared = opt.mapping(raw["datasets"], "datasets")
    if not declared:
        raise opt.invalid("datasets", "must declare at least one dataset")
    datasets = {name: _dataset(name, entry) for name, entry in declared.items()}
    return HttpOptions(datasets, headers, clock, timeout_s, max_attempts, max_wait_s)


def _dataset(name: str, entry: Any) -> HttpDataset:
    path = f"datasets.{name}"
    entry = opt.mapping(entry, path)
    opt.only_keys(entry, DATASET_KEYS, path)
    request_path = _request_path(entry.get("path"), f"{path}.path")
    params = _params(entry.get("params"), f"{path}.params")
    records = None
    if entry.get("records") is not None:
        records = _json_path(entry["records"], f"{path}.records")
    paging = _paging(entry.get("paging"), f"{path}.paging")
    since = _since(entry.get("since"), f"{path}.since")
    columns = _columns(entry.get("columns"), f"{path}.columns")
    reserved = set(paging.params())
    if since is not None:
        if since.param in reserved:
            raise opt.invalid(f"{path}.since.param", "is already set by the paging")
        reserved.add(since.param)
    for key in params:
        if key in reserved:
            raise opt.invalid(f"{path}.params.{key}", "is set by paging or since; remove it here")
    return HttpDataset(name, request_path, params, records, paging, since, columns)


def _request_path(value: Any, path: str) -> str:
    """A path below the location; a full address would bypass it and receive the headers."""
    text = opt.text(value, path).strip()
    parts = urlsplit(text)
    if parts.scheme or parts.netloc:
        raise opt.invalid(path, "must be a path below the location, not a full address")
    if "?" in text or "#" in text:
        raise opt.invalid(path, "must not hold a query or fragment; put parameters under params")
    if ".." in text.split("/"):
        raise opt.invalid(path, "must stay below the location")
    return text


def _params(value: Any, path: str) -> dict[str, ParamValue]:
    if value is None:
        return {}
    params: dict[str, ParamValue] = {}
    for key, item in opt.mapping(value, path).items():
        where = f"{path}.{key}"
        if isinstance(item, list):
            if not item:
                raise opt.invalid(where, "must not be an empty list")
            params[key] = [_param_value(element, where) for element in item]
        else:
            params[key] = _param_value(item, where)
    return params


def _param_value(value: Any, path: str) -> str | int | float:
    if isinstance(value, bool):
        # Written as JSON writes it; Python's `True` is not what an interface expects.
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return value
    raise opt.invalid(path, "must be text, a number or a list of them")


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    headers: dict[str, str] = {}
    for name, item in opt.mapping(value, "headers").items():
        where = f"headers.{name}"
        if not _HEADER_NAME.match(name):
            raise opt.invalid(where, "is not a valid header name")
        if isinstance(item, bool) or not isinstance(item, str | int | float):
            raise opt.invalid(where, "must be text")
        text = str(item)
        if not text or "\r" in text or "\n" in text:
            raise opt.invalid(where, "must be one non-empty line of text")
        headers[name] = text
    return headers


def _clock(value: Any) -> Clock | None:
    if value is None:
        return None
    entry = opt.mapping(value, "clock")
    opt.only_keys(entry, CLOCK_KEYS, "clock")
    return Clock(
        _request_path(entry.get("path"), "clock.path"),
        _json_path(entry.get("field"), "clock.field"),
    )


def _since(value: Any, path: str) -> Since | None:
    if value is None:
        return None
    entry = opt.mapping(value, path)
    opt.only_keys(entry, SINCE_KEYS, path)
    param = opt.text(entry.get("param"), f"{path}.param")
    kind = opt.text(entry.get("format", "iso"), f"{path}.format")
    if kind not in SINCE_FORMATS:
        raise opt.invalid(f"{path}.format", f"must be one of {', '.join(SINCE_FORMATS)}")
    return Since(param, kind)


def _paging(value: Any, path: str) -> Paging:
    if value is None:
        return Paging()
    entry = opt.mapping(value, path)
    kind = opt.text(entry.get("type"), f"{path}.type")
    if kind not in PAGING_KEYS:
        raise opt.invalid(f"{path}.type", f"must be one of {', '.join(PAGING_KEYS)}")
    opt.only_keys(entry, PAGING_KEYS[kind], path)

    def text(key: str, default: str) -> str:
        return opt.text(entry.get(key, default), f"{path}.{key}")

    def json_path(key: str) -> str | None:
        return _json_path(entry[key], f"{path}.{key}") if entry.get(key) is not None else None

    if kind == "cursor":
        cursor_path = _json_path(entry.get("cursor_path"), f"{path}.cursor_path")
        return Paging(kind, cursor_path=cursor_path, cursor_param=text("cursor_param", "cursor"))
    if kind == "page":
        first = _whole(entry.get("first_page", 1), f"{path}.first_page", minimum=0)
        return Paging(
            kind,
            page_param=text("page_param", "page"),
            first_page=first,
            total_path=json_path("total_path"),
        )
    if kind == "offset":
        limit = _whole(entry.get("limit"), f"{path}.limit", minimum=1)
        offset_param = text("offset_param", "offset")
        limit_param = text("limit_param", "limit")
        if offset_param == limit_param:
            raise opt.invalid(f"{path}.limit_param", "must differ from offset_param")
        return Paging(
            kind,
            offset_param=offset_param,
            limit_param=limit_param,
            limit=limit,
            total_path=json_path("total_path"),
        )
    if kind == "link":
        return Paging(kind, next_url_path=json_path("next_url_path"))
    return Paging(kind)


def _columns(value: Any, path: str) -> dict[str, pa.DataType] | None:
    if value is None:
        return None
    declared = opt.mapping(value, path)
    if not declared:
        raise opt.invalid(path, "must declare at least one column")
    return {
        column: opt.arrow_type(type_name, f"{path}.{column}")
        for column, type_name in declared.items()
    }


def _json_path(value: Any, path: str) -> str:
    text = opt.text(value, path).strip()
    try:
        jsonpath.compile_path(text)
    except Exception as exc:  # jsonpath-ng raises its own lexer and parser errors
        raise opt.invalid(path, f"is not a JSON path ({type(exc).__name__})") from None
    return text


def _whole(value: Any, path: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise opt.invalid(path, f"must be a whole number of at least {minimum}")
    return value
