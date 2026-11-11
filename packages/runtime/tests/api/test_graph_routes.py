"""Neighbourhoods, paths, subgraphs and the edge projection through the API."""

from __future__ import annotations

import io
from collections import deque

import pyarrow as pa
import pytest
from api_testkit import SCENARIO, VALVE_REMOVED_SEQ, Api


def nodes_of(body: dict) -> dict[str, tuple[str, int]]:
    return {row["uid"]: (row["type"], row["depth"]) for row in body["nodes"]}


def edges_of(body: dict) -> set[tuple[str, str, str, str | None]]:
    return {(e["src"], e["dst"], e["edge_type"], e["link_uid"]) for e in body["edges"]}


def bfs(edges: list[dict], seeds: list[str], depth: int, direction: str) -> dict[str, int]:
    """Hop distance of every node within `depth` of the seeds, computed from the edge list."""
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if direction in ("out", "both"):
            adjacency.setdefault(edge["src"], set()).add(edge["dst"])
        if direction in ("in", "both"):
            adjacency.setdefault(edge["dst"], set()).add(edge["src"])
    found = dict.fromkeys(seeds, 0)
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        if found[current] == depth:
            continue
        for neighbour in adjacency.get(current, ()):
            if neighbour not in found:
                found[neighbour] = found[current] + 1
                queue.append(neighbour)
    return found


def all_edges(api: Api, **params: object) -> list[dict]:
    return api.ok("/api/v1/graph/edges", params={"limit": 10_000, **params})["data"]


def test_depth_zero_is_the_seeds(api: Api) -> None:
    body = api.ok("/api/v1/graph/neighbors", params={"seed": ["node:J2", "node:T1"], "depth": 0})
    assert nodes_of(body) == {"node:J2": ("Junction", 0), "node:T1": ("Tank", 0)}
    assert body["edges"] == [] and body["truncated"] is False


def test_one_hop(api: Api) -> None:
    body = api.ok("/api/v1/graph/neighbors", params={"seed": "node:J2"})
    assert nodes_of(body) == {
        "node:J2": ("Junction", 0),
        "node:J1": ("Junction", 1),
        "node:J3": ("Junction", 1),
        "node:T1": ("Tank", 1),
        "sensor:S1": ("Sensor", 1),
    }
    assert edges_of(body) == {
        ("node:J1", "node:J2", "Pipe", "link:P1"),
        ("node:J2", "node:J3", "Pipe", "link:P2"),
        ("node:J2", "node:T1", "Pipe", "link:P4"),
        ("sensor:S1", "node:J2", "Sensor.measures", None),
    }


@pytest.mark.parametrize(
    "at, expected",
    [
        ("head", {"node:R1": 0, "node:J1": 1, "node:J2": 2}),
        (f"#{VALVE_REMOVED_SEQ - 1}", {"node:R1": 0, "node:J1": 1, "node:J2": 2, "node:J3": 2}),
    ],
)
def test_direction_and_version(api: Api, at: str, expected: dict[str, int]) -> None:
    body = api.ok(
        "/api/v1/graph/neighbors",
        params={"seed": "node:R1", "depth": 2, "direction": "out", "at": at},
    )
    assert {uid: depth for uid, (_, depth) in nodes_of(body).items()} == expected


def test_edge_types(api: Api) -> None:
    body = api.ok(
        "/api/v1/graph/neighbors",
        params={"seed": "node:J2", "depth": 2, "edge_types": "Sensor.measures,Reading.sensor"},
    )
    assert set(nodes_of(body)) == {"node:J2", "sensor:S1"} | {
        f"reading:S1-{seq}" for seq in range(4, 10)
    }
    assert {edge[2] for edge in edges_of(body)} == {"Sensor.measures", "Reading.sensor"}


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("depth", [3, 5])
def test_deep_neighbourhoods_match_a_breadth_first_search(
    api: Api, direction: str, depth: int
) -> None:
    edges = all_edges(api, at="#9")
    body = api.ok(
        "/api/v1/graph/neighbors",
        params={"seed": "node:J3", "depth": depth, "direction": direction, "at": "#9"},
    )
    expected = bfs(edges, ["node:J3"], depth, direction)
    assert {uid: d for uid, (_, d) in nodes_of(body).items()} == expected
    nearer = {uid for uid, d in expected.items() if d < depth}
    wanted = set()
    for e in edges:
        if direction in ("out", "both") and e["src"] in nearer and e["dst"] in expected:
            wanted.add((e["src"], e["dst"], e["edge_type"], e["link_uid"]))
        if direction in ("in", "both") and e["dst"] in nearer and e["src"] in expected:
            wanted.add((e["src"], e["dst"], e["edge_type"], e["link_uid"]))
    assert edges_of(body) == wanted


