"""`edge_where` through the API: traversals that follow only the links a filter passes.

In the water network at head, the pipes are P1 (J1 -> J2, 100 m), P2 (J2 -> J3, 200 m),
P3 (J3 -> J4, 150 m), P4 (J2 -> T1, 50 m), P5 (J4 -> J5, 300 m), P6 (T1 -> J3, 60 m), all
open, and P7 (J5 -> J6, 80 m) with no status; the pump PU1 (R1 -> J1) is open.
"""

from __future__ import annotations

from collections import deque

import pytest
from api_testkit import VALVE_REMOVED_SEQ, Api

LONG_PIPES = "Pipe.length:gt:100"  # P2, P3, P5
OPEN_LINKS = "Link.status:eq:open"  # every link but P7

GET_ROUTES = {
    "neighbors": ("/api/v1/graph/neighbors", {"seed": "node:J2"}),
    "neighbors, deep": ("/api/v1/graph/neighbors", {"seed": "node:J2", "depth": 3}),
    "paths": ("/api/v1/graph/paths", {"source": "node:J1", "target": "node:J4"}),
    "edges": ("/api/v1/graph/edges", {}),
    "links": ("/api/v1/objects/Junction/node:J2/links", {}),
}


def edge_set(rows: list[dict]) -> set[tuple]:
    return {(e["src"], e["dst"], e["edge_type"], e["link_uid"]) for e in rows}


def all_edges(api: Api, **params: object) -> list[dict]:
    return api.ok("/api/v1/graph/edges", params={"limit": 10_000, **params})["data"]


def bfs(edges: list[dict], seed: str, depth: int, direction: str) -> dict[str, int]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if direction in ("out", "both"):
            adjacency.setdefault(edge["src"], set()).add(edge["dst"])
        if direction in ("in", "both"):
            adjacency.setdefault(edge["dst"], set()).add(edge["src"])
    found = {seed: 0}
    queue = deque([seed])
    while queue:
        current = queue.popleft()
        if found[current] == depth:
            continue
        for neighbour in adjacency.get(current, ()):
            if neighbour not in found:
                found[neighbour] = found[current] + 1
                queue.append(neighbour)
    return found


def test_the_edge_projection_keeps_the_links_that_pass(api: Api) -> None:
    whole = all_edges(api)
    long_pipes = all_edges(api, edge_where=LONG_PIPES)
    pipes = {e["link_uid"] for e in long_pipes if e["edge_type"] == "Pipe"}
    assert pipes == {"link:P2", "link:P3", "link:P5"}
    # Other link types and the references are untouched.
    assert [e for e in long_pipes if e["edge_type"] != "Pipe"] == [
        e for e in whole if e["edge_type"] != "Pipe"
    ]
    both = all_edges(api, edge_where=[LONG_PIPES, "Pipe.length:lt:250"])
    assert {e["link_uid"] for e in both if e["edge_type"] == "Pipe"} == {"link:P2", "link:P3"}
    open_links = all_edges(api, edge_where=OPEN_LINKS)
    assert {e["link_uid"] for e in whole} - {e["link_uid"] for e in open_links} == {"link:P7"}
    typed = all_edges(api, edge_where=LONG_PIPES, edge_types="Pipe")
    assert {e["link_uid"] for e in typed} == pipes
    before = all_edges(api, edge_where="Valve.valve_type:eq:psv", at=f"#{VALVE_REMOVED_SEQ - 1}")
    assert "Valve" not in {e["edge_type"] for e in before}
    assert "Valve" in {e["edge_type"] for e in all_edges(api, at=f"#{VALVE_REMOVED_SEQ - 1}")}


