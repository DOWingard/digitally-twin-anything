"""Whole tables for analysis: node tables, edge tables and slices over versions."""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pytest
from api_testkit import (
    HEAD_SEQ,
    J6_SEQ,
    JUNCTIONS,
    MIGRATION_SEQ,
    VALVE_REMOVED_SEQ,
    Api,
    WaterHistory,
    demand,
    level,
    node,
)


def arrow(api: Api, path: str, **params: object) -> pa.Table:
    response = api.get(path, params={"format": "arrow", **params})
    assert response.status_code == 200, response.text
    return pa.ipc.open_stream(io.BytesIO(response.content)).read_all()


def test_node_table(api: Api) -> None:
    table = arrow(api, "/api/v1/block/nodes/Junction")
    assert table.column("uid").to_pylist() == [node(k) for k in (*JUNCTIONS, "J6")]
    assert table.schema.field("elevation").type == pa.float64()
    earlier = arrow(api, "/api/v1/block/nodes/Junction", at="#5", columns="demand")
    assert earlier.column_names == ["uid", "demand"]
    assert earlier.column("demand").to_pylist() == [demand(k, 5) for k in JUNCTIONS]
    nodes = arrow(api, "/api/v1/block/nodes/Node", columns="elevation")
    assert nodes.column_names == ["uid", "_type", "elevation"]
    assert nodes.num_rows == 8


def test_node_table_as_json_pages(api: Api) -> None:
    first = api.ok("/api/v1/block/nodes/Junction", params={"limit": 4})
    rest = api.ok("/api/v1/block/nodes/Junction", params={"cursor": first["next_cursor"]})
    assert [row["uid"] for row in first["data"] + rest["data"]] == [
        node(k) for k in (*JUNCTIONS, "J6")
    ]


def test_edge_table_of_a_link_type(api: Api) -> None:
    table = arrow(api, "/api/v1/block/edges/Pipe")
    assert table.column_names[:4] == ["src", "dst", "edge_type", "link_uid"]
    assert set(table.column_names[4:]) == {
        "status", "length", "diameter", "roughness", "lined", "material",
    }  # fmt: skip
    assert table.column("link_uid").to_pylist() == [f"link:P{i}" for i in range(1, 8)]
    first = table.slice(0, 1).to_pylist()[0]
    assert (first["src"], first["dst"], first["length"]) == ("node:J1", "node:J2", 100.0)
    before = arrow(api, "/api/v1/block/edges/Pipe", at=f"#{MIGRATION_SEQ - 1}")
    assert "material" not in before.column_names and before.num_rows == 6
    narrow = arrow(api, "/api/v1/block/edges/Pipe", columns="length")
    assert narrow.column_names == ["src", "dst", "edge_type", "link_uid", "length"]


def test_edge_table_of_a_reference(api: Api) -> None:
    table = arrow(api, "/api/v1/block/edges/Reading.sensor")
    assert table.column_names == ["src", "dst", "edge_type", "link_uid"]
    assert table.num_rows == 6
    assert set(table.column("dst").to_pylist()) == {"sensor:S1"}
    assert table.column("link_uid").null_count == 6


def test_edge_table_pages(api: Api) -> None:
    first = api.ok("/api/v1/block/edges/Pipe", params={"limit": 3})
    second = api.ok("/api/v1/block/edges/Pipe", params={"limit": 3, "cursor": first["next_cursor"]})
    third = api.ok("/api/v1/block/edges/Pipe", params={"limit": 3, "cursor": second["next_cursor"]})
    assert third["next_cursor"] is None
    uids = [row["link_uid"] for row in first["data"] + second["data"] + third["data"]]
    assert uids == [f"link:P{i}" for i in range(1, 8)]


@pytest.mark.parametrize(
    "path, params, status, code",
    [
        ("/api/v1/block/edges/Sensor.measures", {"columns": "quantity"}, 400, "api.bad_request"),
        ("/api/v1/block/edges/Hose", {}, 404, "store.unknown_type"),
        ("/api/v1/block/edges/Pipe", {"columns": "colour"}, 404, "store.unknown_property"),
        ("/api/v1/block/nodes/Hose", {}, 404, "store.unknown_type"),
    ],
)
def test_block_errors(api: Api, path: str, params: dict, status: int, code: str) -> None:
    response = api.get(path, params=params)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_state_slice_matches_each_version(api: Api) -> None:
    """Every row of the slice equals the object as listed at that version."""
    sliced = arrow(api, "/api/v1/block/slice/Junction", range="#0..head").to_pylist()
    by_version: dict[int, list[dict]] = {}
    for row in sliced:
        by_version.setdefault(row["_seq"], []).append(row)
    assert min(by_version) == 2  # no junction before the network was loaded
    for seq in range(HEAD_SEQ + 1):
        listed = api.ok("/api/v1/objects/Junction", params={"at": f"#{seq}"})["data"]
        rows = by_version.get(seq, [])
        assert [row["uid"] for row in rows] == [row["uid"] for row in listed], seq
        for got, want in zip(rows, listed, strict=True):
            assert {name: got[name] for name in want} == want, (seq, want["uid"])