@pytest.mark.parametrize(
    "seeds, direction, edge_types, at",
    [
        (["node:J2"], "both", None, "head"),
        (["node:R1"], "out", None, "#9"),
        (["node:J4", "sensor:S2"], "in", None, "head"),
        (["node:J3"], "both", "Pipe,Valve", "#9"),
        (["node:J6"], "both", None, "head"),
    ],
)
def test_tables_and_cached_graph_agree(
    api: Api, seeds: list[str], direction: str, edge_types: str | None, at: str
) -> None:
    params: dict = {"seed": seeds, "depth": 2, "direction": direction, "at": at}
    body: dict = {"seeds": seeds, "depth": 2, "direction": direction}
    if edge_types:
        params["edge_types"] = edge_types
        body["edge_types"] = edge_types.split(",")
    local = api.ok("/api/v1/graph/neighbors", params=params)
    response = api.post("/api/v1/graph/subgraph", params={"at": at}, json=body)
    assert response.status_code == 200, response.text
    cached = response.json()
    assert local["nodes"] == cached["nodes"]
    assert edges_of(local) == edges_of(cached)


def test_limit_truncates_nearest_first(api: Api) -> None:
    response = api.get(
        "/api/v1/graph/neighbors", params={"seed": "node:J2", "depth": 4, "limit": 4}
    )
    body = response.json()
    assert body["truncated"] is True and response.headers["x-dta-truncated"] == "true"
    assert [row["uid"] for row in body["nodes"]] == ["node:J2", "node:J1", "node:J3", "node:T1"]
    kept = {row["uid"] for row in body["nodes"]}
    assert all(e["src"] in kept and e["dst"] in kept for e in body["edges"])
    local = api.get("/api/v1/graph/neighbors", params={"seed": "node:J2", "limit": 4})
    assert local.json()["truncated"] is True
    whole = api.get("/api/v1/graph/neighbors", params={"seed": "node:J2", "limit": 5})
    assert whole.headers["x-dta-truncated"] == "false"


def test_neighbours_as_arrow(api: Api) -> None:
    for part, columns in (("nodes", ["uid", "type", "depth"]), ("edges", None)):
        response = api.get(
            "/api/v1/graph/neighbors",
            params={"seed": "node:J2", "format": "arrow", "part": part, "limit": 3},
        )
        table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
        assert table.schema.metadata[b"dta:truncated"] == b"true"
        assert response.headers["x-dta-truncated"] == "true"
        if columns:
            assert table.column_names == columns and table.num_rows == 3
        else:
            assert table.column_names == ["src", "dst", "edge_type", "link_uid"]


def test_unknown_seeds_and_edge_types(api: Api) -> None:
    response = api.get("/api/v1/graph/neighbors", params={"seed": ["node:J1", "node:nope"]})
    assert response.status_code == 404 and response.json()["code"] == "api.unknown_object"
    response = api.get("/api/v1/graph/neighbors", params={"seed": "node:J1", "edge_types": "Hose"})
    assert response.status_code == 404 and response.json()["code"] == "store.unknown_type"
    response = api.get(
        "/api/v1/graph/neighbors", params={"seed": "link:V1", "at": f"#{VALVE_REMOVED_SEQ}"}
    )
    assert response.status_code == 404


def test_a_link_as_seed_is_a_node_of_its_own(api: Api) -> None:
    body = api.ok("/api/v1/graph/neighbors", params={"seed": "link:P1"})
    assert nodes_of(body) == {"link:P1": ("Pipe", 0)}


