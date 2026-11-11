"""The type rules every connector applies, each tested once."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from dta_core.connect.normalize import (
    UTC_MICROS,
    SchemaMismatch,
    as_utc_datetime,
    conform,
    cursor_scalar,
    filter_after,
    normalize_table,
)


def _one(column: pa.Array, **kwargs: object) -> pa.ChunkedArray:
    return normalize_table(pa.table({"c": column}), **kwargs).column("c")


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_timestamps_of_every_unit_become_microseconds_in_utc(unit: str) -> None:
    column = pa.array([0, 3_600], pa.timestamp("s")).cast(pa.timestamp(unit))
    result = _one(column)
    assert result.type == UTC_MICROS
    assert result.to_pylist() == [
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(1970, 1, 1, 1, tzinfo=UTC),
    ]


def test_zoned_timestamps_keep_their_instant() -> None:
    column = pa.array([datetime(2024, 1, 1, 12, tzinfo=UTC)], pa.timestamp("ms", tz="Asia/Tokyo"))
    assert _one(column).to_pylist() == [datetime(2024, 1, 1, 12, tzinfo=UTC)]


def test_zone_less_timestamps_are_read_in_the_given_zone() -> None:
    column = pa.array([datetime(2024, 7, 1, 12), datetime(2024, 1, 1, 12)], pa.timestamp("us"))
    result = _one(column, timezone="Europe/Berlin")
    assert result.to_pylist() == [
        datetime(2024, 7, 1, 10, tzinfo=UTC),  # summer time, UTC+2
        datetime(2024, 1, 1, 11, tzinfo=UTC),  # winter time, UTC+1
    ]
    assert _one(column).to_pylist()[0] == datetime(2024, 7, 1, 12, tzinfo=UTC)


def test_local_times_in_a_daylight_saving_gap_are_read_not_refused(captured_logs) -> None:
    column = pa.array([datetime(2024, 3, 31, 2, 30)], pa.timestamp("us"))
    result = _one(column, timezone="Europe/Berlin")
    assert result.type == UTC_MICROS and result.null_count == 0
    assert any("earlier" in m for m in captured_logs.messages("warning"))


def test_nanoseconds_are_truncated_to_microseconds() -> None:
    column = pa.array([1_500], pa.timestamp("ns"))
    assert _one(column).cast(pa.int64()).to_pylist() == [1]


def test_decimals_become_float64() -> None:
    column = pa.array([Decimal("1.25"), None, Decimal("-3.50")], pa.decimal128(10, 2))
    result = _one(column)
    assert result.type == pa.float64()
    assert result.to_pylist() == [1.25, None, -3.5]


def test_dictionary_and_large_strings_become_strings() -> None:
    assert _one(pa.array(["a", "b", "a"]).dictionary_encode()).type == pa.string()
    assert _one(pa.array(["a"], pa.large_string())).type == pa.string()
    assert _one(pa.array(["a"], pa.string_view())).type == pa.string()
    decoded = _one(pa.array([1, 2, 1]).dictionary_encode())
    assert decoded.type == pa.int64() and decoded.to_pylist() == [1, 2, 1]


def test_fields_are_nullable_and_schema_metadata_is_dropped() -> None:
    schema = pa.schema([pa.field("a", pa.int64(), nullable=False)], metadata={b"k": b"v"})
    result = normalize_table(pa.table({"a": [1]}, schema=schema))
    assert result.schema.field("a").nullable and not result.schema.metadata


def test_other_types_are_left_alone() -> None:
    table = pa.table({"i": [1], "f": [1.5], "b": [True], "d": [date(2024, 1, 1)], "s": ["x"]})
    assert normalize_table(table).schema == table.schema


class TestIsoText:
    def test_text_of_zoned_timestamps_becomes_utc(self) -> None:
        column = pa.array(["2024-01-01T00:00:00+02:00", "2024-01-01T01:00:00Z", None])
        result = _one(column, detect_iso_text=True)
        assert result.type == UTC_MICROS
        assert result.to_pylist() == [
            datetime(2023, 12, 31, 22, tzinfo=UTC),
            datetime(2024, 1, 1, 1, tzinfo=UTC),
            None,
        ]

    def test_text_without_zone_is_read_in_the_given_zone(self) -> None:
        column = pa.array(["2024-01-01 12:00", "2024-01-01T13:00:00.5"])
        result = _one(column, detect_iso_text=True, timezone="America/New_York")
        assert result.to_pylist() == [
            datetime(2024, 1, 1, 17, tzinfo=UTC),
            datetime(2024, 1, 1, 18, 0, 0, 500_000, tzinfo=UTC),
        ]

    def test_mixed_zoned_and_local_text_is_parsed_value_by_value(self) -> None:
        column = pa.array(["2024-01-01T00:00:00+01:00", "2024-01-01T00:00:00"])
        result = _one(column, detect_iso_text=True, timezone="Europe/Berlin")
        assert result.to_pylist() == [datetime(2023, 12, 31, 23, tzinfo=UTC)] * 2

    def test_more_than_six_fractional_digits_are_accepted(self) -> None:
        result = _one(pa.array(["2024-01-01T00:00:00.123456789Z"]), detect_iso_text=True)
        assert result.to_pylist() == [datetime(2024, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)]

    @pytest.mark.parametrize(
        "values",
        [
            ["2024-01-01T00:00:00Z", "not a time"],
            ["2024-01-01", "2024-01-02"],
            ["2024", "2025"],
            [None, None],
            ["2024-13-45T99:99:99"],
        ],
    )
    def test_text_that_is_not_all_timestamps_stays_text(self, values: list[str | None]) -> None:
        assert _one(pa.array(values, pa.string()), detect_iso_text=True).type == pa.string()

    def test_detection_is_off_unless_asked_and_skips_declared_columns(self) -> None:
        table = pa.table({"a": ["2024-01-01T00:00:00Z"], "b": ["2024-01-01T00:00:00Z"]})
        assert normalize_table(table).schema.types == [pa.string(), pa.string()]
        result = normalize_table(table, detect_iso_text=True, keep_text={"b"})
        assert result.schema.types == [UTC_MICROS, pa.string()]


class TestConform:
    schema = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.float64())])

    def test_equal_schema_passes_through(self) -> None:
        table = pa.table({"a": [1], "b": [1.0]}, schema=self.schema)
        assert conform(table, self.schema) is table

    def test_lossless_differences_are_cast_and_columns_reordered(self) -> None:
        table = pa.table({"b": pa.array([None], pa.null()), "a": pa.array([1], pa.int32())})
        result = conform(table, self.schema)
        assert result.schema == self.schema and result.to_pylist() == [{"a": 1, "b": None}]

    def test_missing_and_extra_columns_are_named(self) -> None:
        with pytest.raises(SchemaMismatch) as caught:
            conform(pa.table({"a": [1], "c": [2]}), self.schema)
        assert caught.value.missing == ["b"] and caught.value.extra == ["c"]
        assert caught.value.columns == ["b", "c"]

    def test_a_type_that_cannot_be_cast_is_named(self) -> None:
        with pytest.raises(SchemaMismatch) as caught:
            conform(pa.table({"a": ["x"], "b": [1.0]}), self.schema)
        assert caught.value.changed == {"a": ("int64", "string")}


class TestCursor:
    def test_points_in_time_are_read_as_utc(self) -> None:
        assert as_utc_datetime("2024-01-01T02:00:00+02:00") == datetime(2024, 1, 1, tzinfo=UTC)
        assert as_utc_datetime(datetime(2024, 1, 1)) == datetime(2024, 1, 1, tzinfo=UTC)
        assert as_utc_datetime(date(2024, 1, 1)) == datetime(2024, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            as_utc_datetime(12)

    def test_filter_after_is_strictly_greater_and_drops_nulls(self) -> None:
        table = normalize_table(
            pa.table(
                {
                    "t": pa.array(
                        [datetime(2024, 1, 1), datetime(2024, 1, 2), None], pa.timestamp("s")
                    ),
                    "n": [1, 2, 3],
                }
            )
        )
        assert filter_after(table, "t", "2024-01-01T00:00:00Z").column("n").to_pylist() == [2]
        assert filter_after(table, "n", 1).column("n").to_pylist() == [2, 3]

    def test_values_that_cannot_be_compared_are_refused(self) -> None:
        with pytest.raises(ValueError):
            cursor_scalar("abc", pa.int64())
