"""Graph queries over a twin at a version, independent of any web framework.

Small neighbourhoods (an object's links, neighbours up to two hops) are read
from the store's tables with filtered reads; deeper traversals, paths and
subgraphs run on the version's whole edge projection, loaded once into
rustworkx and cached by version.
"""

from .edges import DIRECTIONS, EDGE_SCHEMA, NODE_SCHEMA, EdgeKind, edge_kinds, select_kinds
from .local import LINK_SCHEMA, links, resolve_types
from .local import neighbourhood as local_neighbourhood
from .queries import MAX_SEARCH_NODES, edge_weights, shortest_paths
from .queries import neighbourhood as cached_neighbourhood
from .result import BadWeight, GraphError, Neighbourhood, Path, SearchTooBroad
from .snapshot import GraphCache, GraphSnapshot, build_snapshot

# Neighbourhoods up to this depth are read from the tables rather than the cached graph.
LOCAL_DEPTH = 2

__all__ = [
    "DIRECTIONS",
    "EDGE_SCHEMA",
    "LINK_SCHEMA",
    "LOCAL_DEPTH",
    "MAX_SEARCH_NODES",
    "NODE_SCHEMA",
    "BadWeight",
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
    "links",
    "local_neighbourhood",
    "resolve_types",
    "select_kinds",
    "shortest_paths",
]
