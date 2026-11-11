"""Traversals over the cached graph of a version: neighbourhoods, subgraphs and paths.

Every graph algorithm used here is rustworkx's: breadth-first layers for
distances and neighbourhoods, induced subgraphs and simple-path enumeration
for paths. rustworkx has no k-shortest-paths routine that returns the paths
themselves, so the k shortest are found by enumerating the simple paths of
bounded length inside the subgraph that two breadth-first searches, one from
each end, prove can hold such a path, and ranking them.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import rustworkx as rx
from dta_core.log import Log
from dta_core.schema import Kind, Ontology
from dta_core.store import Store, UnknownProperty, VersionRef

from .edges import NODE_SCHEMA, check_direction, edge_kinds
from .result import BadWeight, Neighbourhood, Path, SearchTooBroad, finish
from .snapshot import GraphSnapshot, GraphView

log = Log("graph.query")

# Largest subgraph in which simple paths are enumerated.
MAX_SEARCH_NODES = 5_000
# Most walks between the two ends a path search may face; simple paths are
# among the walks, so this bounds what the enumeration can produce.
MAX_SEARCH_WALKS = 200_000
_REVERSE = {"out": "in", "in": "out", "both": "both"}


def _distances(graph: rx.PyDiGraph | rx.PyGraph, starts: Sequence[int], limit: int) -> np.ndarray:
    """Hops from the nearest start to every node, -1 beyond `limit` or unreachable."""
    found = np.full(graph.num_nodes(), -1, dtype=np.int32)
    if not starts:
        return found
    for level, layer in enumerate(rx.bfs_layers(graph, list(starts))):
        if level > limit:
            break
        found[np.asarray(layer, dtype=np.int64)] = level
    return found


def _walks(graph: rx.PyDiGraph | rx.PyGraph, start: int, end: int, hops: int) -> float:
    """Walks of at most `hops` edges from start to end, an upper bound on the simple paths."""
    pairs = np.asarray(graph.edge_list(), dtype=np.int64).reshape(-1, 2)
    tails, heads = pairs[:, 0], pairs[:, 1]
    if isinstance(graph, rx.PyGraph):
        tails, heads = np.concatenate([tails, heads]), np.concatenate([heads, tails])
    counts = np.zeros(graph.num_nodes(), dtype=np.float64)
    counts[start] = 1.0
    total = 0.0
    for _ in range(hops):
        counts = np.bincount(heads, weights=counts[tails], minlength=graph.num_nodes())
        total += float(counts[end])
    return total


def neighbourhood(
    snapshot: GraphSnapshot,
    seeds: Mapping[str, str],
    depth: int,
    direction: str,
    edge_types: Sequence[str] | None,
    limit: int,
    node_types: frozenset[str] | None = None,
    mask: np.ndarray | None = None,
    mask_key: object = None,
) -> Neighbourhood:
    """Objects within `depth` hops of the seeds (uid -> type) and the edges that reach them.

    The edges returned are those leaving a node nearer than `depth` in the
    direction followed. With `node_types`, only nodes of those types are
    returned, besides the seeds; the traversal itself passes through every node.
    `mask`, named by `mask_key`, keeps only the edge rows where it is true (an
    edge filter's `follow_mask`).
    """
    check_direction(direction)
    started = time.perf_counter()
    view_key = None if mask is None else ("filter", mask_key)
    view = snapshot.view(edge_types, direction, mask, view_key)
    starts = sorted({snapshot.index[uid] for uid in seeds if uid in snapshot.index})
    depth_of = _distances(view.graph, starts, depth)
    src, dst = snapshot.src[view.rows], snapshot.dst[view.rows]

    def near(ends: np.ndarray) -> np.ndarray:
        reached = depth_of[ends]
        return (reached >= 0) & (reached < depth)

    if direction == "out":
        keep = near(src)
    elif direction == "in":
        keep = near(dst)
    else:
        keep = near(src) | near(dst)
    indices = np.nonzero(depth_of >= 0)[0]
    nodes = snapshot.node_table(indices, depth_of[indices])
    # A seed that exists but can be the end of no edge is still part of the answer.
    isolated = [uid for uid in seeds if uid not in snapshot.index]
    if isolated:
        nodes = pa.concat_tables(
            [
                nodes,
                pa.table(
                    [
                        pa.array(isolated, pa.string()),
                        pa.array([seeds[uid] for uid in isolated], pa.string()),
                        pa.array([0] * len(isolated), pa.int32()),
                    ],
                    schema=NODE_SCHEMA,
                ),
            ]
        )
    if node_types is not None:
        wanted = pc.or_(
            pc.is_in(nodes.column("type"), value_set=pa.array(sorted(node_types), pa.string())),
            pc.is_in(nodes.column("uid"), value_set=pa.array(list(seeds), pa.string())),
        )
        nodes = nodes.filter(pc.fill_null(wanted, False))
    result = finish(nodes, snapshot.edge_table(view.rows[keep]), limit)
    log.debug(
        "neighbourhood read from the cached graph",
        version=snapshot.version.id,
        seeds=len(seeds),
        depth=depth,
        direction=direction,
        nodes=result.nodes.num_rows,
        edges=result.edges.num_rows,
        truncated=result.truncated,
        filtered=mask is not None,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return result


def edge_weights(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    snapshot: GraphSnapshot,
    name: str,
    default: float | None,
) -> np.ndarray:
    """The weight of every edge of the version: property `name` of its link, else `default`.

    NaN marks an edge without a weight. One read per link type that has the property.
    Kept with the snapshot, among its most recently used weights.
    """

    def compute() -> np.ndarray:
        holders = []
        for kind in edge_kinds(ontology).values():
            object_type = ontology.type(kind.holder)
            if not kind.is_link or not object_type.has_field(name):
                continue
            field = object_type.field(name)
            if field.kind not in (Kind.integer, Kind.float) or field.multivalued:
                raise BadWeight(
                    f"{kind.holder}.{name} is not a number and cannot weigh a path",
                    detail={"type": kind.holder, "property": name},
                )
            holders.append(kind)
        if not holders:
            raise UnknownProperty(
                f"no link type has a property {name!r}", detail={"property": name}
            )
        fill = np.nan if default is None else float(default)
        weights = np.full(snapshot.edge_count, fill, dtype=np.float64)
        link_uids = snapshot.edges.column("link_uid")
        for kind in holders:
            if kind.name not in snapshot.type_names:
                continue
            code = snapshot.type_names.index(kind.name)
            rows = np.nonzero(snapshot.type_codes == code)[0]
            table = store.table(kind.holder, columns=[name], at=at, ontology=ontology)
            position = pc.index_in(
                link_uids.take(pa.array(rows, pa.int64())),
                value_set=table.column("uid").combine_chunks(),
            )
            values = table.column(name).cast(pa.float64()).take(position)
            found = values.to_numpy(zero_copy_only=False).astype(np.float64)
            weights[rows] = np.where(np.isnan(found), fill, found)
        return weights

    return snapshot.weights.get((name, default), compute)


def _cheapest(
    view: GraphView,
    snapshot: GraphSnapshot,
    u: int,
    v: int,
    weights: np.ndarray | None,
) -> int:
    """The row of the edge to report between two consecutive nodes of a path."""
    rows = view.graph.get_all_edge_data(u, v)
    if weights is None:
        return min(
            rows,
            key=lambda row: (
                snapshot.type_names[snapshot.type_codes[row]],
                snapshot.edges.column("link_uid")[row].as_py() or "",
            ),
        )
    return min(rows, key=lambda row: (weights[row], row))


def shortest_paths(
    snapshot: GraphSnapshot,
    source: str,
    target: str,
    k: int,
    max_depth: int,
    direction: str,
    edge_types: Sequence[str] | None,
    weights: np.ndarray | None = None,
    weight_key: object = None,
    mask: np.ndarray | None = None,
    mask_key: object = None,
) -> list[Path]:
    """The `k` cheapest simple paths of at most `max_depth` hops from source to target.

    Without weights a path costs its number of hops; with them, the sum of the
    weights of its edges, and edges without a weight are not followed;
    `weight_key` names the weights, so that graphs built for them are reused.
    `mask`, named by `mask_key`, keeps only the edge rows where it is true (an
    edge filter's `follow_mask`); with weights, an edge is followed when both
    allow it. Paths are distinct sequences of nodes; between two consecutive
    nodes the cheapest edge is reported.
    """
    check_direction(direction)
    started = time.perf_counter()
    if source not in snapshot.index or target not in snapshot.index:
        return []
    if source == target:
        return [Path(nodes=(source,), edges=(), cost=0.0)]
    followed, view_key = mask, None if mask is None else ("filter", mask_key)
    if weights is not None:
        weighted = ~np.isnan(weights)
        followed = weighted if mask is None else weighted & mask
        view_key = ("weighted", weight_key, mask_key)
    forward = snapshot.view(edge_types, direction, followed, view_key)
    backward = snapshot.view(edge_types, _REVERSE[direction], followed, view_key)
    s, t = snapshot.index[source], snapshot.index[target]
    from_source = _distances(forward.graph, [s], max_depth)
    to_target = _distances(backward.graph, [t], max_depth)
    if to_target[s] < 0:
        return []
    # Unweighted, paths are ranked by length, so the search stops at the first
    # length that yields k of them; weighted, a longer path may be cheaper.
    lengths = range(int(to_target[s]), max_depth + 1) if weights is None else [max_depth]
    found: set[tuple[int, ...]] = set()
    for hops in lengths:
        allowed = np.nonzero(
            (from_source >= 0) & (to_target >= 0) & (from_source + to_target <= hops)
        )[0]
        if len(allowed) > MAX_SEARCH_NODES:
            log.warn(
                "path search refused: too much of the graph lies within reach",
                version=snapshot.version.id,
                nodes=len(allowed),
                max_nodes=MAX_SEARCH_NODES,
                hops=hops,
            )
            raise SearchTooBroad(
                f"{len(allowed)} nodes lie on paths of up to {hops} hops between the two "
                f"objects, more than {MAX_SEARCH_NODES}; lower max_depth or name edge_types",
                detail={"nodes": int(len(allowed)), "max_nodes": MAX_SEARCH_NODES},
            )
        sub = forward.graph.subgraph(allowed.tolist())
        original = sub.nodes()
        position = {node: index for index, node in enumerate(original)}
        walks = _walks(sub, position[s], position[t], hops)
        if walks > MAX_SEARCH_WALKS:
            log.warn(
                "path search refused: too many walks between the two objects",
                version=snapshot.version.id,
                walks=walks,
                max_walks=MAX_SEARCH_WALKS,
                hops=hops,
            )
            raise SearchTooBroad(
                f"about {walks:.0f} walks of up to {hops} hops join the two objects, more "
                f"than {MAX_SEARCH_WALKS}; lower max_depth or name edge_types",
                detail={"walks": walks, "max_walks": MAX_SEARCH_WALKS},
            )
        for path in rx.all_simple_paths(sub, position[s], position[t], cutoff=hops + 1):
            found.add(tuple(original[index] for index in path))
        if weights is None and len(found) >= k:
            break
    paths = []
    for node_path in found:
        rows = [
            _cheapest(forward, snapshot, u, v, weights)
            for u, v in zip(node_path, node_path[1:], strict=False)
        ]
        edges = snapshot.edge_table(np.asarray(rows, dtype=np.int64)).to_pylist()
        cost = float(len(rows)) if weights is None else float(sum(weights[row] for row in rows))
        paths.append(
            Path(
                nodes=tuple(snapshot.uids[index].as_py() for index in node_path),
                edges=tuple(
                    (edge["src"], edge["dst"], edge["edge_type"], edge["link_uid"])
                    for edge in edges
                ),
                cost=cost,
            )
        )
    paths.sort(key=lambda path: (path.cost, path.hops, path.nodes))
    log.debug(
        "paths found",
        version=snapshot.version.id,
        candidates=len(paths),
        returned=min(k, len(paths)),
        weighted=weights is not None,
        filtered=mask is not None,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return paths[:k]
