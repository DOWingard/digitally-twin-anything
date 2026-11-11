"""Neighbourhoods read from the tables and from the cached graph, against a plain BFS."""

from __future__ import annotations

import itertools

import pytest
from api_testkit import SCENARIO, VALVE_REMOVED_SEQ, WaterHistory
from dta_core.store import UnknownType, VersionRef
from dta_runtime.graph import (
    GraphCache,
    build_snapshot,
    cached_neighbourhood,
    links,
    local_neighbourhood,
    resolve_types,
    select_kinds,
)
from graph_testkit import distances, reached_edges

SEEDS = ["node:R1", "node:J1", "node:J3", "node:T1", "node:J6", "sensor:S1", "link:P1"]
EDGE_TYPES = [None, ["Pipe", "Valve"], ["Sensor.measures", "Reading.sensor"]]


def versions(history: WaterHistory) -> dict[str, VersionRef]:
    assert history.scenario is not None
    return {
        "sensors": history.versions[3],
        "valve": history.versions[VALVE_REMOVED_SEQ - 1],
        "head": history.head,
        "scenario": history.scenario,
    }


def edge_list(history: WaterHistory, at: VersionRef, edge_types: list[str] | None) -> list:
    table = history.twin.store.edges(edge_types, at=at)
    return [
        (row["src"], row["dst"], row["edge_type"], row["link_uid"]) for row in table.to_pylist()
    ]


def as_sets(result) -> tuple[dict, set]:  # noqa: ANN001
    nodes = {row["uid"]: (row["type"], row["depth"]) for row in result.nodes.to_pylist()}
    edges = {
        (row["src"], row["dst"], row["edge_type"], row["link_uid"])
        for row in result.edges.to_pylist()
    }
    return nodes, edges


@pytest.mark.parametrize("name", ["sensors", "valve", "head", "scenario"])
def test_tables_and_cached_graph_agree(graph_history: WaterHistory, name: str) -> None:
    store = graph_history.twin.store
    at = versions(graph_history)[name]
    ontology = store.ontology_at(at)
    snapshot = build_snapshot(store, ontology, at)
    present = resolve_types(store, ontology, at, SEEDS, verify=True)
    checked = 0
    for uid, edge_types, depth, direction in itertools.product(
        present, EDGE_TYPES, (0, 1, 2), ("out", "in", "both")
    ):
        seeds = {uid: present[uid]}
        local = local_neighbourhood(
            store, ontology, at, seeds, depth, direction, edge_types, 10_000
        )
        cached = cached_neighbourhood(snapshot, seeds, depth, direction, edge_types, 10_000)
        assert local.nodes.equals(cached.nodes), (uid, edge_types, depth, direction)
        assert as_sets(local)[1] == as_sets(cached)[1], (uid, edge_types, depth, direction)
        assert local.truncated is cached.truncated is False
        checked += 1
    assert checked >= 5 * 3 * 3 * 3


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("edge_types", EDGE_TYPES)
def test_cached_neighbourhoods_match_a_plain_bfs(
    graph_history: WaterHistory, direction: str, edge_types: list[str] | None
) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[VALVE_REMOVED_SEQ - 1]
    ontology = store.ontology_at(at)
    snapshot = build_snapshot(store, ontology, at)
    edges = edge_list(graph_history, at, edge_types)
    seeds = {"node:J3": "Junction", "sensor:S2": "Sensor"}
    for depth in range(0, 7):
        result = cached_neighbourhood(snapshot, seeds, depth, direction, edge_types, 10_000)
        nodes, found_edges = as_sets(result)
        expected = distances(edges, seeds, depth, direction)
        assert {uid: d for uid, (_, d) in nodes.items()} == expected, depth
        assert found_edges == reached_edges(edges, expected, depth, direction), depth


