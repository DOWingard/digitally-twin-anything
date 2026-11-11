"""Rows as tables: declared and inferred columns, projection, nested values, empty
results, and `since` sent to the source and applied to what it returns."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from dta_core.sources import SourceConfigError, SourceReadError
from dta_http_testkit import (
    T0,
    Api,
    Reply,
    connect,
    cursor_route,
    iso,
    readings,
    sequence,
    table_rows,
)

UTC_US = pa.timestamp("us", tz="UTC")
ROWS = readings(25)
COLUMNS = {
    "sensor_id": "string",
    "timestamp": "timestamp[us, tz=UTC]",
    "value": "double",
    "quality": "string",
}
SCHEMA = pa.schema(
    [
        pa.field("sensor_id", pa.string()),
        pa.field("timestamp", UTC_US),
        pa.field("value", pa.float64()),
        pa.field("quality", pa.string()),
    ]
)
CURSOR_PAGING = {"type": "cursor", "cursor_path": "next_cursor"}


def _dataset(**extra: Any) -> dict[str, Any]:
    return {"path": "/r", "records": "data", "paging": CURSOR_PAGING, **extra}


def _tables(api: Api, dataset: dict[str, Any], **kwargs: Any) -> list[pa.Table]:
    with connect(api, {"r": dataset}) as connector:
        return list(connector.read("r", **kwargs))


class TestSince:
    # Three sensors every 15 minutes: rows 6-8 are at 06:30, the instant asked for.
    SINCE = T0 + timedelta(minutes=30)

    @pytest.mark.parametrize(
        ("fmt", "sent"),
        [
            ("iso", "2026-01-19T06:30:00Z"),
            ("epoch_s", str(int(SINCE.timestamp()))),
            ("epoch_ms", str(int(SINCE.timestamp()) * 1000)),
        ],
    )
    def test_sent_in_each_format_and_filtered_after_an_inclusive_source(
        self, api: Api, fmt: str, sent: str
    ) -> None:
        api.route("/r", cursor_route(ROWS, 4, since=("start", fmt)))
        dataset = _dataset(since={"param": "start", "format": fmt})
        with connect(api, {"r": dataset}) as connector:
            rows = table_rows(connector, "r", cursor_column="timestamp", since=self.SINCE)
        calls = api.calls_to("/r")
        assert all(call.arg("start") == sent for call in calls)
        # The source answered from 06:30 on; the rows at 06:30 itself are not newer.
        assert [row["value"] for row in rows] == [row["value"] for row in ROWS[9:]]
        assert min(row["timestamp"] for row in rows) > self.SINCE

    @pytest.mark.parametrize(
        ("fmt", "sent"),
        [
            ("iso", "2026-01-19T06:30:00.500250Z"),
            ("epoch_s", str(int(SINCE.timestamp()))),
            ("epoch_ms", str(int(SINCE.timestamp()) * 1000 + 500)),
        ],
    )
    def test_a_fraction_of_a_second_is_rounded_down(self, api: Api, fmt: str, sent: str) -> None:
        api.route("/r", cursor_route(ROWS, 100, since=("start", fmt)))
        since = self.SINCE + timedelta(milliseconds=500, microseconds=250)
        dataset = _dataset(since={"param": "start", "format": fmt})
        with connect(api, {"r": dataset}) as connector:
            rows = table_rows(connector, "r", cursor_column="timestamp", since=since)
        assert api.calls_to("/r")[0].arg("start") == sent
        assert len(rows) == len(ROWS) - 9

    def test_iso_text_and_other_offsets_are_accepted(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 100, since=("start", "iso")))
        dataset = _dataset(since={"param": "start"})
        with connect(api, {"r": dataset}) as connector:
            rows = table_rows(
                connector, "r", cursor_column="timestamp", since="2026-01-19T07:30:00+01:00"
            )
        assert api.calls_to("/r")[0].arg("start") == "2026-01-19T06:30:00Z"
        assert len(rows) == len(ROWS) - 9

    def test_without_a_since_parameter_only_the_rows_are_filtered(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with connect(api, {"r": _dataset()}) as connector:
            rows = table_rows(connector, "r", cursor_column="timestamp", since=self.SINCE)
        assert len(rows) == len(ROWS) - 9
        assert all(set(call.query) <= {"cursor"} for call in api.calls_to("/r"))

    def test_a_count_since_the_epoch_is_sent_as_it_is(self, api: Api) -> None:
        rows = [{"id": i, "at_ms": 1_700_000_000_000 + i} for i in range(5)]
        api.route("/r", Reply(rows))
        dataset = {
            "path": "/r",
            "since": {"param": "after", "format": "epoch_ms"},
            "columns": {"id": "int64", "at_ms": "double"},
        }
        with connect(api, {"r": dataset}) as connector:
            found = table_rows(connector, "r", cursor_column="at_ms", since=1_700_000_000_002)
            # A fraction is rounded down, so that no newer row is skipped at the source.
            fraction = table_rows(connector, "r", cursor_column="at_ms", since=1_700_000_000_002.5)
        assert [call.arg("after") for call in api.calls_to("/r")] == ["1700000000002"] * 2
        assert [row["id"] for row in found] == [3, 4]
        assert [row["id"] for row in fraction] == [3, 4]

    def test_since_that_is_no_time_is_refused_before_any_request(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        dataset = _dataset(since={"param": "start", "format": "iso"})
        with connect(api, {"r": dataset}) as connector, pytest.raises(SourceConfigError):
            list(connector.read("r", cursor_column="timestamp", since=12))
        assert api.calls_to("/r") == []

    def test_since_that_cannot_be_compared_with_the_cursor(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with connect(api, {"r": _dataset()}) as connector, pytest.raises(SourceConfigError):
            list(connector.read("r", cursor_column="value", since=datetime(2026, 1, 1)))


class TestDeclaredColumns:
    def test_types_projection_dropped_and_missing_fields(self, api: Api) -> None:
        rows = [dict(row, unit="m") for row in ROWS]
        for row in rows[::2]:
            del row["quality"]
        api.route("/r", cursor_route(rows, 10))
        tables = _tables(api, _dataset(columns=COLUMNS))
        assert all(table.schema.equals(SCHEMA) for table in tables)
        found = [row for table in tables for row in table.to_pylist()]
        assert "unit" not in found[0]
        assert [row["quality"] for row in found[:3]] == [None, "good", None]
        assert found[0]["timestamp"] == T0

    def test_projection_keeps_the_order_asked_for(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        since = T0 + timedelta(minutes=30)
        tables = _tables(
            api,
            _dataset(columns=COLUMNS),
            columns=["value", "sensor_id"],
            cursor_column="timestamp",
            since=since,
        )
        assert tables[0].column_names == ["value", "sensor_id"]
        assert sum(t.num_rows for t in tables) == len(ROWS) - 9

    def test_an_unknown_column_is_refused_before_any_request(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with pytest.raises(SourceConfigError) as caught:
            _tables(api, _dataset(columns=COLUMNS), columns=["value", "nope"])
        assert caught.value.detail["columns"] == ["nope"]
        with pytest.raises(SourceConfigError):
            _tables(api, _dataset(columns=COLUMNS), cursor_column="nope", since=T0)
        assert api.calls_to("/r") == []

    def test_an_empty_result_is_one_empty_table_with_the_schema(self, api: Api) -> None:
        api.route("/r", Reply({"data": [], "next_cursor": None}))
        tables = _tables(api, _dataset(columns=COLUMNS))
        assert [t.num_rows for t in tables] == [0]
        assert tables[0].schema.equals(SCHEMA)
        projected = _tables(api, _dataset(columns=COLUMNS), columns=["timestamp"])
        assert projected[0].schema.names == ["timestamp"]
        assert projected[0].schema.field("timestamp").type == UTC_US

    def test_nested_values_are_kept_as_json_text_in_a_text_column(self, api: Api) -> None:
        rows = [
            {"id": 1, "meta": {"a": 1, "b": [1, 2]}, "tags": ["x", "ü"]},
            {"id": 2, "meta": None, "tags": "plain"},
            {"id": 3, "meta": 5, "tags": True},
        ]
        api.route("/r", Reply(rows))
        dataset = {"path": "/r", "columns": {"id": "int64", "meta": "string", "tags": "utf8"}}
        found = [row for table in _tables(api, dataset) for row in table.to_pylist()]
        assert [row["meta"] for row in found] == ['{"a":1,"b":[1,2]}', None, "5"]
        assert [row["tags"] for row in found] == ['["x","ü"]', "plain", "true"]

    def test_nested_values_in_a_column_of_another_type_are_refused(self, api: Api) -> None:
        api.route("/r", Reply([{"id": 1, "meta": {"a": 1}}]))
        dataset = {"path": "/r", "columns": {"id": "int64", "meta": "int64"}}
        with pytest.raises(SourceReadError, match="declare it as string") as caught:
            _tables(api, dataset)
        assert caught.value.detail["columns"] == ["meta"]

    def test_values_are_converted_without_loss_or_refused(self, api: Api) -> None:
        rows = [
            {"n": 1, "x": "41.87", "at": int(T0.timestamp()), "f": 0.5},
            {"n": 2, "x": "3", "at": None, "f": 1},
        ]
        api.route("/r", Reply(rows))
        dataset = {
            "path": "/r",
            "columns": {"n": "int32", "x": "double", "at": "timestamp[s]", "f": "float"},
        }
        table = _tables(api, dataset)[0]
        assert [str(t) for t in table.schema.types] == [
            "int32",
            "double",
            "timestamp[us, tz=UTC]",
            "float",
        ]
        assert table.column("x").to_pylist() == [41.87, 3.0]
        assert table.column("at").to_pylist() == [T0, None]
        api.route("/r", Reply([{"n": 1.5}]))
        with pytest.raises(SourceReadError, match="cannot be read as int64"):
            _tables(api, {"path": "/r", "columns": {"n": "int64"}})
        api.route("/r", Reply([{"at": "yesterday"}]))
        with pytest.raises(SourceReadError, match="not an ISO 8601 timestamp"):
            _tables(api, {"path": "/r", "columns": {"at": "timestamp[us, tz=UTC]"}})


class TestInferredColumns:
    def test_types_are_inferred_and_times_become_utc(self, api: Api) -> None:
        rows = [
            {"id": 1, "at": "2026-01-19T07:00:00+01:00", "v": 1, "ok": True},
            {"id": 2, "at": "2026-01-19T06:15:00", "v": 2.5, "ok": None},
        ]
        api.route("/r", Reply(rows))
        table = _tables(api, {"path": "/r"})[0]
        assert [str(t) for t in table.schema.types] == [
            "int64",
            "timestamp[us, tz=UTC]",
            "double",
            "bool",
        ]
        assert table.column("at").to_pylist() == [T0, T0 + timedelta(minutes=15)]

    def test_the_schema_comes_from_the_first_page_with_rows(self, api: Api) -> None:
        api.route(
            "/r",
            sequence(
                Reply({"data": [], "next_cursor": "1"}),
                Reply({"data": ROWS[:3], "next_cursor": "2"}),
                Reply({"data": [{"sensor_id": "S-9", "value": 2}], "next_cursor": None}),
            ),
        )
        tables = _tables(api, _dataset())
        assert tables[0].schema.equals(SCHEMA)
        found = [row for table in tables for row in table.to_pylist()]
        # A field a later page leaves out is null; an integer joins the floats.
        assert found[-1] == {"sensor_id": "S-9", "timestamp": None, "value": 2.0, "quality": None}

    @pytest.mark.parametrize(
        "later",
        [
            {"sensor_id": "S-9", "timestamp": "late", "value": 1.0, "quality": "good"},
            {"sensor_id": "S-9", "timestamp": iso(T0), "value": 1.0, "extra": 1},
        ],
    )
    def test_a_later_page_that_does_not_conform_is_refused(
        self, api: Api, later: dict[str, Any]
    ) -> None:
        api.route(
            "/r",
            sequence(
                Reply({"data": ROWS[:3], "next_cursor": "1"}),
                Reply({"data": [later], "next_cursor": None}),
            ),
        )
        with pytest.raises(SourceReadError, match="page 2"):
            _tables(api, _dataset())

    def test_an_empty_result_needs_declared_columns(self, api: Api) -> None:
        api.route("/r", Reply({"data": [], "next_cursor": None}))
        with pytest.raises(SourceConfigError, match="datasets.r.columns"):
            _tables(api, _dataset())

    def test_nested_values_need_a_declared_text_column(self, api: Api) -> None:
        api.route("/r", Reply([{"id": 1, "meta": {"a": 1}}]))
        with pytest.raises(SourceConfigError, match="declare it as string") as caught:
            _tables(api, {"path": "/r"})
        assert caught.value.detail["columns"] == ["meta"]

    def test_values_of_different_json_types_are_refused(self, api: Api) -> None:
        api.route("/r", Reply([{"v": 1}, {"v": "one"}]))
        with pytest.raises(SourceReadError, match="different JSON types"):
            _tables(api, {"path": "/r"})

    def test_an_unknown_column_is_refused(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with pytest.raises(SourceConfigError) as caught:
            _tables(api, _dataset(), columns=["nope"])
        assert caught.value.detail["columns"] == ["nope"]


class TestResponseShape:
    @pytest.mark.parametrize(
        ("body", "records"),
        [
            ({"data": ROWS}, None),
            ({"other": ROWS}, "data"),
            ({"data": {"a": 1}}, "data"),
            ({"data": None}, "data"),
            ([1, 2, 3], None),
        ],
    )
    def test_rows_that_are_not_where_records_says_are_refused(
        self, api: Api, body: Any, records: str | None
    ) -> None:
        api.route("/r", Reply(body))
        dataset: dict[str, Any] = {"path": "/r"}
        if records:
            dataset["records"] = records
        with pytest.raises(SourceReadError) as caught:
            _tables(api, dataset)
        assert caught.value.detail["path"] == "/r"
        assert caught.value.detail["dataset"] == "r"