def path_nodes(body: dict) -> list[list[str]]:
    return [path["nodes"] for path in body["paths"]]


def test_shortest_paths_by_hops(api: Api) -> None:
    body = api.ok("/api/v1/graph/paths", params={"source": "node:R1", "target": "node:J4", "k": 5})
    assert path_nodes(body) == [
        ["node:R1", "node:J1", "node:J2", "node:J3", "node:J4"],
        ["node:R1", "node:J1", "node:J2", "node:T1", "node:J3", "node:J4"],
    ]
    assert [path["hops"] for path in body["paths"]] == [4, 5]
    assert [path["cost"] for path in body["paths"]] == [4.0, 5.0]
    assert body["paths"][0]["edges"][0] == {
        "src": "node:R1",
        "dst": "node:J1",
        "edge_type": "Pump",
        "link_uid": "link:PU1",
    }


def test_shortest_paths_before_the_valve_was_removed(api: Api) -> None:
    body = api.ok(
        "/api/v1/graph/paths",
        params={"source": "node:R1", "target": "node:J4", "k": 2, "at": "#9", "direction": "out"},
    )
    assert path_nodes(body) == [
        ["node:R1", "node:J1", "node:J3", "node:J4"],
        ["node:R1", "node:J1", "node:J2", "node:J3", "node:J4"],
    ]
    assert body["paths"][0]["edges"][1]["link_uid"] == "link:V1"


def test_paths_follow_direction_and_depth(api: Api) -> None:
    backwards = api.ok(
        "/api/v1/graph/paths", params={"source": "node:R1", "target": "node:J4", "direction": "in"}
    )
    assert backwards["paths"] == []
    reverse = api.ok(
        "/api/v1/graph/paths", params={"source": "node:J4", "target": "node:R1", "direction": "in"}
    )
    assert reverse["paths"][0]["nodes"] == ["node:J4", "node:J3", "node:J2", "node:J1", "node:R1"]
    shallow = api.ok(
        "/api/v1/graph/paths", params={"source": "node:R1", "target": "node:J4", "max_depth": 3}
    )
    assert shallow["paths"] == []


def test_paths_may_be_long_but_not_without_limit(api: Api) -> None:
    ends = {"source": "node:R1", "target": "node:J4", "direction": "out"}
    usual = api.ok("/api/v1/graph/paths", params={**ends, "k": 1})
    deep = api.ok("/api/v1/graph/paths", params={**ends, "max_depth": 64, "k": 1})
    # The limit is a ceiling, not a target: the shortest path is still the answer.
    assert path_nodes(deep) == path_nodes(usual) != []
    refused = api.get("/api/v1/graph/paths", params={**ends, "max_depth": 65})
    assert refused.status_code == 400
    assert refused.json()["code"] == "api.bad_request"


def test_weighted_paths(api: Api) -> None:
    body = api.ok(
        "/api/v1/graph/paths",
        params={"source": "node:J2", "target": "node:J3", "weight": "length", "k": 3},
    )
    assert path_nodes(body) == [["node:J2", "node:T1", "node:J3"], ["node:J2", "node:J3"]]
    assert [path["cost"] for path in body["paths"]] == [110.0, 200.0]
    unweighted = api.ok(
        "/api/v1/graph/paths", params={"source": "node:R1", "target": "node:J2", "weight": "length"}
    )
    assert unweighted["paths"] == []  # the pump has no length and no default was given
    defaulted = api.ok(
        "/api/v1/graph/paths",
        params={
            "source": "node:R1",
            "target": "node:J2",
            "weight": "length",
            "default_weight": 1.5,
            "edge_types": "Pipe,Pump",
        },
    )
    assert defaulted["paths"][0]["cost"] == 101.5