def test_state_slice_of_objects_that_come_and_go(api: Api) -> None:
    valves = arrow(api, "/api/v1/block/slice/Valve", range=f"#8..#{VALVE_REMOVED_SEQ + 2}")
    assert valves.column("_seq").to_pylist() == [8, 9]
    j6 = arrow(
        api, "/api/v1/block/slice/Junction", range=f"#{J6_SEQ - 1}..#{J6_SEQ + 1}", uids="node:J6"
    )
    assert j6.column("_seq").to_pylist() == [J6_SEQ, J6_SEQ + 1]


def test_state_slice_options(api: Api, water_history: WaterHistory) -> None:
    table = arrow(
        api,
        "/api/v1/block/slice/Junction",
        range="#4..#12/4",
        properties="demand",
        uids="node:J1,node:J3",
    )
    assert table.column_names == ["_version", "_seq", "_t", "uid", "demand"]
    assert table.to_pylist() == [
        {
            "_version": water_history.versions[seq].id,
            "_seq": seq,
            "_t": water_history.versions[seq].t,
            "uid": uid,
            "demand": demand(uid.split(":")[1], seq if seq != 12 else 12),
        }
        for uid in ("node:J1", "node:J3")
        for seq in (4, 8, 12)
    ]


def test_changes_slice_matches_the_histories(api: Api) -> None:
    changes = api.ok(
        "/api/v1/block/slice/Tank",
        params={"range": "#0..head", "mode": "changes", "properties": "level", "limit": 1000},
    )["data"]
    history = api.ok("/api/v1/objects/Tank/node:T1/history", params={"properties": "level"})
    assert changes == history["data"]
    valves = api.ok("/api/v1/block/slice/Valve", params={"mode": "changes"})["data"]
    assert [(row["_seq"], row["_change"]) for row in valves] == [
        (2, "added"),
        (VALVE_REMOVED_SEQ, "removed"),
    ]


def test_tensor_layout(api: Api, water_history: WaterHistory) -> None:
    body = api.ok(
        "/api/v1/block/slice/Tank",
        params={"range": "#18..head", "layout": "tensor", "properties": "level,elevation"},
    )
    assert body["uids"] == ["node:T1"]
    assert body["properties"] == ["level", "elevation"]
    assert [version["seq"] for version in body["versions"]] == [18, 19, 20, 21]
    cells = {
        (row["uid_index"], row["property_index"], row["version_index"]): row["value"]
        for row in body["data"]
    }
    for index, seq in enumerate((18, 19, 20, 21)):
        expected = level(seq) if seq > 18 else level(14)
        assert cells[(0, 0, index)] == pytest.approx(expected)
        assert cells[(0, 1, index)] == 60.0
    table = arrow(
        api, "/api/v1/block/slice/Tank", range="#18..head", layout="tensor", properties="level"
    )
    assert table.column_names == ["uid_index", "property_index", "version_index", "value"]
    assert table.schema.field("value").type == pa.float64()
    assert json.loads(table.schema.metadata[b"dta:uids"]) == ["node:T1"]
    versions = json.loads(table.schema.metadata[b"dta:versions"])
    assert versions[0]["id"] == water_history.versions[18].id


@pytest.mark.parametrize(
    "params, status, code",
    [
        ({"layout": "tensor", "properties": "name"}, 400, "api.bad_request"),
        ({"properties": "colour"}, 404, "store.unknown_property"),
        ({"range": "#5..#2"}, 400, None),
    ],
)
def test_slice_errors(api: Api, params: dict, status: int, code: str | None) -> None:
    response = api.get("/api/v1/block/slice/Junction", params=params)
    assert response.status_code == status, response.text
    if code:
        assert response.json()["code"] == code


def test_slice_of_an_abstract_class_is_refused(api: Api) -> None:
    response = api.get("/api/v1/block/slice/Node")
    assert response.status_code == 400
    assert response.json()["code"] == "api.bad_request"


def test_slice_too_large(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from dta_runtime.api import slices

    monkeypatch.setattr(slices, "MAX_SLICE_CELLS", 10)
    response = api.get("/api/v1/block/slice/Junction", params={"range": "#0..head"})
    assert response.status_code == 400
    assert response.json()["code"] == "api.slice_too_large"


def test_slice_pages_keep_their_range(api: Api) -> None:
    whole = api.ok("/api/v1/block/slice/Junction", params={"range": "#10..#14", "limit": 1000})[
        "data"
    ]
    found: list[dict] = []
    params: dict = {"range": "#10..#14", "limit": 7}
    while True:
        body = api.ok("/api/v1/block/slice/Junction", params=params)
        found.extend(body["data"])
        if not body["next_cursor"]:
            break
        params = {"range": "#0..#1", "limit": 7, "cursor": body["next_cursor"]}
    assert found == whole and len(whole) == 25
