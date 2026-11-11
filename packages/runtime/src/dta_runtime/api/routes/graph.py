"""The twin as a graph: neighbourhoods, paths, subgraphs and the edge projection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

import pyarrow as pa
from dta_core.schema import UID_FIELD, Ontology
from dta_core.store import LIVE_BRANCH, Store, UnknownProperty, UnknownType, VersionRef
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ...graph import (
    LOCAL_DEPTH,
    Neighbourhood,
    cached_neighbourhood,
    edge_weights,
    local_neighbourhood,
    resolve_types,
    select_kinds,
    shortest_paths,
)
from ...graph.local import IN_CHUNK
from ...graph.result import ordered_edges
from ..deps import ServicesDep
from ..docs import API_PREFIX, tabular, versioned
from ..formats import json_response, negotiate, rows, table_response
from ..models import GraphOut, Page, PathsOut, SubgraphRequest
from ..paging import paged_response, start
from ..params import At, Branch, Cursor, EdgeTypes, FormatParam, Limit, page_limit, split_list
from ..problems import ApiError
from ..services import Services
from ..tables import fingerprint, json_properties, mark_json, version_json

router = APIRouter(prefix=f"{API_PREFIX}/graph", tags=["graph"])

TRUNCATED_HEADER = "x-dta-truncated"
MAX_DEPTH = 16
# Paths in real networks are long (a pipe network's supply paths run to tens of
# hops); what bounds a path search is how much of the graph it must look at,
# which the search itself refuses when it is too much.
MAX_PATH_HOPS = 64

Direction = Annotated[
    Literal["out", "in", "both"],
    Query(description="Follow edges from source to target (out), backwards (in), or both ways"),
]
Part = Annotated[
    Literal["nodes", "edges"],
    Query(description="The table rendered when the format is Arrow or Parquet"),
]
NodeLimit = Annotated[int, Query(ge=1, le=100_000, description="Most nodes returned")]


def known_seeds(
    services: Services, ontology: Ontology, version: VersionRef, uids: Sequence[str]
) -> dict[str, str]:
    """uid -> type of each seed; every one must exist at the version."""
    wanted = list(dict.fromkeys(uids))
    found = resolve_types(services.store, ontology, version, wanted, verify=True)
    missing = [uid for uid in wanted if uid not in found]
    if missing:
        raise ApiError(
            f"no object with uid {missing[0]!r} exists at version #{version.seq} of "
            f"{version.branch}",
            code="api.unknown_object",
        )
    return {uid: found[uid] for uid in wanted}


def graph_response(
    result: Neighbourhood,
    nodes: pa.Table,
    chosen: str,
    part: str,
    version: VersionRef,
) -> Response:
    flag = {TRUNCATED_HEADER: "true" if result.truncated else "false"}
    if chosen == "json":
        return json_response(
            {
                "nodes": rows(nodes),
                "edges": rows(result.edges),
                "truncated": result.truncated,
                "version": version_json(version),
            },
            version,
            headers=flag,
        )
    table = nodes if part == "nodes" else result.edges
    metadata = dict(table.schema.metadata or {})
    metadata[b"dta:truncated"] = flag[TRUNCATED_HEADER].encode()
    response = table_response(table.replace_schema_metadata(metadata), chosen, version)  # type: ignore[arg-type]
    response.headers[TRUNCATED_HEADER] = flag[TRUNCATED_HEADER]
    return response


@router.get(
    "/neighbors",
    summary="Objects within some hops of others",
    description="Nodes with their type and depth, and the edges leaving nodes nearer than "
    "`depth` in the direction followed. Up to depth 2 the answer is read from the tables; "
    "deeper, from the version's cached graph. `limit` bounds the nodes, nearest first; "
    "`truncated` says whether some were left out.",
    response_model=GraphOut,
    responses=tabular(400, 404),
)
def neighbors(
    request: Request,
    services: ServicesDep,
    seed: Annotated[list[str], Query(description="uid to start from; repeat for several")],
    depth: Annotated[int, Query(ge=0, le=MAX_DEPTH, description="Hops from the seeds")] = 1,
    direction: Direction = "both",
    edge_types: EdgeTypes = None,
    limit: NodeLimit = 1_000,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    part: Part = "nodes",
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    version = services.resolve(at, branch)
    ontology = services.ontology(version)
    kinds = split_list(edge_types)
    select_kinds(ontology, kinds)
    seeds = known_seeds(services, ontology, version, split_list(seed) or [])
    if depth <= LOCAL_DEPTH:
        result = local_neighbourhood(
            services.store, ontology, version, seeds, depth, direction, kinds, limit
        )
    else:
        snapshot = services.graphs.get(version, ontology)
        result = cached_neighbourhood(snapshot, seeds, depth, direction, kinds, limit)
    return graph_response(result, result.nodes, chosen, part, version)


@router.get(
    "/paths",
    summary="The k shortest paths between two objects",
    description="Simple paths of at most `max_depth` hops, shortest first. A search that "
    "would have to look at too much of the graph is refused with `graph.too_broad`. Without "
    "`weight` "
    "a path costs its number of hops; with it, the sum over its edges of that numeric "
    "property of their link type. An edge without the property costs `default_weight`, or is "
    "not followed when none is given. Paths are distinct sequences of nodes; between two "
    "nodes the cheapest edge is reported, in its stored direction.",
    response_model=PathsOut,
    responses=versioned(400, 404),
)
def paths(
    services: ServicesDep,
    source: Annotated[str, Query(description="uid where paths start", max_length=190)],
    target: Annotated[str, Query(description="uid where paths end", max_length=190)],
    max_depth: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PATH_HOPS,
            description="Most hops of a path. No path within that many hops gives an empty "
            "list, which does not mean the two objects are unconnected",
        ),
    ] = 6,
    k: Annotated[int, Query(ge=1, le=100, description="Most paths returned")] = 3,
    weight: Annotated[
        str | None,
        Query(description="Numeric property of the link types to add up", max_length=200),
    ] = None,
    default_weight: Annotated[
        float | None,
        Query(
            allow_inf_nan=False,
            description="Weight of an edge without the property; such edges are not "
            "followed when absent",
        ),
    ] = None,
    direction: Direction = "both",
    edge_types: EdgeTypes = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
) -> Response:
    version = services.resolve(at, branch)
    ontology = services.ontology(version)
    kinds = split_list(edge_types)
    select_kinds(ontology, kinds)
    known_seeds(services, ontology, version, [source, target])
    snapshot = services.graphs.get(version, ontology)
    weights = None
    if weight is not None:
        weights = edge_weights(services.store, ontology, version, snapshot, weight, default_weight)
    found = shortest_paths(
        snapshot,
        source,
        target,
        k,
        max_depth,
        direction,
        kinds,
        weights,
        weight_key=(weight, default_weight),
    )
    return json_response(
        {
            "source": source,
            "target": target,
            "paths": [
                {
                    "nodes": list(path.nodes),
                    "edges": [
                        {"src": src, "dst": dst, "edge_type": edge_type, "link_uid": link_uid}
                        for src, dst, edge_type, link_uid in path.edges
                    ],
                    "hops": path.hops,
                    "cost": path.cost,
                }
                for path in found
            ],
            "version": version_json(version),
        },
        version,
    )


def _node_types(ontology: Ontology, names: Sequence[str] | None) -> frozenset[str] | None:
    if not names:
        return None
    found: set[str] = set()
    for name in names:
        if name in ontology.abstract:
            found.update(ontology.abstract[name])
        elif ontology.has_type(name):
            found.add(name)
        else:
            raise UnknownType(f"unknown type {name!r}", detail={"type": name})
    return frozenset(found)


def with_properties(
    store: Store, ontology: Ontology, version: VersionRef, nodes: pa.Table, names: Sequence[str]
) -> pa.Table:
    """The nodes with the named properties added; one read per type among the nodes."""
    known = {field.name for object_type in ontology.types for field in object_type.fields}
    for name in names:
        if name not in known or name == UID_FIELD:
            raise UnknownProperty(f"no type has a property {name!r}", detail={"property": name})
    by_type: dict[str, list[str]] = {}
    for uid, type_name in zip(
        nodes.column("uid").to_pylist(), nodes.column("type").to_pylist(), strict=True
    ):
        if type_name is not None:
            by_type.setdefault(type_name, []).append(uid)
    values: dict[str, dict[str, Any]] = {name: {} for name in names}
    arrow_types: dict[str, set[pa.DataType]] = {name: set() for name in names}
    structured: set[str] = set()
    for type_name, uids in by_type.items():
        object_type = ontology.type(type_name)
        present = [name for name in names if object_type.has_field(name)]
        if not present:
            continue
        structured.update(set(json_properties(object_type)) & set(present))
        for begin in range(0, len(uids), IN_CHUNK):
            table = store.table(
                type_name,
                columns=present,
                filter=[("uid", "in", uids[begin : begin + IN_CHUNK])],
                at=version,
                ontology=ontology,
            )
            found = table.column("uid").to_pylist()
            for name in present:
                arrow_types[name].add(table.schema.field(name).type)
                values[name].update(zip(found, table.column(name).to_pylist(), strict=True))
    uids = nodes.column("uid").to_pylist()
    for name in names:
        kinds = arrow_types[name]
        column_type = next(iter(kinds)) if len(kinds) == 1 else pa.string()
        cells = [values[name].get(uid) for uid in uids]
        if len(kinds) > 1:
            cells = [None if cell is None else str(cell) for cell in cells]
        nodes = nodes.append_column(pa.field(name, column_type), pa.array(cells, column_type))
    return mark_json(nodes, structured)


@router.post(
    "/subgraph",
    summary="A bounded part of the graph around some objects",
    description="Breadth-first from the seeds over the version's cached graph. Nodes with "
    "type, depth and the properties asked for, and the edges among them leaving nodes nearer "
    "than `depth`. `node_types` keeps only nodes of those types besides the seeds, the "
    "traversal itself passing through every node. `limit` bounds the nodes, nearest first.",
    response_model=GraphOut,
    responses=tabular(400, 404),
)
def subgraph(
    request: Request,
    services: ServicesDep,
    body: SubgraphRequest,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    part: Part = "nodes",
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    version = services.resolve(at, branch)
    ontology = services.ontology(version)
    kinds = split_list(body.edge_types)
    select_kinds(ontology, kinds)
    node_types = _node_types(ontology, split_list(body.node_types))
    properties = split_list(body.properties)
    seeds = known_seeds(services, ontology, version, body.seeds)
    snapshot = services.graphs.get(version, ontology)
    result = cached_neighbourhood(
        snapshot, seeds, body.depth, body.direction, kinds, body.limit, node_types
    )
    nodes = result.nodes
    if properties:
        nodes = with_properties(services.store, ontology, version, nodes, properties)
    return graph_response(result, nodes, chosen, part, version)


@router.get(
    "/edges",
    summary="The edge projection of the graph",
    description="`src`, `dst`, `edge_type` and `link_uid` (null for a reference) of every "
    "edge, or of the edge types named.",
    response_model=Page,
    responses=tabular(400, 404),
)
def edges(
    request: Request,
    services: ServicesDep,
    edge_types: EdgeTypes = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    kinds = split_list(edge_types)
    query = fingerprint("edges", kinds)
    version, position = start(services, cursor, "edges", query, at, branch)
    table = ordered_edges(services.store.edges(kinds, at=version))
    return paged_response(table, chosen, version, per_page, position, "edges", query)
