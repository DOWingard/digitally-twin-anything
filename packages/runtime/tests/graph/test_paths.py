"""k shortest paths on the cached graph, against an exhaustive enumeration."""

from __future__ import annotations

import itertools
import math

import pytest
from api_testkit import VALVE_REMOVED_SEQ, WaterHistory
from dta_core.store import UnknownProperty, VersionRef
from dta_runtime.graph import (
    BadWeight,
    SearchTooBroad,
    build_snapshot,
    edge_weights,
    shortest_paths,
)
from graph_testkit import simple_paths

ENDS = ["node:R1", "node:J1", "node:J2", "node:J3", "node:J4", "node:J5", "node:T1", "sensor:S2"]


def edge_list(history: WaterHistory, at: VersionRef, edge_types: list[str] | None) -> list:
    table = history.twin.store.edges(edge_types, at=at)
    return [
        (row["src"], row["dst"], row["edge_type"], row["link_uid"]) for row in table.to_pylist()
    ]


def lengths(history: WaterHistory, at: VersionRef, edges: list, default: float | None) -> dict:
    stored = {
        row["uid"]: row["length"]
        for row in history.twin.store.table("Pipe", columns=["length"], at=at).to_pylist()
    }
    found = {}
    for edge in edges:
        value = stored.get(edge[3]) if edge[2] == "Pipe" else None
        if value is None:
            value = default
        if value is not None:
            found[edge] = float(value)
    return found


@pytest.mark.parametrize("seq", [VALVE_REMOVED_SEQ - 1, 21])
@pytest.mark.parametrize("direction", ["out", "both"])
def test_hop_counts_match_an_exhaustive_search(
    graph_history: WaterHistory, seq: int, direction: str
) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[seq]
    snapshot = build_snapshot(store, store.ontology_at(at), at)
    edges = edge_list(graph_history, at, None)
    for source, target in itertools.permutations(ENDS, 2):
        expected = simple_paths(edges, source, target, 6, direction)
        found = shortest_paths(snapshot, source, target, 100, 6, direction, None)
        assert [(p.cost, p.nodes) for p in found] == expected[:100], (source, target)
        for path in found:
            assert path.hops == len(path.edges) == len(path.nodes) - 1
            for (u, v), edge in zip(itertools.pairwise(path.nodes), path.edges, strict=True):
                assert {u, v} == {edge[0], edge[1]}
                if direction == "out":
                    assert (edge[0], edge[1]) == (u, v)


@pytest.mark.parametrize("default", [None, 7.0])
def test_weighted_costs_match_an_exhaustive_search(
    graph_history: WaterHistory, default: float | None
) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[VALVE_REMOVED_SEQ - 1]
    ontology = store.ontology_at(at)
    snapshot = build_snapshot(store, ontology, at)
    weights = edge_weights(store, ontology, at, snapshot, "length", default)
    edges = edge_list(graph_history, at, None)
    weight = lengths(graph_history, at, edges, default)
    for source, target in itertools.permutations(ENDS, 2):
        expected = simple_paths(edges, source, target, 6, "both", weight)
        found = shortest_paths(
            snapshot, source, target, 5, 6, "both", None, weights, ("length", default)
        )
        assert [p.nodes for p in found] == [nodes for _, nodes in expected[:5]], (source, target)
        for path, (cost, _) in zip(found, expected, strict=False):
            assert math.isclose(path.cost, cost)


def test_k_and_depth_bound_the_answer(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[VALVE_REMOVED_SEQ - 1]
    snapshot = build_snapshot(store, store.ontology_at(at), at)
    three = shortest_paths(snapshot, "node:R1", "node:J4", 3, 6, "out", None)
    assert [p.hops for p in three] == [3, 4, 5]
    one = shortest_paths(snapshot, "node:R1", "node:J4", 1, 6, "out", None)
    assert one[0].nodes == ("node:R1", "node:J1", "node:J3", "node:J4")
    assert shortest_paths(snapshot, "node:R1", "node:J4", 3, 2, "out", None) == []
    assert shortest_paths(snapshot, "node:J4", "node:R1", 3, 6, "out", None) == []
    same = shortest_paths(snapshot, "node:J4", "node:J4", 3, 6, "out", None)
    assert [(p.nodes, p.cost) for p in same] == [(("node:J4",), 0.0)]
    pipes = shortest_paths(snapshot, "node:R1", "node:J4", 3, 6, "both", ["Pipe"])
    assert pipes == []


def test_weights_must_be_numeric_properties(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.head
    ontology = store.ontology_at(at)
    snapshot = build_snapshot(store, ontology, at)
    with pytest.raises(UnknownProperty):
        edge_weights(store, ontology, at, snapshot, "colour", None)
    with pytest.raises(BadWeight):
        edge_weights(store, ontology, at, snapshot, "material", None)


def test_searches_that_are_too_broad_are_refused(
    graph_history: WaterHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dta_runtime.graph import queries

    store = graph_history.twin.store
    at = graph_history.head
    snapshot = build_snapshot(store, store.ontology_at(at), at)
    monkeypatch.setattr(queries, "MAX_SEARCH_WALKS", 1)
    with pytest.raises(SearchTooBroad) as refused:
        shortest_paths(snapshot, "node:J2", "node:J3", 3, 6, "both", None)
    assert refused.value.code == "graph.too_broad"
    monkeypatch.setattr(queries, "MAX_SEARCH_WALKS", 10**9)
    monkeypatch.setattr(queries, "MAX_SEARCH_NODES", 2)
    with pytest.raises(SearchTooBroad):
        shortest_paths(snapshot, "node:J2", "node:J3", 3, 6, "both", None)