def test_several_seeds_and_types(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.head
    ontology = store.ontology_at(at)
    seeds = {"node:R1": "Reservoir", "reading:S1-4": "Reading"}
    result = local_neighbourhood(store, ontology, at, seeds, 2, "both", None, 1000)
    nodes, _ = as_sets(result)
    assert nodes["node:R1"] == ("Reservoir", 0)
    assert nodes["reading:S1-4"] == ("Reading", 0)
    assert nodes["sensor:S1"] == ("Sensor", 1)
    assert nodes["node:J2"] == ("Junction", 2)
    assert nodes["node:J1"] == ("Junction", 1)
    assert result.nodes.column_names == ["uid", "type", "depth"]


def test_limit_keeps_the_nearest(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.head
    ontology = store.ontology_at(at)
    snapshot = build_snapshot(store, ontology, at)
    seeds = {"node:J2": "Junction"}
    for depth in (1, 2):
        local = local_neighbourhood(store, ontology, at, seeds, depth, "both", None, 3)
        cached = cached_neighbourhood(snapshot, seeds, depth, "both", None, 3)
        assert local.truncated and cached.truncated
        assert local.nodes.equals(cached.nodes)
        assert local.nodes.column("uid").to_pylist() == ["node:J2", "node:J1", "node:J3"]
        kept = set(local.nodes.column("uid").to_pylist())
        for result in (local, cached):
            assert set(result.edges.column("src").to_pylist()) <= kept
            assert set(result.edges.column("dst").to_pylist()) <= kept


def test_node_types_filter_the_cached_neighbourhood(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.head
    snapshot = build_snapshot(store, store.ontology_at(at), at)
    result = cached_neighbourhood(
        snapshot, {"sensor:S1": "Sensor"}, 2, "both", None, 1000, frozenset({"Tank"})
    )
    nodes, edges = as_sets(result)
    assert nodes == {"sensor:S1": ("Sensor", 0), "node:T1": ("Tank", 2)}
    assert edges == set()


def test_links_of_an_object(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[VALVE_REMOVED_SEQ - 1]
    ontology = store.ontology_at(at)
    table = links(store, ontology, at, "node:J1", "Junction", "both", None)
    rows = table.to_pylist()
    assert [(r["direction"], r["edge_type"], r["neighbor"]) for r in rows] == [
        ("in", "Pump", "node:R1"),
        ("out", "Pipe", "node:J2"),
        ("out", "Valve", "node:J3"),
    ]
    assert rows[0]["neighbor_type"] == "Reservoir"
    sensors = links(store, ontology, at, "sensor:S1", "Sensor", "both", None).to_pylist()
    assert {(r["direction"], r["edge_type"]) for r in sensors} == {
        ("out", "Sensor.measures"),
        ("in", "Reading.sensor"),
    }


def test_resolve_types(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.head
    ontology = store.ontology_at(at)
    wanted = ["node:J1", "link:P7", "sensor:S2", "reading:S1-9", "node:J9", "link:V1", "zz:1"]
    found = resolve_types(store, ontology, at, wanted, verify=True)
    assert found == {
        "node:J1": "Junction",
        "link:P7": "Pipe",
        "sensor:S2": "Sensor",
        "reading:S1-9": "Reading",
    }
    on_branch = resolve_types(
        store, ontology, store.head(SCENARIO), ["node:J9", "link:P9"], verify=True
    )
    assert on_branch == {"node:J9": "Junction", "link:P9": "Pipe"}


def test_unknown_edge_types(graph_history: WaterHistory) -> None:
    ontology = graph_history.twin.store.ontology
    with pytest.raises(UnknownType):
        select_kinds(ontology, ["Hose"])
    names = {kind.name for kind in select_kinds(ontology, None)}
    assert {"Pipe", "Pump", "Valve", "Sensor.measures", "Reading.sensor"} <= names


def test_the_cache_serves_a_version_once_built(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    cache = GraphCache(store, capacity=2)
    at = graph_history.head
    first = cache.get(at, store.ontology_at(at))
    assert cache.get(at, store.ontology_at(at)) is first
    assert (cache.builds, cache.hits) == (1, 1)
