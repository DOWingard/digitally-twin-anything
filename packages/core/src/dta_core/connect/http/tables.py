"""Rows of JSON objects, page by page, as Arrow tables of one schema.

With declared columns every page is built to the declared types: a field that
is not declared is dropped and a declared field that is absent is null. Without
them each column's type is inferred from the values; the first page with rows
fixes the schema and a later page that differs is refused by the read loop.

Times are ISO 8601 text or, in a column declared as a timestamp, whole numbers
counted in the declared unit since the epoch; a time without an offset is UTC.
Objects and lists are kept, as their JSON text, only in a column declared as text.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

from ..base import ReadRequest
from ..normalize import SchemaMismatch, conform, filter_after, normalize_table, parse_iso_timestamps
from .settings import HttpDataset

TIMEZONE = "UTC"

_CONVERSION_ERRORS = (
    pa.ArrowInvalid,
    pa.ArrowTypeError,
    pa.ArrowNotImplementedError,
    OverflowError,
    TypeError,
    ValueError,
)


class RowsError(Exception):
    """A page's rows cannot become the table the options describe.

    `config` is set when the remedy is in the manifest (declare a column, fix a
    name) rather than in what the source sent.
    """

    def __init__(self, message: str, *, config: bool = False, columns: list[str] | None = None):
        super().__init__(message)
        self.config = config
        self.columns = columns or []


def declared_schema(columns: dict[str, pa.DataType], names: list[str]) -> pa.Schema:
    """The schema a read with declared columns yields: the declared types after normalisation."""
    empty = pa.schema([pa.field(name, columns[name]) for name in names]).empty_table()
    return normalize_table(empty, timezone=TIMEZONE).schema


class PageTables:
    """Builds the table of each page of one read."""

    def __init__(self, spec: HttpDataset, request: ReadRequest) -> None:
        self._spec = spec
        self._request = request
        self._needed = request.needed_columns()
        self._asked = list(dict.fromkeys([*(request.columns or ()), *_cursor(request)]))
        self._schema: pa.Schema | None = None
        self._known: list[str] | None = None
        self.produced = False
        if spec.columns is not None:
            missing = [name for name in self._asked if name not in spec.columns]
            if missing:
                raise _no_column(spec.name, missing)
            names = list(self._needed) if self._needed is not None else list(spec.columns)
            self._schema = declared_schema(spec.columns, names)

    def page(self, rows: list[Any]) -> pa.Table | None:
        """The table of one page's rows; None for a page without rows."""
        if not rows:
            return None
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RowsError(f"row {index} is {_json_kind(row)}, not a JSON object")
        if self._schema is not None:
            table = self._declared(rows, self._schema)
        else:
            table = self._inferred(rows)
        request = self._request
        if request.cursor_column is not None and request.since is not None:
            try:
                table = filter_after(table, request.cursor_column, request.since)
            except (ValueError, *_CONVERSION_ERRORS) as exc:
                raise RowsError(
                    f"since cannot be compared with column {request.cursor_column!r}: {exc}",
                    config=True,
                    columns=[request.cursor_column],
                ) from None
        if request.columns is not None:
            table = table.select(list(request.columns))
        self.produced = True
        return table

    def empty(self) -> pa.Table:
        """The one table of a read that found no rows; only declared columns can give it."""
        if self._schema is None:
            raise RowsError(
                f"dataset {self._spec.name!r} returned no rows, so its columns cannot be "
                f"inferred; declare them under datasets.{self._spec.name}.columns",
                config=True,
            )
        table = self._schema.empty_table()
        if self._request.columns is not None:
            table = table.select(list(self._request.columns))
        return table

    # -- declared columns ------------------------------------------------------

    def _declared(self, rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
        declared = self._spec.columns or {}
        arrays = [
            declared_column([row.get(name) for row in rows], declared[name], name)
            for name in schema.names
        ]
        table = normalize_table(pa.Table.from_arrays(arrays, names=schema.names), timezone=TIMEZONE)
        try:
            return conform(table, schema)
        except SchemaMismatch as exc:
            raise RowsError(f"the rows do not fit the declared columns: {exc}") from None

    # -- inferred columns ------------------------------------------------------

    def _inferred(self, rows: list[dict[str, Any]]) -> pa.Table:
        keys = list(dict.fromkeys(key for row in rows for key in row))
        if self._known is None:
            missing = [name for name in self._asked if name not in keys]
            if missing:
                raise _no_column(self._spec.name, missing)
            self._known = keys
        if self._needed is not None:
            names = list(self._needed)
        else:
            # Fields this page adds stay in, so that the read loop reports the change.
            known = set(self._known)
            names = [*self._known, *(key for key in keys if key not in known)]
        arrays = [inferred_column([row.get(name) for row in rows], name) for name in names]
        table = pa.Table.from_arrays(arrays, names=names)
        return normalize_table(table, timezone=TIMEZONE, detect_iso_text=True)


def declared_column(values: list[Any], kind: pa.DataType, name: str) -> pa.Array | pa.ChunkedArray:
    """One column's values as the declared type, refusing anything that would lose data."""
    if pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
        try:
            return pa.array(values, type=pa.string())
        except _CONVERSION_ERRORS:
            return pa.array([_as_text(value) for value in values], type=pa.string())
    if pa.types.is_timestamp(kind):
        return _timestamps(values, kind, name)
    inferred = inferred_column(values, name, declared=True)
    if inferred.type.equals(kind):
        return inferred
    try:
        # A safe cast: a fraction into an integer or an overflow is refused, not truncated.
        return inferred.cast(kind)
    except _CONVERSION_ERRORS as exc:
        raise RowsError(
            f"column {name!r} cannot be read as {kind}: {_first_line(exc)}", columns=[name]
        ) from None


def inferred_column(values: list[Any], name: str, *, declared: bool = False) -> pa.Array:
    try:
        array = pa.array(values)
    except _CONVERSION_ERRORS:
        raise RowsError(
            f"column {name!r} holds values of different JSON types; declare its type in columns",
            columns=[name],
        ) from None
    if pa.types.is_nested(array.type):
        raise RowsError(
            f"column {name!r} holds objects or lists; declare it as string in columns to "
            "keep them as JSON text",
            config=not declared,
            columns=[name],
        )
    return array


def _timestamps(values: list[Any], kind: pa.DataType, name: str) -> pa.Array | pa.ChunkedArray:
    present = [value for value in values if value is not None]
    if not present:
        return pa.nulls(len(values), kind)
    if all(isinstance(value, str) for value in present):
        text = pa.chunked_array([pa.array(values, type=pa.string())])
        parsed = parse_iso_timestamps(text, timezone=TIMEZONE, name=name)
        if parsed is None:
            raise RowsError(
                f"column {name!r} holds text that is not an ISO 8601 timestamp", columns=[name]
            )
        return parsed
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        try:
            return pa.array(values, type=pa.int64()).cast(kind)
        except _CONVERSION_ERRORS as exc:
            raise RowsError(
                f"column {name!r} cannot be read as {kind}: {_first_line(exc)}", columns=[name]
            ) from None
    raise RowsError(
        f"column {name!r} cannot be read as {kind}: its values must be ISO 8601 text or "
        "whole numbers since the epoch",
        columns=[name],
    )


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _cursor(request: ReadRequest) -> list[str]:
    return [request.cursor_column] if request.cursor_column is not None else []


def _no_column(dataset: str, missing: list[str]) -> RowsError:
    return RowsError(
        f"dataset {dataset!r} has no column {', '.join(missing)}", config=True, columns=missing
    )


def _json_kind(value: Any) -> str:
    if isinstance(value, list):
        return "a list"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "text"
    if isinstance(value, bool):
        return "a boolean"
    return "a number"


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
