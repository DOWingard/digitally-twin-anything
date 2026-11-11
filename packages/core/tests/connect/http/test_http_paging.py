"""Every paging type over several pages, the parameters each request carries, and
`limit` stopping the requests."""

from __future__ import annotations

import dataclasses
from typing import Any

import pyarrow as pa
import pytest
from dta_core.sources import BATCH_ROWS, Connector
from dta_http_testkit import (
    Api,
    Call,
    Reply,
    connect,
    cursor_route,
    link_body_route,
    link_header_route,
    offset_route,
    page_route,
    readings,
    table_rows,
)

ROWS = readings(25)
UTC_US = pa.timestamp("us", tz="UTC")


def _read_all(connector: Connector, dataset: str) -> list[dict[str, Any]]:
    with connector:
        return table_rows(connector, dataset)


def _values(rows: list[dict[str, Any]]) -> list[float]:
    return [row["value"] for row in rows]


EXPECTED = [row["value"] for row in ROWS]


class TestPagingTypes:
    def test_cursor(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        dataset = {
            "path": "/r",
            "params": {"limit": 10, "sensor_id": ["S-0", "S-1"]},
            "records": "data",
            "paging": {"type": "cursor", "cursor_path": "next_cursor", "cursor_param": "cursor"},
        }
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        calls = api.calls_to("/r")
        assert [call.arg("cursor") for call in calls] == [None, "10", "20"]
        # The configured parameters go with every page, a list as a repeated parameter.
        assert all(call.query["limit"] == ["10"] for call in calls)
        assert all(call.query["sensor_id"] == ["S-0", "S-1"] for call in calls)

    @pytest.mark.parametrize("total", [False, True])
    def test_page_number(self, api: Api, total: bool) -> None:
        api.route("/r", page_route(ROWS, 10, total=total))
        paging: dict[str, Any] = {"type": "page", "page_param": "p"}
        if total:
            paging["total_path"] = "pages"
        dataset = {"path": "/r", "params": {"size": 10}, "records": "items", "paging": paging}
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        pages = [call.arg("p") for call in api.calls_to("/r")]
        # Without a total the end is the first empty page.
        assert pages == (["1", "2", "3"] if total else ["1", "2", "3", "4"])
        assert all(call.arg("size") == "10" for call in api.calls_to("/r"))

    def test_page_numbers_from_zero(self, api: Api) -> None:
        api.route("/r", lambda call: page_route(ROWS, 10)(_shift(call)))
        paging = {"type": "page", "page_param": "p", "first_page": 0}
        dataset = {"path": "/r", "records": "items", "paging": paging}
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        assert [call.arg("p") for call in api.calls_to("/r")] == ["0", "1", "2", "3"]

    @pytest.mark.parametrize("total", [False, True])
    def test_offset(self, api: Api, total: bool) -> None:
        api.route("/r", offset_route(ROWS, total=total))
        paging: dict[str, Any] = {
            "type": "offset",
            "limit": 10,
            "offset_param": "skip",
            "limit_param": "take",
        }
        if total:
            paging["total_path"] = "count"
        dataset = {"path": "/r", "params": {"q": "x"}, "records": "results", "paging": paging}
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        calls = api.calls_to("/r")
        expected = ["0", "10", "20"] if total else ["0", "10", "20", "30"]
        assert [call.arg("skip") for call in calls] == expected
        assert all(call.arg("take") == "10" and call.arg("q") == "x" for call in calls)

    @pytest.mark.parametrize("absolute", [False, True])
    def test_link_in_the_body(self, api: Api, absolute: bool) -> None:
        api.route("/r", link_body_route("/r", ROWS, 10, base=api.url if absolute else ""))
        dataset = {
            "path": "/r",
            "params": {"size": 10},
            "records": "data",
            "paging": {"type": "link", "next_url_path": "links.next"},
        }
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        calls = api.calls_to("/r")
        assert [call.arg("from") for call in calls] == [None, "10", "20"]
        # A next link is followed as given: the configured parameters go with the first page.
        assert [call.arg("size") for call in calls] == ["10", None, None]

    def test_link_header(self, api: Api) -> None:
        api.route("/r", link_header_route("/r", ROWS, 10))
        dataset = {"path": "/r", "paging": {"type": "link"}}
        assert _values(_read_all(connect(api, {"r": dataset}), "r")) == EXPECTED
        assert [call.arg("from") for call in api.calls_to("/r")] == [None, "10", "20"]

    def test_no_paging_is_one_request(self, api: Api) -> None:
        api.route("/r", Reply(ROWS))
        rows = _read_all(connect(api, {"r": {"path": "/r"}}), "r")
        assert _values(rows) == EXPECTED
        assert len(api.calls_to("/r")) == 1

    def test_header_goes_with_every_request_and_only_get_is_sent(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        dataset = {
            "path": "/r",
            "records": "data",
            "paging": {"type": "cursor", "cursor_path": "next_cursor"},
        }
        connector = connect(
            api, {"r": dataset}, key="k-123", headers={"X-API-Key": "${DTA_TEST_HTTP_KEY}"}
        )
        _read_all(connector, "r")
        assert len(api.calls) == 3
        assert all(call.method == "GET" for call in api.calls)
        assert all(call.headers.get("X-API-Key") == "k-123" for call in api.calls)
        assert all(call.headers.get("User-Agent") == "dta-connect" for call in api.calls)


def _shift(call: Call) -> Call:
    """A zero-based page number seen as the one-based route expects."""
    page = int(call.arg("p") or 0)
    return dataclasses.replace(call, query={**call.query, "p": [str(page + 1)]})


class TestLimitAndBatches:
    def _cursor_dataset(self) -> dict[str, Any]:
        return {
            "path": "/r",
            "records": "data",
            "paging": {"type": "cursor", "cursor_path": "next_cursor"},
        }

    def test_limit_stops_requesting_pages(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with connect(api, {"r": self._cursor_dataset()}) as connector:
            rows = table_rows(connector, "r", limit=15)
        assert _values(rows) == EXPECTED[:15]
        assert len(api.calls_to("/r")) == 2

    def test_limit_of_a_whole_page(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with connect(api, {"r": self._cursor_dataset()}) as connector:
            assert len(table_rows(connector, "r", limit=10)) == 10
        assert len(api.calls_to("/r")) == 1

    def test_limit_zero_gives_the_schema(self, api: Api) -> None:
        api.route("/r", cursor_route(ROWS, 10))
        with connect(api, {"r": self._cursor_dataset()}) as connector:
            tables = list(connector.read("r", limit=0))
        assert [t.num_rows for t in tables] == [0]
        assert tables[0].schema.field("timestamp").type == UTC_US

    def test_large_pages_come_in_batches(self, api: Api) -> None:
        many = readings(BATCH_ROWS + 10)
        api.route("/r", Reply({"data": many, "next_cursor": None}))
        with connect(api, {"r": self._cursor_dataset()}) as connector:
            sizes = [t.num_rows for t in connector.read("r")]
        assert sizes == [BATCH_ROWS, 10]
