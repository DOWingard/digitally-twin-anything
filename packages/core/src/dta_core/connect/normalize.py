"""One set of type rules for every connector, so that what a dataset looks like
does not depend on where it is kept.

- every timestamp becomes microseconds in UTC; a value without a zone is read
  as wall-clock time in the dataset's time zone
- text that is entirely ISO 8601 timestamps becomes timestamps (text formats only)
- decimals become 64-bit floats
- dictionary-encoded columns are decoded; large and view strings become plain strings
- fields are nullable and carry no metadata, so batches from different files
  or chunks compare equal when their columns do
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc

from ..log import Log

log = Log("connect.normalize")

UTC_MICROS = pa.timestamp("us", tz="UTC")

# Date and time of day are both required, so that plain years or dates stay what they are.
_ISO_TIMESTAMP = r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}(:?\d{2})?)?$"
_ISO_ZONED = r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}(:?\d{2})?)$"

_ARROW_ERRORS = (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError)


class SchemaMismatch(Exception):
    """A table does not have the columns, or the column types, of the dataset's first table."""

    def __init__(
        self,
        missing: list[str],
        extra: list[str],
        changed: dict[str, tuple[str, str]],
    ) -> None:
        self.missing = missing
        self.extra = extra
        self.changed = changed
        parts = []
        if missing:
            parts.append(f"missing columns {missing}")
        if extra:
            parts.append(f"unexpected columns {extra}")
        if changed:
            described = ", ".join(
                f"{name} ({was} vs {now})" for name, (was, now) in changed.items()
            )
            parts.append(f"columns of another type: {described}")
        super().__init__("; ".join(parts))

    @property
    def columns(self) -> list[str]:
        return [*self.missing, *self.extra, *self.changed]


def normalize_table(
    table: pa.Table,
    *,
    timezone: str = "UTC",
    detect_iso_text: bool = False,
    keep_text: Collection[str] = (),
) -> pa.Table:
    """Apply the type rules to every column.

    `detect_iso_text` turns text columns whose values are all ISO 8601
    timestamps into timestamps; columns named in `keep_text` are left as
    text because their type was declared.
    """
    arrays = []
    fields = []
    for field, column in zip(table.schema, table.columns, strict=True):
        normalized = normalize_column(
            column,
            name=field.name,
            timezone=timezone,
            detect_iso_text=detect_iso_text and field.name not in keep_text,
        )
        arrays.append(normalized)
        fields.append(pa.field(field.name, normalized.type, nullable=True))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def normalize_column(
    column: pa.ChunkedArray,
    *,
    name: str,
    timezone: str = "UTC",
    detect_iso_text: bool = False,
) -> pa.ChunkedArray:
    kind = column.type
    if pa.types.is_dictionary(kind):
        column = column.cast(kind.value_type)
        kind = column.type
    if pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
        column = column.cast(pa.string())
        kind = column.type
    if pa.types.is_decimal(kind):
        return column.cast(pa.float64())
    if pa.types.is_timestamp(kind):
        return timestamps_to_utc(column, timezone=timezone, name=name)
    if detect_iso_text and pa.types.is_string(kind):
        parsed = parse_iso_timestamps(column, timezone=timezone, name=name)
        if parsed is not None:
            return parsed
    return column


def timestamps_to_utc(column: pa.ChunkedArray, *, timezone: str, name: str) -> pa.ChunkedArray:
    """Timestamps of any unit as microseconds in UTC; zone-less values are local to `timezone`."""
    kind = column.type
    if kind.tz is None:
        column = assume_timezone(column, timezone=timezone, name=name)
    # Nanoseconds lose their sub-microsecond part, which an unsafe cast allows; a
    # coarser unit can only overflow, which the safe cast reports.
    return column.cast(UTC_MICROS, safe=kind.unit != "ns")


def assume_timezone(column: pa.ChunkedArray, *, timezone: str, name: str) -> pa.ChunkedArray:
    if timezone == "UTC":
        return column.cast(pa.timestamp(column.type.unit, tz="UTC"))
    try:
        return pc.assume_timezone(column, timezone=timezone)
    except pa.ArrowInvalid as exc:
        # Local times that fall in a daylight-saving gap or overlap have no single
        # instant; the earlier reading is taken rather than failing the whole read.
        log.warn(
            "local times without a single instant read as the earlier one",
            column=name,
            timezone=timezone,
            error=str(exc).splitlines()[0],
        )
        return pc.assume_timezone(
            column, timezone=timezone, ambiguous="earliest", nonexistent="earliest"
        )


