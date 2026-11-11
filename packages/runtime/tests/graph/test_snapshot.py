"""The graph of a version as loaded from the store, and the cache that keeps recent ones."""

from __future__ import annotations

import threading

import numpy as np
import pytest
from api_testkit import MIGRATION_SEQ, VALVE_REMOVED_SEQ, WaterHistory
from dta_runtime.graph import EDGE_SCHEMA, GraphCache, build_snapshot
from graph_testkit import Records


def test_the_snapshot_holds_the_versions_edges(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    for seq in (2, 3, VALVE_REMOVED_SEQ - 1, VALVE_REMOVED_SEQ, MIGRATION_SEQ, 21):
        at = graph_history.versions[seq]
        snapshot = build_snapshot(store, store.ontology_at(at), at)
        stored = store.edges(None, at=at)
        assert snapshot.edge_count == stored.num_rows, seq
        assert snapshot.edges.schema.equals(EDGE_SCHEMA)
        ends = set(stored.column("src").to_pylist()) | set(stored.column("dst").to_pylist())
        assert ends <= set(snapshot.index), seq
        uids = snapshot.uids.to_pylist()
        assert uids == sorted(uids)
        assert all(snapshot.index[uid] == position for position, uid in enumerate(uids))
        types = dict(zip(uids, snapshot.node_table(
            np.arange(snapshot.node_count), np.zeros(snapshot.node_count, dtype=np.int32)
        ).column("type").to_pylist(), strict=True))  # fmt: skip
        if seq >= 3:
            assert types["sensor:S1"] == "Sensor"
            assert types["node:T1"] == "Tank"
        assert snapshot.nbytes > 0


def test_an_empty_version_has_an_empty_graph(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    at = graph_history.versions[1]
    snapshot = build_snapshot(store, store.ontology_at(at), at)
    assert snapshot.edge_count == 0
    assert "node:J1" not in snapshot.index


def test_the_cache_evicts_the_least_recently_used(
    graph_history: WaterHistory, logged: Records
) -> None:
    store = graph_history.twin.store
    cache = GraphCache(store, capacity=2)
    first, second, third = (graph_history.versions[seq] for seq in (3, 9, 21))
    for at in (first, second, first, third):
        cache.get(at, store.ontology_at(at))
    assert (cache.builds, cache.hits) == (3, 1)
    assert cache.cached(first.id) and cache.cached(third.id)
    assert not cache.cached(second.id) and len(cache) == 2
    built = logged.named("graph of a version built")
    assert [entry["seq"] for entry in built] == [3, 9, 21]
    assert all(
        {"nodes", "edges", "bytes", "elapsed_ms", "cached_versions"} <= set(entry)
        for entry in built
    )
    assert built[-1]["edges"] == store.edges(None, at=third).num_rows
    evicted = logged.named("graph of a version evicted from the cache")
    assert [entry["seq"] for entry in evicted] == [9]
    assert evicted[0]["bytes"] > 0 and evicted[0]["capacity"] == 2


def test_concurrent_requests_for_a_version_build_it_once(graph_history: WaterHistory) -> None:
    store = graph_history.twin.store
    cache = GraphCache(store, capacity=2)
    at = graph_history.versions[18]
    ontology = store.ontology_at(at)
    barrier = threading.Barrier(8)
    found = []

    def fetch() -> None:
        barrier.wait()
        found.append(cache.get(at, ontology))

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache.builds == 1 and cache.hits == 7
    assert all(snapshot is found[0] for snapshot in found)


def test_a_failed_build_is_logged_and_not_cached(
    graph_history: WaterHistory, logged: Records, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dta_runtime.graph import snapshot as module

    store = graph_history.twin.store
    cache = GraphCache(store, capacity=2)
    at = graph_history.versions[5]

    def broken(*args: object) -> None:
        raise RuntimeError("no graph today")

    monkeypatch.setattr(module, "build_snapshot", broken)
    with pytest.raises(RuntimeError):
        cache.get(at, store.ontology_at(at))
    assert not cache.cached(at.id)
    assert logged.named("building the graph of a version failed")
    monkeypatch.undo()
    assert cache.get(at, store.ontology_at(at)).edge_count == store.edges(None, at=at).num_rows


def test_capacity_must_be_positive(graph_history: WaterHistory) -> None:
    with pytest.raises(ValueError):
        GraphCache(graph_history.twin.store, capacity=0)