@pytest.mark.parametrize(
    "params, status, code",
    [
        ({"weight": "material"}, 400, "graph.bad_weight"),
        ({"weight": "colour"}, 404, "store.unknown_property"),
        ({"target": "node:nope"}, 404, "api.unknown_object"),
        ({"edge_types": "Hose"}, 404, "store.unknown_type"),
        ({"default_weight": "nan", "weight": "length"}, 400, "api.bad_request"),
        ({"k": 0}, 400, "api.bad_request"),
    ],
)
def test_path_errors(api: Api, params: dict, status: int, code: str) -> None:
    response = api.get(
        "/api/v1/graph/paths", params={"source": "node:J1", "target": "node:J4", **params}
    )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_too_broad_a_path_search(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from dta_runtime.graph import queries

    monkeypatch.setattr(queries, "MAX_SEARCH_NODES", 3)
    response = api.get(
        "/api/v1/graph/paths",
        params={"source": "node:R1", "target": "node:J4", "at": "#8", "k": 1},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "graph.too_broad"


def test_paths_on_a_branch(api: Api) -> None:
    body = api.ok(
        "/api/v1/graph/paths",
        params={"source": "node:J5", "target": "node:J9", "branch": SCENARIO},
    )
    assert path_nodes(body) == [["node:J5", "node:J6", "node:J9"]]
    missing = api.get("/api/v1/graph/paths", params={"source": "node:J5", "target": "node:J9"})
    assert missing.status_code == 404


def test_subgraph_with_types_and_properties(api: Api) -> None:
    response = api.post(
        "/api/v1/graph/subgraph",
        json={
            "seeds": ["sensor:S1"],
            "depth": 3,
            "node_types": ["Node"],
            "properties": ["elevation", "volume_curve", "quantity"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["uid"]: row for row in body["nodes"]}
    assert set(rows) == {"sensor:S1", "node:J2", "node:J1", "node:J3", "node:T1", "node:R1",
                         "node:J4"}  # fmt: skip
    assert rows["sensor:S1"]["quantity"] == "pressure"
    assert rows["node:T1"]["volume_curve"] == [0.0, 10.0, 25.0]
    assert rows["node:J1"]["elevation"] == 50.0 and rows["node:J1"]["quantity"] is None
    assert rows["node:J4"]["depth"] == 3
    kept = set(rows)
    assert all(e["src"] in kept and e["dst"] in kept for e in body["edges"])


def test_subgraph_as_parquet(api: Api) -> None:
    import pyarrow.parquet as pq

    response = api.post(
        "/api/v1/graph/subgraph",
        params={"format": "parquet", "part": "edges"},
        json={"seeds": ["node:J2"], "depth": 1},
    )
    table = pq.read_table(io.BytesIO(response.content))
    assert table.num_rows == 4 and set(table.column("edge_type").to_pylist()) == {
        "Pipe",
        "Sensor.measures",
    }


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"seeds": ["node:J2"], "properties": ["colour"]}, 404, "store.unknown_property"),
        ({"seeds": ["node:J2"], "node_types": ["Hose"]}, 404, "store.unknown_type"),
        ({"seeds": ["node:J2"], "depth": 17}, 400, "api.bad_request"),
        ({"seeds": ["node:J2"], "limit": 0}, 400, "api.bad_request"),
    ],
)
def test_subgraph_errors(api: Api, body: dict, status: int, code: str) -> None:
    response = api.post("/api/v1/graph/subgraph", json=body)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_edge_projection(api: Api) -> None:
    edges = all_edges(api)
    kinds = {e["edge_type"] for e in edges}
    assert kinds == {"Pipe", "Pump", "Sensor.measures", "Reading.sensor"}
    assert len([e for e in edges if e["edge_type"] == "Pipe"]) == 7
    before = all_edges(api, at=f"#{VALVE_REMOVED_SEQ - 1}")
    assert {"src": "node:J1", "dst": "node:J3", "edge_type": "Valve", "link_uid": "link:V1"} in (
        before
    )
    sensors = all_edges(api, edge_types="Sensor.measures")
    assert sensors == [
        {"src": "sensor:S1", "dst": "node:J2", "edge_type": "Sensor.measures", "link_uid": None},
        {"src": "sensor:S2", "dst": "node:T1", "edge_type": "Sensor.measures", "link_uid": None},
    ]
    first = api.ok("/api/v1/graph/edges", params={"limit": 4})
    rest = api.ok("/api/v1/graph/edges", params={"limit": 100, "cursor": first["next_cursor"]})
    assert first["data"] + rest["data"] == edges