def parse_iso_timestamps(
    column: pa.ChunkedArray, *, timezone: str, name: str
) -> pa.ChunkedArray | None:
    """The column as timestamps when every value is an ISO 8601 timestamp, else None."""
    if len(column) == 0 or column.null_count == len(column):
        return None
    if not pc.all(pc.match_substring_regex(column, _ISO_TIMESTAMP)).as_py():
        return None
    zoned = pc.match_substring_regex(column, _ISO_ZONED)
    zoned_count = pc.sum(zoned).as_py() or 0
    valid = len(column) - column.null_count
    try:
        if zoned_count == valid:
            return _parse_text(column, pa.timestamp("us", tz="UTC"))
        if zoned_count == 0:
            return timestamps_to_utc(
                _parse_text(column, pa.timestamp("us")), timezone=timezone, name=name
            )
        null_text = pa.scalar(None, pa.string())
        aware = _parse_text(pc.if_else(zoned, column, null_text), pa.timestamp("us", tz="UTC"))
        local = timestamps_to_utc(
            _parse_text(pc.if_else(zoned, null_text, column), pa.timestamp("us")),
            timezone=timezone,
            name=name,
        )
        return pc.if_else(zoned, aware, local)
    except _ARROW_ERRORS as exc:
        log.debug("text column kept as text", column=name, reason=str(exc).splitlines()[0])
        return None


def _parse_text(column: pa.ChunkedArray, kind: pa.TimestampType) -> pa.ChunkedArray:
    try:
        return column.cast(kind)
    except pa.ArrowInvalid:
        # More than six fractional digits only parse at nanosecond resolution.
        return column.cast(pa.timestamp("ns", tz=kind.tz)).cast(
            pa.timestamp("us", tz=kind.tz), safe=False
        )


def conform(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """The table with exactly the columns and types of `schema`.

    A column whose type differs is cast when that loses nothing (a column of
    nulls, integers into floats); anything else raises `SchemaMismatch`.
    """
    if table.schema.equals(schema):
        return table
    names = table.column_names
    expected = schema.names
    missing = [name for name in expected if name not in names]
    extra = [name for name in names if name not in expected]
    if missing or extra:
        raise SchemaMismatch(missing, extra, {})
    arrays = []
    changed: dict[str, tuple[str, str]] = {}
    for field in schema:
        column = table.column(field.name)
        if column.type.equals(field.type):
            arrays.append(column)
            continue
        try:
            arrays.append(column.cast(field.type))
        except _ARROW_ERRORS:
            changed[field.name] = (str(field.type), str(column.type))
    if changed:
        raise SchemaMismatch([], [], changed)
    return pa.Table.from_arrays(arrays, schema=schema)


def as_utc_datetime(value: Any) -> datetime:
    """A cursor value meant as a point in time, as an aware datetime in UTC.

    Accepts datetimes (naive ones are taken as UTC), dates (midnight UTC) and
    ISO 8601 text. Raises `ValueError` for anything else.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip())
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(), tzinfo=UTC)
    raise ValueError(f"not a point in time: {type(value).__name__}")


def local_naive(value: datetime, timezone: str) -> datetime:
    """An aware datetime as wall-clock time in `timezone`, without a zone."""
    return value.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


def cursor_scalar(value: Any, kind: pa.DataType) -> pa.Scalar:
    """`value` as a scalar comparable with a column of type `kind`. Raises `ValueError`."""
    if pa.types.is_timestamp(kind):
        moment = as_utc_datetime(value)
        if kind.tz is None:
            moment = moment.replace(tzinfo=None)
        return pa.scalar(moment, type=pa.timestamp("us", tz=kind.tz)).cast(kind, safe=False)
    try:
        if pa.types.is_date(kind) and isinstance(value, str):
            value = date.fromisoformat(value.strip())
        return pa.scalar(value).cast(kind)
    except (*_ARROW_ERRORS, TypeError) as exc:
        raise ValueError(f"cannot compare {type(value).__name__} with {kind}") from exc


def filter_after(table: pa.Table, column: str, since: Any) -> pa.Table:
    """Rows whose `column` is greater than `since`; rows where it is null are dropped."""
    target = table.column(column)
    return table.filter(pc.greater(target, cursor_scalar(since, target.type)))


def empty_like(schema: pa.Schema) -> pa.Table:
    return schema.empty_table()


def micros(delta: timedelta) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
