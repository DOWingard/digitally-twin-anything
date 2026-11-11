"""Graph queries over a twin at a version, independent of any web framework.

Small neighbourhoods (an object's links, neighbours up to two hops) are read
from the store's tables with filtered reads; deeper traversals, paths and
subgraphs run on the version's whole edge projection, loaded once into
rustworkx and cached by version.
"""

from .edges import DIRECTIONS, EDGE_SCHEMA, NODE_SCHEMA, EdgeKind, edge_kinds, select_kinds
from .filtering import EdgeFilter, follow_mask, keep_passing, passing_links
from .local import LINK_SCHEMA, links, resolve_types
from .local import neighbourhood as local_neighbourhood
from .queries import MAX_SEARCH_NODES, MAX_SEARCH_WALKS, edge_weights, shortest_paths
from .queries import neighbourhood as cached_neighbourhood
from .result import BadEdgeFilter, BadWeight, GraphError, Neighbourhood, Path, SearchTooBroad
from .snapshot import (
    MASKS_PER_VERSION,
    WEIGHTS_PER_VERSION,
    GraphCache,
    GraphSnapshot,
    build_snapshot,
)

# Neighbourhoods up to this depth are read from the tables rather than the cached graph.
LOCAL_DEPTH = 2

__all__ = [
    "DIRECTIONS",
    "EDGE_SCHEMA",
    "LINK_SCHEMA",
    "LOCAL_DEPTH",
    "MASKS_PER_VERSION",
    "MAX_SEARCH_NODES",
    "MAX_SEARCH_WALKS",
    "NODE_SCHEMA",
    "WEIGHTS_PER_VERSION",
    "BadEdgeFilter",
    "BadWeight",
    "EdgeFilter",
    "EdgeKind",
    "GraphCache",
    "GraphError",
    "GraphSnapshot",
    "Neighbourhood",
    "Path",
    "SearchTooBroad",
    "build_snapshot",
    "cached_neighbourhood",
    "edge_kinds",
    "edge_weights",
    "follow_mask",
    "keep_passing",
    "links",
    "local_neighbourhood",
    "passing_links",
    "resolve_types",
    "select_kinds",
    "shortest_paths",
]