def test_a_cursor_belongs_to_its_filter(api: Api) -> None:
    whole = all_edges(api, edge_where=LONG_PIPES)
    first = api.ok("/api/v1/graph/edges", params={"limit": 3, "edge_where": LONG_PIPES})
    rest = api.ok(
        "/api/v1/graph/edges",
        params={"limit": 100, "edge_where": LONG_PIPES, "cursor": first["next_cursor"]},
    )
    assert first["data"] + rest["data"] == whole
    for other in ({"edge_where": OPEN_LINKS}, {}):
        refused = api.get(
            "/api/v1/graph/edges", params={"limit": 3, "cursor": first["next_cursor"], **other}
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "api.bad_cursor"
    unfiltered = api.ok("/api/v1/graph/edges", params={"limit": 3})
    refused = api.get(
        "/api/v1/graph/edges",
        params={"limit": 3, "cursor": unfiltered["next_cursor"], "edge_where": LONG_PIPES},
    )
    assert refused.json()["code"] == "api.bad_cursor"


def test_neighbours_follow_only_the_links_that_pass(api: Api) -> None:
    body = api.ok("/api/v1/graph/neighbors", params={"seed": "node:J2", "edge_where": LONG_PIPES})
    assert {row["uid"]: row["depth"] for row in body["nodes"]} == {
        "node:J2": 0,
        "node:J3": 1,
        "sensor:S1": 1,
    }
    assert edge_set(body["edges"]) == {
        ("node:J2", "node:J3", "Pipe", "link:P2"),
        ("sensor:S1", "node:J2", "Sensor.measures", None),
    }


@pytest.mark.parametrize("clauses", [[LONG_PIPES], [OPEN_LINKS], [LONG_PIPES, OPEN_LINKS]])
@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("depth", [1, 2, 3, 5])
def test_neighbours_under_a_filter_match_a_breadth_first_search(
    api: Api, clauses: list[str], direction: str, depth: int
) -> None:
    edges = all_edges(api, edge_where=clauses)
    params = {"seed": "node:J3", "depth": depth, "direction": direction, "edge_where": clauses}
    body = api.ok("/api/v1/graph/neighbors", params=params)
    expected = bfs(edges, "node:J3", depth, direction)
    assert {row["uid"]: row["depth"] for row in body["nodes"]} == expected
    nearer = {uid for uid, d in expected.items() if d < depth}
    wanted = set()
    for e in edges:
        if direction in ("out", "both") and e["src"] in nearer and e["dst"] in expected:
            wanted.add((e["src"], e["dst"], e["edge_type"], e["link_uid"]))
        if direction in ("in", "both") and e["dst"] in nearer and e["src"] in expected:
            wanted.add((e["src"], e["dst"], e["edge_type"], e["link_uid"]))
    assert edge_set(body["edges"]) == wanted


@pytest.mark.parametrize("clauses", [[LONG_PIPES], [OPEN_LINKS], ["Pump.status:eq:closed"]])
@pytest.mark.parametrize("seeds", [["node:J2"], ["node:R1", "node:J5"]])
def test_tables_and_cached_graph_agree_under_a_filter(
    api: Api, clauses: list[str], seeds: list[str]
) -> None:
    for edge_types in (None, "Pipe,Pump"):
        params: dict = {"seed": seeds, "depth": 2, "edge_where": clauses}
        body: dict = {"seeds": seeds, "depth": 2, "edge_where": clauses}
        if edge_types:
            params["edge_types"] = edge_types
            body["edge_types"] = edge_types.split(",")
        local = api.ok("/api/v1/graph/neighbors", params=params)
        response = api.post("/api/v1/graph/subgraph", json=body)
        assert response.status_code == 200, response.text
        cached = response.json()
        assert local["nodes"] == cached["nodes"]
        assert edge_set(local["edges"]) == edge_set(cached["edges"])


def test_the_subgraph_takes_the_filter_in_its_body(api: Api) -> None:
    response = api.post(
        "/api/v1/graph/subgraph",
        json={"seeds": ["node:R1"], "depth": 6, "direction": "out", "edge_where": [LONG_PIPES]},
    )
    assert response.status_code == 200, response.text
    # From R1 the pump reaches J1, whose only pipe out (P1, 100 m) is not long.
    assert [row["uid"] for row in response.json()["nodes"]] == ["node:R1", "node:J1"]
    whole = api.post(
        "/api/v1/graph/subgraph", json={"seeds": ["node:R1"], "depth": 6, "direction": "out"}
    )
    assert len(whole.json()["nodes"]) > 2


def path_nodes(body: dict) -> list[list[str]]:
    return [path["nodes"] for path in body["paths"]]


def test_paths_follow_only_the_links_that_pass(api: Api) -> None:
    ends = {"source": "node:R1", "target": "node:J4", "k": 5}
    assert path_nodes(api.ok("/api/v1/graph/paths", params=ends)) == [
        ["node:R1", "node:J1", "node:J2", "node:J3", "node:J4"],
        ["node:R1", "node:J1", "node:J2", "node:T1", "node:J3", "node:J4"],
    ]
    # P4 (50 m) is closed to this filter, so only the way through P2 is left.
    kept = api.ok("/api/v1/graph/paths", params={**ends, "edge_where": "Pipe.length:ge:100"})
    assert path_nodes(kept) == [["node:R1", "node:J1", "node:J2", "node:J3", "node:J4"]]
    assert api.ok("/api/v1/graph/paths", params={**ends, "edge_where": LONG_PIPES})["paths"] == []
    to_j6 = {"source": "node:J4", "target": "node:J6"}
    assert path_nodes(api.ok("/api/v1/graph/paths", params=to_j6)) == [
        ["node:J4", "node:J5", "node:J6"]
    ]
    closed = api.ok("/api/v1/graph/paths", params={**to_j6, "edge_where": OPEN_LINKS})
    assert closed["paths"] == []


def test_paths_with_weights_and_a_filter(api: Api) -> None:
    ends = {"source": "node:J2", "target": "node:J3", "weight": "length", "k": 3}
    assert path_nodes(api.ok("/api/v1/graph/paths", params=ends)) == [
        ["node:J2", "node:T1", "node:J3"],
        ["node:J2", "node:J3"],
    ]
    body = api.ok("/api/v1/graph/paths", params={**ends, "edge_where": "Pipe.length:gt:55"})
    assert path_nodes(body) == [["node:J2", "node:J3"]]
    assert [path["cost"] for path in body["paths"]] == [200.0]


def test_the_links_of_an_object_under_a_filter(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction/node:J2/links", params={"edge_where": LONG_PIPES})
    found = {(row["direction"], row["edge_type"], row["neighbor"]) for row in body["data"]}
    assert found == {("out", "Pipe", "node:J3"), ("in", "Sensor.measures", "sensor:S1")}
    first = api.ok(
        "/api/v1/objects/Junction/node:J2/links", params={"edge_where": OPEN_LINKS, "limit": 2}
    )
    refused = api.get(
        "/api/v1/objects/Junction/node:J2/links",
        params={"edge_where": LONG_PIPES, "limit": 2, "cursor": first["next_cursor"]},
    )
    assert refused.json()["code"] == "api.bad_cursor"


ERRORS = [
    ("Hose.length:gt:1", 404, "store.unknown_type"),
    ("Node.elevation:gt:1", 400, "graph.bad_edge_filter"),
    ("Sensor.active:eq:true", 400, "graph.bad_edge_filter"),
    ("Link.length:gt:1", 404, "store.unknown_property"),
    ("Pipe.colour:eq:red", 404, "store.unknown_property"),
    ("Pipe.length:between:1", 400, "store.bad_filter"),
    ("Pipe.length:gt:long", 400, "store.bad_filter"),
    ("Pipe.lined:eq:maybe", 400, "store.bad_filter"),
    ("Pump.curve:eq:1", 400, "store.bad_filter"),
    ("length:gt:1", 400, "store.bad_filter"),
    ("Pipe.length", 400, "store.bad_filter"),
]


@pytest.mark.parametrize("route", [*GET_ROUTES, "subgraph"])
@pytest.mark.parametrize("clause, status, code", ERRORS)
def test_bad_edge_filters(api: Api, route: str, clause: str, status: int, code: str) -> None:
    if route == "subgraph":
        response = api.post(
            "/api/v1/graph/subgraph", json={"seeds": ["node:J2"], "edge_where": [clause]}
        )
    else:
        path, params = GET_ROUTES[route]
        response = api.get(path, params={**params, "edge_where": [OPEN_LINKS, clause]})
    assert response.status_code == status, response.text
    problem = response.json()
    assert problem["code"] == code
    if code == "graph.bad_edge_filter":
        assert problem["title"] == "Bad edge filter"
        assert problem["type"] == "urn:dta:error:graph.bad_edge_filter"


def test_the_document_describes_edge_where(api: Api) -> None:
    document = api.ok("/api/v1/openapi.json")
    templates = {path: path for path, _ in GET_ROUTES.values()}
    templates[GET_ROUTES["links"][0]] = "/api/v1/objects/{type}/{uid}/links"
    for path in templates.values():
        operation = document["paths"][path]["get"]
        found = [p for p in operation["parameters"] if p["name"] == "edge_where"]
        assert len(found) == 1, path
        parameter = found[0]
        assert parameter["in"] == "query"
        assert "<LinkType>.<property>:<op>:<value>" in parameter["description"]
        assert parameter["schema"]["anyOf"][0]["type"] == "array"
        assert "400" in operation["responses"] and "404" in operation["responses"]
    body = document["components"]["schemas"]["SubgraphRequest"]["properties"]["edge_where"]
    assert "<LinkType>.<property>:<op>:<value>" in body["description"]
