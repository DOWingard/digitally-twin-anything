"""The shape every traversal returns, whichever way it was computed."""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.errors import DtaError

from .edges import EDGE_SCHEMA, NODE_SCHEMA


class GraphError(DtaError):
    code = "graph.error"


class SearchTooBroad(GraphError):
    """A path search would have to enumerate more of the graph than is allowed."""

    code = "graph.too_broad"


class BadWeight(GraphError):
    """The property named as a path weight cannot weigh the edges."""

    code = "graph.bad_weight"


@dataclass(frozen=True)
class Neighbourhood:
    # uid, type, depth; nearest first, then by uid.
    nodes: pa.Table
    # src, dst, edge_type, link_uid; only edges whose two ends are among the nodes.
    edges: pa.Table
    # Whether nodes were left out because of the limit.
    truncated: bool


@dataclass(frozen=True)
class Path:
    nodes: tuple[str, ...]
    # One (src, dst, edge_type, link_uid) per hop, in the stored direction of each edge.
    edges: tuple[tuple[str, str, str, str | None], ...]
    cost: float

    @property
    def hops(self) -> int:
        return len(self.edges)


def ordered_edges(edges: pa.Table) -> pa.Table:
    if edges.num_rows == 0:
        return EDGE_SCHEMA.empty_table()
    order = pc.sort_indices(
        edges,
        sort_keys=[
            ("edge_type", "ascending"),
            ("src", "ascending"),
            ("dst", "ascending"),
            ("link_uid", "ascending"),
        ],
        null_placement="at_start",
    )
    return edges.take(order)


def finish(nodes: pa.Table, edges: pa.Table, limit: int) -> Neighbourhood:
    """Order the result, keep at most `limit` nodes and the edges among those kept."""
    nodes = nodes.cast(NODE_SCHEMA)
    edges = edges.cast(EDGE_SCHEMA)
    if nodes.num_rows:
        order = pc.sort_indices(nodes, sort_keys=[("depth", "ascending"), ("uid", "ascending")])
        nodes = nodes.take(order)
    truncated = nodes.num_rows > limit
    if truncated:
        nodes = nodes.slice(0, limit)
    if edges.num_rows:
        kept = nodes.column("uid").combine_chunks()
        inside = pc.and_(
            pc.is_in(edges.column("src"), value_set=kept),
            pc.is_in(edges.column("dst"), value_set=kept),
        )
        edges = edges.filter(inside)
    return Neighbourhood(nodes=nodes, edges=ordered_edges(edges), truncated=truncated)
