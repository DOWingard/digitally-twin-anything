"""Traversals that follow only the links a filter passes, on a network whose links are open,
closed or of no status: the reads from the tables and the cached graph against each other and
against plain searches over the edge list with the refused links taken out by hand."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from dta_runtime.filters import parse_edge_where
from dta_runtime.graph import (
    MASKS_PER_VERSION,
    WEIGHTS_PER_VERSION,
    EdgeFilter,
    GraphSnapshot,
    build_snapshot,
    cached_neighbourhood,
    edge_weights,
    follow_mask,
    keep_passing,
    links,
    local_neighbourhood,
    passing_links,
    shortest_paths,
)
from graph_testkit import (
    FilteredNetwork,
    Records,
    distances,
    network_edges,
    reached_edges,
    simple_paths,
)

Keep = Callable[[str, dict[str, Any]], bool]

# Clauses, and the same filter written by hand: whether a link of a type passes.
FILTERS: dict[str, tuple[list[str], Keep]] = {
    "one link type": (
        ["Pipe.status:eq:open"],
        lambda kind, row: kind != "Pipe" or row["status"] == "open",
    ),
    "two link types": (
        ["Pipe.status:eq:open", "Pipe.length:lt:60", "Valve.status:ne:closed"],
        lambda kind, row: (
            (row["status"] == "open" and row["length"] < 60)
            if kind == "Pipe"
            # A comparison with a missing value passes nothing, as in SQL.
            else (row["status"] is not None and row["status"] != "closed")
            if kind == "Valve"
            else True
        ),
    ),
    "an abstract class": (
        ["Link.status:in:open"],
        lambda kind, row: row["status"] == "open",
    ),
    "missing values": (
        ["Link.status:null"],
        lambda kind, row: row["status"] is None,
    ),
}
EDGE_TYPES = [None, ["Pipe", "Valve", "Sensor.measures"]]
SEEDS = [
    {"node:G000": "Junction"},
    {"node:G137": "Junction", "sensor:G004": "Sensor"},
    {"node:A1": "Junction"},
]


@pytest.fixture(scope="module")
def snapshot(filtered_network: FilteredNetwork) -> GraphSnapshot:
    store = filtered_network.twin.store
    at = filtered_network.version
    return build_snapshot(store, store.ontology_at(at), at)


def edge_filter(network: FilteredNetwork, clauses: list[str]) -> EdgeFilter:
    found = parse_edge_where(clauses, network.twin.store.ontology_at(network.version))
    assert found is not None
    return found


def mask_args(network: FilteredNetwork, snapshot: GraphSnapshot, found: EdgeFilter) -> dict:
    store = network.twin.store
    at = network.version
    mask = follow_mask(store, store.ontology_at(at), at, snapshot, found)
    return {"mask": mask, "mask_key": found.key}


def as_sets(result) -> tuple[dict, set]:  # noqa: ANN001
    nodes = {row["uid"]: (row["type"], row["depth"]) for row in result.nodes.to_pylist()}
    edges = {
        (row["src"], row["dst"], row["edge_type"], row["link_uid"])
        for row in result.edges.to_pylist()
    }
    return nodes, edges


def test_the_network_splits_every_link_type(filtered_network: FilteredNetwork) -> None:
    statuses: dict[str, set] = {}
    for kind, row in filtered_network.links.values():
        statuses.setdefault(kind, set()).add(row["status"])
    assert statuses == {kind: {"open", "closed", None} for kind in ("Pipe", "Valve", "Pump")}
    assert len(filtered_network.links) > 1.4 * len(filtered_network.generated)


def test_the_filter_is_a_value(filtered_network: FilteredNetwork) -> None:
    ontology = filtered_network.twin.store.ontology_at(filtered_network.version)
    found = parse_edge_where(["Valve.status:in:open,closed", "Pipe.length:gt:3"], ontology)
    assert found is not None
    assert found.link_types == ("Pipe", "Valve")
    assert found.on("Valve") == [("status", "in", ("open", "closed"))]
    assert found.on("Pump") == []
    again = EdgeFilter.of(
        {"Valve": [("status", "in", ["open", "closed"])], "Pipe": [("length", ">", 3.0)]}
    )
    assert again == found and hash(again) == hash(found) and again.key == found.key
    assert parse_edge_where(["Pipe.length:gt:4"], ontology) != found
    assert parse_edge_where(None, ontology) is None and not EdgeFilter.of({})
    abstract = parse_edge_where(["Link.status:eq:open"], ontology)
    assert abstract is not None and abstract.link_types == ("Pipe", "Pump", "Valve")


@pytest.mark.parametrize("name", list(FILTERS))
def test_tables_and_cached_graph_agree_under_a_filter(
    filtered_network: FilteredNetwork, snapshot: GraphSnapshot, name: str
) -> None:
    clauses, keep = FILTERS[name]
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    found = edge_filter(filtered_network, clauses)
    masked = mask_args(filtered_network, snapshot, found)
    kept = network_edges(filtered_network, keep)
    checked = 0
    for seeds, edge_types, depth, direction in itertools.product(
        SEEDS, EDGE_TYPES, (1, 2, 3, 4), ("out", "in", "both")
    ):
        case = (name, list(seeds), edge_types, depth, direction)
        local = local_neighbourhood(
            store, ontology, at, seeds, depth, direction, edge_types, 10_000, found
        )
        cached = cached_neighbourhood(
            snapshot, seeds, depth, direction, edge_types, 10_000, **masked
        )
        assert local.nodes.equals(cached.nodes), case
        assert as_sets(local)[1] == as_sets(cached)[1], case
        edges = [edge for edge in kept if edge_types is None or edge[2] in edge_types]
        expected = distances(edges, seeds, depth, direction)
        nodes, found_edges = as_sets(cached)
        assert {uid: d for uid, (_, d) in nodes.items()} == expected, case
        assert found_edges == reached_edges(edges, expected, depth, direction), case
        checked += 1
    assert checked == len(SEEDS) * len(EDGE_TYPES) * 4 * 3


def test_a_filter_changes_the_neighbourhood(
    filtered_network: FilteredNetwork, snapshot: GraphSnapshot
) -> None:
    # Of the filters above, at least one leaves out nodes the unfiltered traversal reaches,
    # so that the agreement above is not that of two unfiltered answers.
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    seeds = {"node:G000": "Junction"}
    whole = local_neighbourhood(store, ontology, at, seeds, 4, "both", None, 10_000)
    for clauses, _ in FILTERS.values():
        found = edge_filter(filtered_network, clauses)
        part = local_neighbourhood(store, ontology, at, seeds, 4, "both", None, 10_000, found)
        assert part.nodes.num_rows < whole.nodes.num_rows, clauses


def test_reference_edges_are_never_filtered(
    filtered_network: FilteredNetwork, snapshot: GraphSnapshot
) -> None:
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    # No link passes this filter: what is left is reached through references only.
    found = edge_filter(filtered_network, ["Link.uid:eq:link:none"])
    seeds = {"sensor:G004": "Sensor"}
    local = local_neighbourhood(store, ontology, at, seeds, 3, "both", None, 1000, found)
    cached = cached_neighbourhood(
        snapshot, seeds, 3, "both", None, 1000, **mask_args(filtered_network, snapshot, found)
    )
    assert local.nodes.equals(cached.nodes)
    assert as_sets(local)[1] == as_sets(cached)[1]
    assert {edge[2] for edge in as_sets(cached)[1]} == {"Sensor.measures"}
    assert cached.nodes.num_rows >= 2


@pytest.mark.parametrize("name", list(FILTERS))
def test_links_of_an_object_under_a_filter(filtered_network: FilteredNetwork, name: str) -> None:
    clauses, keep = FILTERS[name]
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    found = edge_filter(filtered_network, clauses)
    kept = set(network_edges(filtered_network, keep))
    for uid in ("node:A1", "node:A3", "node:G000", "node:G137"):
        table = links(store, ontology, at, uid, "Junction", "both", None, found)
        rows = {(r["src"], r["dst"], r["edge_type"], r["link_uid"]) for r in table.to_pylist()}
        assert rows == {edge for edge in kept if uid in edge[:2]}, (name, uid)


@pytest.mark.parametrize("name", list(FILTERS))
def test_the_edge_projection_under_a_filter(filtered_network: FilteredNetwork, name: str) -> None:
    clauses, keep = FILTERS[name]
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    found = edge_filter(filtered_network, clauses)
    table = keep_passing(store.edges(None, at=at), passing_links(store, ontology, at, found))
    rows = [(r["src"], r["dst"], r["edge_type"], r["link_uid"]) for r in table.to_pylist()]
    assert sorted(rows, key=str) == sorted(network_edges(filtered_network, keep), key=str)


def corner_paths(
    network: FilteredNetwork, snapshot: GraphSnapshot, clauses: list[str] | None
) -> list[tuple[str, ...]]:
    masked = mask_args(network, snapshot, edge_filter(network, clauses)) if clauses else {}
    found = shortest_paths(snapshot, "node:A1", "node:A3", 5, 6, "out", None, **masked)
    return [path.nodes for path in found]


def test_a_closed_short_path_leaves_the_longer_one(
    filtered_network: FilteredNetwork, snapshot: GraphSnapshot
) -> None:
    a1, a2, a3, a4, a5 = (f"node:A{index}" for index in range(1, 6))
    assert corner_paths(filtered_network, snapshot, None) == [
        (a1, a3),
        (a1, a2, a3),
        (a1, a4, a5, a3),
    ]
    assert corner_paths(filtered_network, snapshot, ["Pipe.status:eq:open"]) == [
        (a1, a2, a3),
        (a1, a4, a5, a3),
    ]
    assert corner_paths(filtered_network, snapshot, ["Link.status:eq:open"]) == [(a1, a4, a5, a3)]
    assert corner_paths(filtered_network, snapshot, ["Pipe.length:gt:1000"]) == []


def pairs(
    network: FilteredNetwork, kept: list, depth: int, direction: str, sources: int
) -> list[tuple[str, str]]:
    """From random sources, to a few objects within reach over the kept edges and to one
    random object, which mostly is not."""
    rng = random.Random(5)
    nodes = [f"node:{key}" for key in network.generated]
    found = []
    for source in rng.sample(nodes, sources):
        reach = sorted(set(distances(kept, [source], depth, direction)) - {source})
        found += [(source, target) for target in rng.sample(reach, min(3, len(reach)))]
        found.append((source, rng.choice([node for node in nodes if node != source])))
    return found


@pytest.mark.parametrize("name", list(FILTERS))
@pytest.mark.parametrize("direction, depth", [("out", 8), ("both", 6)])
def test_filtered_paths_match_an_exhaustive_search(
    filtered_network: FilteredNetwork,
    snapshot: GraphSnapshot,
    name: str,
    direction: str,
    depth: int,
) -> None:
    clauses, keep = FILTERS[name]
    masked = mask_args(filtered_network, snapshot, edge_filter(filtered_network, clauses))
    kept = network_edges(filtered_network, keep)
    answered = 0
    for source, target in pairs(filtered_network, kept, depth, direction, 20):
        expected = simple_paths(kept, source, target, depth, direction)[:10]
        found = shortest_paths(snapshot, source, target, 10, depth, direction, None, **masked)
        assert [(p.cost, p.nodes) for p in found] == expected, (source, target)
        for path in found:
            assert all(edge in kept for edge in path.edges), (source, target)
        answered += bool(found)
    assert answered >= 3, "too few pairs are joined to tell anything"


def lengths(network: FilteredNetwork, edges: list, default: float | None) -> dict:
    found = {}
    for edge in edges:
        value = None
        if edge[3] is not None:
            kind, row = network.links[edge[3]]
            value = row["length"] if kind == "Pipe" else None
        if value is None:
            value = default
        if value is not None:
            found[edge] = float(value)
    return found


@pytest.mark.parametrize("default", [None, 25.0])
def test_weights_and_a_filter_together(
    filtered_network: FilteredNetwork, snapshot: GraphSnapshot, default: float | None
) -> None:
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    weights = edge_weights(store, ontology, at, snapshot, "length", default)
    weight_key = ("length", default)
    a1, a2, a3, a4, a5 = (f"node:A{index}" for index in range(1, 6))
    open_pipes = mask_args(
        filtered_network, snapshot, edge_filter(filtered_network, ["Pipe.status:eq:open"])
    )
    whole = shortest_paths(snapshot, a1, a3, 5, 6, "out", None, weights, weight_key)
    part = shortest_paths(snapshot, a1, a3, 5, 6, "out", None, weights, weight_key, **open_pipes)
    by_length = ((a1, a4, a5, a3), 30.0)
    if default is None:
        # The valve has no length and there is no default: it is not followed.
        assert [(p.nodes, p.cost) for p in whole] == [by_length, ((a1, a3), 100.0)]
        assert [(p.nodes, p.cost) for p in part] == [by_length]
    else:
        through_valve = ((a1, a2, a3), 10.0 + default)
        assert [(p.nodes, p.cost) for p in whole] == [by_length, through_valve, ((a1, a3), 100.0)]
        assert [(p.nodes, p.cost) for p in part] == [by_length, through_valve]
    for name in ("one link type", "an abstract class"):
        clauses, keep = FILTERS[name]
        masked = mask_args(filtered_network, snapshot, edge_filter(filtered_network, clauses))
        kept = network_edges(filtered_network, keep)
        weight = lengths(filtered_network, kept, default)
        for source, target in pairs(filtered_network, kept, 6, "both", 10):
            expected = simple_paths(kept, source, target, 6, "both", weight)[:5]
            found = shortest_paths(
                snapshot, source, target, 5, 6, "both", None, weights, weight_key, **masked
            )
            assert [p.nodes for p in found] == [nodes for _, nodes in expected], (source, target)
            for path, (cost, _) in zip(found, expected, strict=True):
                assert math.isclose(path.cost, cost)


def expected_mask(network: FilteredNetwork, snapshot: GraphSnapshot, keep: Keep) -> list[bool]:
    found = []
    for row in snapshot.edges.to_pylist():
        if row["link_uid"] is None:
            found.append(True)
        else:
            kind, link = network.links[row["link_uid"]]
            found.append(keep(kind, link))
    return found


def test_masks_and_weights_are_bounded(filtered_network: FilteredNetwork, logged: Records) -> None:
    store = filtered_network.twin.store
    at = filtered_network.version
    ontology = store.ontology_at(at)
    # A snapshot of its own, so the counts are not those of other tests.
    snapshot = build_snapshot(store, ontology, at)
    filters = [edge_filter(filtered_network, [f"Pipe.length:lt:{n}"]) for n in range(1, 41)]
    for found in filters:
        follow_mask(store, ontology, at, snapshot, found)
    for default in range(1, 41):
        edge_weights(store, ontology, at, snapshot, "length", float(default))
    assert MASKS_PER_VERSION == WEIGHTS_PER_VERSION == 16
    assert len(snapshot.masks) == MASKS_PER_VERSION
    assert len(snapshot.weights) == WEIGHTS_PER_VERSION
    assert snapshot.masks.computed == snapshot.weights.computed == 40
    assert filters[0].key not in snapshot.masks and filters[-1].key in snapshot.masks
    assert ("length", 1.0) not in snapshot.weights and ("length", 40.0) in snapshot.weights
    computed = logged.named("derived array computed")
    assert sum(entry["evicted"] for entry in computed) == 2 * (40 - 16)

    # Evicted, then asked for again: computed anew, and still right.
    first = filters[0]
    mask = follow_mask(store, ontology, at, snapshot, first)
    assert snapshot.masks.computed == 41 and len(snapshot.masks) == MASKS_PER_VERSION
    shorter = expected_mask(
        filtered_network, snapshot, lambda kind, row: kind != "Pipe" or row["length"] < 1
    )
    assert mask.tolist() == shorter
    later = expected_mask(
        filtered_network, snapshot, lambda kind, row: kind != "Pipe" or row["length"] < 40
    )
    assert follow_mask(store, ontology, at, snapshot, filters[-1]).tolist() == later
    assert snapshot.masks.computed == 41  # kept: not read again
    weights = edge_weights(store, ontology, at, snapshot, "length", 1.0)
    expected = [
        float(filtered_network.links[row["link_uid"]][1]["length"])
        if row["edge_type"] == "Pipe"
        else 1.0
        for row in snapshot.edges.to_pylist()
    ]
    assert np.allclose(weights, np.asarray(expected))
    # A traversal under the recomputed mask is still the plain search's.
    seeds = {"node:G000": "Junction"}
    result = cached_neighbourhood(
        snapshot, seeds, 4, "both", None, 10_000, mask=mask, mask_key=first.key
    )
    no_pipes = network_edges(filtered_network, lambda kind, row: kind != "Pipe")
    assert {uid: d for uid, (_, d) in as_sets(result)[0].items()} == distances(
        no_pipes, seeds, 4, "both"
    )
