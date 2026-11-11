"""JSON, Arrow IPC and Parquet renderings of the same tables."""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from api_testkit import HEAD_SEQ, Api, WaterHistory

ARROW = "application/vnd.apache.arrow.stream"
PARQUET = "application/vnd.apache.parquet"
UTC_MICROS = pa.timestamp("us", tz="UTC")


def arrow_table(content: bytes) -> pa.Table:
    return pa.ipc.open_stream(io.BytesIO(content)).read_all()


def parquet_table(content: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(content))


@pytest.mark.parametrize(
    "params, headers, media",
    [
        ({"format": "arrow"}, {}, ARROW),
        ({}, {"Accept": ARROW}, ARROW),
        ({}, {"Accept": f"application/json;q=0.4, {ARROW}"}, ARROW),
        ({"format": "parquet"}, {"Accept": ARROW}, PARQUET),
        ({}, {"Accept": PARQUET}, PARQUET),
        ({"format": "json"}, {"Accept": ARROW}, "application/json"),
        ({}, {"Accept": "text/csv"}, "application/json"),
        ({}, {"Accept": f"{ARROW};q=0, */*"}, "application/json"),
    ],
)
def test_negotiation(api: Api, params: dict, headers: dict, media: str) -> None:
    response = api.get("/api/v1/objects/Junction", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == media


def test_the_renderings_agree(api: Api, water_history: WaterHistory) -> None:
    rendered = api.ok("/api/v1/objects/Tank", params={"include": "provenance"})["data"]
    arrow = api.get("/api/v1/objects/Tank", params={"include": "provenance", "format": "arrow"})
    parquet = api.get("/api/v1/objects/Tank", params={"include": "provenance", "format": "parquet"})
    for response, read in ((arrow, arrow_table), (parquet, parquet_table)):
        assert response.headers["x-dta-version-id"] == water_history.head.id
        assert response.headers["x-dta-version-seq"] == str(HEAD_SEQ)
        assert response.headers["x-dta-version-t"] == "2026-01-05T21:00:00Z"
        table = read(response.content)
        metadata = json.loads(table.schema.metadata[b"dta:version"])
        assert metadata["id"] == water_history.head.id and metadata["seq"] == HEAD_SEQ
        assert pa.types.is_list(table.schema.field("volume_curve").type)
        assert table.schema.field("_prov").metadata[b"dta:json"] == b"true"
        (row,) = table.to_pylist()
        assert row["volume_curve"] == rendered[0]["volume_curve"]
        assert json.loads(row["_prov"]) == rendered[0]["_prov"]
        assert row["level"] == rendered[0]["level"]


def test_structured_values_are_json_text_marked_as_such(api: Api) -> None:
    rendered = api.ok("/api/v1/objects/Pump")["data"][0]["curve"]
    for fmt, read in (("arrow", arrow_table), ("parquet", parquet_table)):
        table = read(api.get("/api/v1/objects/Pump", params={"format": fmt}).content)
        field = table.schema.field("curve")
        assert field.type == pa.string() and field.metadata[b"dta:json"] == b"true"
        assert json.loads(table.column("curve")[0].as_py()) == rendered


def test_times_are_utc_microseconds(api: Api) -> None:
    for fmt, read in (("arrow", arrow_table), ("parquet", parquet_table)):
        history = read(
            api.get("/api/v1/objects/Tank/node:T1/history", params={"format": fmt}).content
        )
        assert history.schema.field("_t").type == UTC_MICROS
        readings = read(api.get("/api/v1/objects/Reading", params={"format": fmt}).content)
        assert readings.schema.field("observed_at").type == UTC_MICROS
        assert readings.column("observed_at")[0].as_py().isoformat() == (
            "2026-01-05T04:00:00+00:00"
        )
        sliced = read(
            api.get(
                "/api/v1/block/slice/Tank", params={"format": fmt, "range": "#18..head"}
            ).content
        )
        assert sliced.schema.field("_t").type == UTC_MICROS


def test_arrow_returns_every_row_unless_limited(api: Api) -> None:
    whole = api.get("/api/v1/objects/Node", params={"format": "arrow"})
    assert arrow_table(whole.content).num_rows == 8
    assert "x-dta-next-cursor" not in whole.headers
    first = api.get("/api/v1/objects/Node", params={"format": "arrow", "limit": 5})
    cursor = first.headers["x-dta-next-cursor"]
    rest = api.get("/api/v1/objects/Node", params={"format": "arrow", "cursor": cursor})
    uids = arrow_table(first.content).column("uid").to_pylist()
    uids += arrow_table(rest.content).column("uid").to_pylist()
    assert uids == arrow_table(whole.content).column("uid").to_pylist()


def test_empty_tables_keep_their_schema(api: Api) -> None:
    response = api.get(
        "/api/v1/objects/Junction", params={"format": "arrow", "where": "elevation:gt:1000"}
    )
    table = arrow_table(response.content)
    assert table.num_rows == 0
    assert "elevation" in table.column_names
    parquet = api.get(
        "/api/v1/objects/Junction", params={"format": "parquet", "where": "elevation:gt:1000"}
    )
    assert parquet_table(parquet.content).column_names == table.column_names


def test_errors_are_problems_whatever_the_format(api: Api) -> None:
    response = api.get("/api/v1/objects/Nope", params={"format": "arrow"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
