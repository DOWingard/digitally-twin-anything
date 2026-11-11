"""Renderings of one Arrow table: JSON, an Arrow IPC stream, or Parquet.

Arrow and Parquet are written batch by batch into the response as they are
produced, so a large table is never held a second time as one block of bytes.
JSON renders times as ISO 8601 in UTC, structured values as JSON and missing
values as null.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from dta_core.store import VersionRef
from starlette.responses import Response, StreamingResponse

from .tables import is_json_field, iso, version_headers, version_json, with_version

JSON_MEDIA = "application/json"
ARROW_MEDIA = "application/vnd.apache.arrow.stream"
PARQUET_MEDIA = "application/vnd.apache.parquet"
Format = Literal["json", "arrow", "parquet"]
_BY_MEDIA: dict[str, Format] = {
    JSON_MEDIA: "json",
    "*/*": "json",
    "application/*": "json",
    ARROW_MEDIA: "arrow",
    PARQUET_MEDIA: "parquet",
}
BATCH_ROWS = 65_536
NEXT_CURSOR_HEADER = "x-dta-next-cursor"


def negotiate(requested: str | None, accept: str | None) -> Format:
    """The `format` parameter if given, else the most preferred media type of
    `Accept` that the API produces, else JSON."""
    if requested in ("json", "arrow", "parquet"):
        return requested  # type: ignore[return-value]
    best: tuple[float, int, Format] | None = None
    for position, item in enumerate((accept or "").split(",")):
        media, *options = (part.strip() for part in item.split(";"))
        found = _BY_MEDIA.get(media.lower())
        if found is None:
            continue
        quality = 1.0
        for option in options:
            name, _, value = option.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        candidate = (-quality, position, found)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best is not None else "json"


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return iso(value.astimezone(UTC))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"{type(value).__name__} cannot be rendered as JSON")


def dumps(document: Any) -> bytes:
    return json.dumps(
        document, default=_default, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def rows(table: pa.Table) -> list[dict[str, Any]]:
    """The rows of a table as JSON-ready dictionaries."""
    marked = {field.name for field in table.schema if is_json_field(field)}
    columns: list[list[Any]] = []
    for field, column in zip(table.schema, table.columns, strict=True):
        values = column.to_pylist()
        if field.name in marked:
            values = [None if value is None else json.loads(value) for value in values]
        elif pa.types.is_struct(field.type):
            nested = [child.name for child in field.type if child.name in marked]
            for value in values:
                if value is None:
                    continue
                for name in nested:
                    if value.get(name) is not None:
                        value[name] = json.loads(value[name])
        columns.append(values)
    names = table.column_names
    return [dict(zip(names, row, strict=True)) for row in zip(*columns, strict=True)]


def json_response(
    document: Mapping[str, Any],
    version: VersionRef | None = None,
    headers: Mapping[str, str] | None = None,
    status: int = 200,
) -> Response:
    merged = dict(version_headers(version)) if version is not None else {}
    merged.update(headers or {})
    return Response(
        content=dumps(document), status_code=status, media_type=JSON_MEDIA, headers=merged
    )


class _Chunks:
    """A file-like sink that hands out what was written to it since the last call."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._position = 0
        self.closed = False

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self._parts.append(chunk)
        self._position += len(chunk)
        return len(chunk)

    def flush(self) -> None:
        return None

    def tell(self) -> int:
        return self._position

    def close(self) -> None:
        self.closed = True

    def take(self) -> bytes:
        chunk = b"".join(self._parts)
        self._parts = []
        return chunk


def arrow_chunks(table: pa.Table, batch_rows: int = BATCH_ROWS) -> Iterator[bytes]:
    sink = _Chunks()
    writer = pa.ipc.new_stream(sink, table.schema)
    for batch in table.to_batches(max_chunksize=batch_rows):
        writer.write_batch(batch)
        chunk = sink.take()
        if chunk:
            yield chunk
    writer.close()
    yield sink.take()


def parquet_chunks(table: pa.Table, batch_rows: int = BATCH_ROWS) -> Iterator[bytes]:
    sink = _Chunks()
    writer = pq.ParquetWriter(sink, table.schema)
    for start in range(0, table.num_rows, batch_rows):
        writer.write_table(table.slice(start, batch_rows), row_group_size=batch_rows)
        chunk = sink.take()
        if chunk:
            yield chunk
    writer.close()
    yield sink.take()


def table_response(
    table: pa.Table,
    fmt: Format,
    version: VersionRef | None,
    next_cursor: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Response:
    """One table, rendered as asked. JSON wraps the rows as `{data, next_cursor, version}`
    plus `extra`; Arrow and Parquet carry the version and next cursor in headers."""
    headers = dict(version_headers(version)) if version is not None else {}
    if fmt == "json":
        document: dict[str, Any] = {"data": rows(table), "next_cursor": next_cursor}
        if version is not None:
            document["version"] = version_json(version)
        document.update(extra or {})
        return Response(content=dumps(document), media_type=JSON_MEDIA, headers=headers)
    if next_cursor is not None:
        headers[NEXT_CURSOR_HEADER] = next_cursor
    if version is not None:
        table = with_version(table, version)
    if fmt == "arrow":
        return StreamingResponse(arrow_chunks(table), media_type=ARROW_MEDIA, headers=headers)
    return StreamingResponse(parquet_chunks(table), media_type=PARQUET_MEDIA, headers=headers)
