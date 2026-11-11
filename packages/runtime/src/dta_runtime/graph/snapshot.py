"""The whole edge projection of one version as a rustworkx graph, cached by version.

A version never changes, so a cached graph never goes stale and is only ever
dropped to make room. Node `i` of every graph built for a version is the
object with the `i`-th smallest uid, so node order is uid order and results
built from node indices come out sorted without further work.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import rustworkx as rx
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import Store, VersionRef

from .edges import EDGE_SCHEMA, NODE_SCHEMA, check_direction, edge_kinds, node_types

log = Log("graph.cache")

# Graphs kept per version for combinations of edge types and direction.
VIEWS_PER_VERSION = 8
# Edge masks (which edges a filter lets through) and edge weights kept per version.
MASKS_PER_VERSION = 16
WEIGHTS_PER_VERSION = 16


@dataclass(frozen=True)
class GraphView:
    graph: rx.PyDiGraph | rx.PyGraph
    # Rows of the edge projection the graph holds; each edge's payload is its row.
    rows: np.ndarray
    direction: str


class Derived:
    """Arrays computed from one version, the `capacity` most recently used of them.

    A version never changes, so a kept array never goes stale. The keys come
    from requests (a filter, a default weight), so without the bound one client
    could grow the snapshot without limit.
    """

    def __init__(self, kind: str, capacity: int, version: VersionRef) -> None:
        if capacity < 1:
            raise ValueError("a store of derived arrays holds at least one")
        self.kind = kind
        self.capacity = capacity
        self._version = version
        self._entries: OrderedDict[Any, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self.computed = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            return key in self._entries

    def get(self, key: Any, compute: Callable[[], np.ndarray]) -> np.ndarray:
        """The array kept under `key`, computed by `compute` when it is not kept."""
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                return found
        started = time.perf_counter()
        # Computed outside the lock: it reads the store, and other keys need not wait.
        value = compute()
        with self._lock:
            kept = self._entries.setdefault(key, value)
            self._entries.move_to_end(key)
            self.computed += 1
            evicted = 0
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
                evicted += 1
            size = len(self._entries)
        log.debug(
            "derived array computed",
            kind=self.kind,
            version=self._version.id,
            kept=size,
            evicted=evicted,
            capacity=self.capacity,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return kept


class GraphSnapshot:
    def __init__(
        self,
        version: VersionRef,
        uids: pa.Array,
        types: pa.Array,
        edges: pa.Table,
        src: np.ndarray,
        dst: np.ndarray,
    ) -> None:
        self.version = version
        self.uids = uids
        self.types = types
        self.index: dict[str, int] = {uid: i for i, uid in enumerate(uids.to_pylist())}
        self.edges = edges
        self.src = src
        self.dst = dst
        encoded = edges.column("edge_type").combine_chunks().dictionary_encode()
        self.type_codes = encoded.indices.to_numpy(zero_copy_only=False).astype(np.int32)
        self.type_names: list[str] = encoded.dictionary.to_pylist()
        self._views: OrderedDict[Any, GraphView] = OrderedDict()
        self.masks = Derived("mask", MASKS_PER_VERSION, version)
        self.weights = Derived("weight", WEIGHTS_PER_VERSION, version)
        self._lock = threading.Lock()

    @property
    def node_count(self) -> int:
        return len(self.uids)

    @property
    def edge_count(self) -> int:
        return len(self.src)

    @property
    def nbytes(self) -> int:
        """Size of the arrays held, not counting the graphs built from them."""
        return int(self.uids.nbytes + self.types.nbytes + self.edges.nbytes)

    def edge_mask(self, edge_types: Sequence[str] | None) -> np.ndarray:
        if not edge_types:
            return np.ones(self.edge_count, dtype=bool)
        codes = [code for code, name in enumerate(self.type_names) if name in set(edge_types)]
        return np.isin(self.type_codes, np.array(codes, dtype=np.int32))

    def view(
        self,
        edge_types: Sequence[str] | None,
        direction: str,
        mask: np.ndarray | None = None,
        mask_key: Any = None,
    ) -> GraphView:
        """The graph of the chosen edges, followed in `direction`.

        `out` follows each edge from source to target, `in` the other way and
        `both` either way. `mask`, identified by `mask_key`, leaves out more edges.
        """
        check_direction(direction)
        key = (frozenset(edge_types or ()), direction, mask_key)
        with self._lock:
            found = self._views.get(key)
            if found is not None:
                self._views.move_to_end(key)
                return found
            selected = self.edge_mask(edge_types)
            if mask is not None:
                selected = selected & mask
            rows = np.nonzero(selected)[0]
            graph: rx.PyDiGraph | rx.PyGraph
            graph = rx.PyGraph(multigraph=True) if direction == "both" else rx.PyDiGraph()
            graph.add_nodes_from(range(self.node_count))
            ends = (self.dst, self.src) if direction == "in" else (self.src, self.dst)
            graph.add_edges_from(
                list(
                    zip(ends[0][rows].tolist(), ends[1][rows].tolist(), rows.tolist(), strict=True)
                )
            )
            built = GraphView(graph=graph, rows=rows, direction=direction)
            self._views[key] = built
            while len(self._views) > VIEWS_PER_VERSION:
                self._views.popitem(last=False)
            return built

    def edge_table(self, rows: np.ndarray) -> pa.Table:
        if len(rows) == 0:
            return EDGE_SCHEMA.empty_table()
        return self.edges.take(pa.array(rows, pa.int64()))

    def node_table(self, indices: np.ndarray, depths: np.ndarray) -> pa.Table:
        taken = pa.array(indices, pa.int64())
        return pa.table(
            [self.uids.take(taken), self.types.take(taken), pa.array(depths, pa.int32())],
            schema=NODE_SCHEMA,
        )


def build_snapshot(store: Store, ontology: Ontology, at: VersionRef) -> GraphSnapshot:
    """Read the edge projection of a version and the type of every object that can be an end.

    One read of the edges and one read per type that can be an end of one.
    """
    edges = store.edges(at=at).cast(EDGE_SCHEMA)
    uid_parts: list[pa.Array] = []
    type_parts: list[pa.Array] = []
    for type_name in sorted(node_types(edge_kinds(ontology).values())):
        if not ontology.has_type(type_name):
            continue
        found = store.table(type_name, columns=[], at=at, ontology=ontology).column("uid")
        uid_parts.append(found.combine_chunks())
        type_parts.append(pa.array([type_name] * len(found), pa.string()))
    uids = pa.concat_arrays(uid_parts) if uid_parts else pa.array([], pa.string())
    types = pa.concat_arrays(type_parts) if type_parts else pa.array([], pa.string())
    ends = pa.concat_arrays(
        [edges.column("src").combine_chunks(), edges.column("dst").combine_chunks()]
    )
    stray = pc.unique(ends.filter(pc.invert(pc.is_in(ends, value_set=uids))))
    if len(stray):
        # The store keeps references intact, so this means the ontology and the
        # edge projection disagree; the ends are kept, without a type.
        log.warn(
            "edge ends found in no table of a type that can be an end",
            count=len(stray),
            first=stray.slice(0, 3).to_pylist(),
            version=at.id,
        )
        uids = pa.concat_arrays([uids, stray])
        types = pa.concat_arrays([types, pa.nulls(len(stray), pa.string())])
    order = pc.sort_indices(uids)
    uids, types = uids.take(order), types.take(order)
    src = pc.index_in(edges.column("src"), value_set=uids)
    dst = pc.index_in(edges.column("dst"), value_set=uids)
    return GraphSnapshot(
        version=at,
        uids=uids,
        types=types,
        edges=edges.combine_chunks(),
        src=src.to_numpy().astype(np.int64),
        dst=dst.to_numpy().astype(np.int64),
    )


class GraphCache:
    """Graphs of recently used versions, at most `capacity` of them."""

    def __init__(self, store: Store, capacity: int = 4) -> None:
        if capacity < 1:
            raise ValueError("the graph cache holds at least one version")
        self._store = store
        self._capacity = capacity
        self._entries: OrderedDict[str, GraphSnapshot] = OrderedDict()
        self._building: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.builds = 0

    def __len__(self) -> int:
        return len(self._entries)

    def cached(self, version_id: str) -> bool:
        with self._lock:
            return version_id in self._entries

    def get(self, at: VersionRef, ontology: Ontology) -> GraphSnapshot:
        with self._lock:
            found = self._entries.get(at.id)
            if found is not None:
                self._entries.move_to_end(at.id)
                self.hits += 1
                return found
            building = self._building.setdefault(at.id, threading.Lock())
        # Requests for a version being built wait for that build instead of starting another.
        with building:
            with self._lock:
                found = self._entries.get(at.id)
                if found is not None:
                    self._entries.move_to_end(at.id)
                    self.hits += 1
                    return found
            started = time.perf_counter()
            try:
                snapshot = build_snapshot(self._store, ontology, at)
            except Exception as exc:
                log.error("building the graph of a version failed", exc=exc, version=at.id)
                with self._lock:
                    self._building.pop(at.id, None)
                raise
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            with self._lock:
                self._entries[at.id] = snapshot
                self._building.pop(at.id, None)
                self.builds += 1
                evicted = []
                while len(self._entries) > self._capacity:
                    evicted.append(self._entries.popitem(last=False)[1])
            log.info(
                "graph of a version built",
                version=at.id,
                seq=at.seq,
                branch=at.branch,
                nodes=snapshot.node_count,
                edges=snapshot.edge_count,
                bytes=snapshot.nbytes,
                elapsed_ms=elapsed_ms,
                cached_versions=len(self._entries),
            )
            for gone in evicted:
                log.info(
                    "graph of a version evicted from the cache",
                    version=gone.version.id,
                    seq=gone.version.seq,
                    nodes=gone.node_count,
                    edges=gone.edge_count,
                    bytes=gone.nbytes,
                    capacity=self._capacity,
                )
            return snapshot
