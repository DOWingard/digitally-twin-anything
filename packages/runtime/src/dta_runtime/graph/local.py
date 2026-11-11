"""Small neighbourhoods read straight from the tables at a version.

An object's links, and its neighbours up to two hops away, are found with
filtered reads of the tables holding the edges: at each hop, one read per
edge type and direction, with the frontier as an `in` filter. The number of
reads therefore grows with the number of edge types and hops, never with the
number of objects, and nothing is loaded that the answer does not need.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import Store, VersionRef

from .edges import EDGE_SCHEMA, NODE_SCHEMA, EdgeKind, check_direction, select_kinds
from .result import Neighbourhood, finish

log = Log("graph.local")

# Longest list of uids sent in one `in` filter; a larger frontier takes several reads.
IN_CHUNK = 5_000

Edge = tuple[str, str, str, str | None]

LINK_SCHEMA = pa.schema(
    [
        pa.field("direction", pa.string()),
        pa.field("edge_type", pa.string()),
        pa.field("src", pa.string()),
        pa.field("dst", pa.string()),
        pa.field("link_uid", pa.string()),
        pa.field("neighbor", pa.string()),
        pa.field("neighbor_type", pa.string()),
    ]
)


def _chunks(items: Sequence[str]) -> Iterator[list[str]]:
    for start in range(0, len(items), IN_CHUNK):
        yield list(items[start : start + IN_CHUNK])


def resolve_types(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    uids: Sequence[str],
    *,
    verify: bool,
) -> dict[str, str]:
    """The type of each uid that names an object at the version; others are left out.

    A uid starts with the scope of its type, which narrows the candidates to
    the types sharing that scope; each candidate is read once with the uids
    as a filter. Without `verify`, a uid whose scope admits one type only is
    taken to exist without a read: it was found through a reference, and the
    store never keeps a reference to an object that does not exist.
    """
    by_scope: dict[str, list[str]] = {}
    for object_type in ontology.types:
        by_scope.setdefault(object_type.scope, []).append(object_type.name)
    pending: dict[str, list[str]] = {}
    for uid in dict.fromkeys(uids):
        pending.setdefault(uid.split(":", 1)[0], []).append(uid)
    found: dict[str, str] = {}
    for scope, members in pending.items():
        candidates = by_scope.get(scope, [])
        if len(candidates) == 1 and not verify:
            found.update(dict.fromkeys(members, candidates[0]))
            continue
        left = members
        for type_name in candidates:
            if not left:
                break
            for chunk in _chunks(left):
                table = store.table(
                    type_name,
                    columns=[],
                    filter=[("uid", "in", chunk)],
                    at=at,
                    ontology=ontology,
                )
                found.update(dict.fromkeys(table.column("uid").to_pylist(), type_name))
            left = [uid for uid in left if uid not in found]
    return found


def _read_kind(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    kind: EdgeKind,
    ends: Sequence[str],
    outgoing: bool,
) -> list[Edge]:
    """The edges of one type whose source (outgoing) or target end is among `ends`."""
    found: list[Edge] = []
    for chunk in _chunks(ends):
        if kind.is_link:
            assert kind.source is not None
            near, far = (kind.source, kind.target) if outgoing else (kind.target, kind.source)
            table = store.table(
                kind.holder,
                columns=[kind.source, kind.target],
                filter=[(near, "in", chunk), (far, "not_null", None)],
                at=at,
                ontology=ontology,
            )
            found.extend(
                zip(
                    table.column(kind.source).to_pylist(),
                    table.column(kind.target).to_pylist(),
                    [kind.name] * table.num_rows,
                    table.column("uid").to_pylist(),
                    strict=True,
                )
            )
            continue
        if outgoing:
            conditions = [("uid", "in", chunk), (kind.target, "not_null", None)]
        else:
            conditions = [(kind.target, "in", chunk)]
        table = store.table(
            kind.holder, columns=[kind.target], filter=conditions, at=at, ontology=ontology
        )
        found.extend(
            zip(
                table.column("uid").to_pylist(),
                table.column(kind.target).to_pylist(),
                [kind.name] * table.num_rows,
                [None] * table.num_rows,
                strict=True,
            )
        )
    return found


def _expand(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    kinds: Sequence[EdgeKind],
    frontier: Mapping[str, str | None],
    direction: str,
) -> list[Edge]:
    """Every edge with an end in the frontier, following `direction` from it."""
    found: list[Edge] = []
    for kind in kinds:
        if direction in ("out", "both"):
            ends = [uid for uid, name in frontier.items() if name in kind.source_types]
            if ends:
                found.extend(_read_kind(store, ontology, at, kind, ends, outgoing=True))
        if direction in ("in", "both"):
            ends = [uid for uid, name in frontier.items() if name in kind.target_types]
            if ends:
                found.extend(_read_kind(store, ontology, at, kind, ends, outgoing=False))
    return list(dict.fromkeys(found))


def neighbourhood(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    seeds: Mapping[str, str],
    depth: int,
    direction: str,
    edge_types: Sequence[str] | None,
    limit: int,
) -> Neighbourhood:
    """Objects within `depth` hops of the seeds (uid -> type), and the edges that reach them.

    The edges returned are those leaving a node nearer than `depth` in the
    direction followed, which is what the cached graph returns as well.
    """
    check_direction(direction)
    started = time.perf_counter()
    kinds = select_kinds(ontology, edge_types)
    types: dict[str, str | None] = dict(seeds)
    depth_of = dict.fromkeys(seeds, 0)
    edges: dict[Edge, None] = {}
    frontier: dict[str, str | None] = dict(seeds)
    for level in range(depth):
        new: list[str] = []
        for edge in _expand(store, ontology, at, kinds, frontier, direction):
            edges[edge] = None
            for end in edge[:2]:
                if end not in depth_of:
                    depth_of[end] = level + 1
                    new.append(end)
        if not new:
            break
        resolved = resolve_types(store, ontology, at, new, verify=False)
        unknown = [uid for uid in new if uid not in resolved]
        if unknown:
            log.warn(
                "edge ends of no known type; they are returned without one",
                count=len(unknown),
                first=unknown[:3],
                version=at.id,
            )
        types.update(resolved)
        frontier = {uid: resolved.get(uid) for uid in new}
    nodes = pa.table(
        [
            pa.array(list(depth_of), pa.string()),
            pa.array([types.get(uid) for uid in depth_of], pa.string()),
            pa.array(list(depth_of.values()), pa.int32()),
        ],
        schema=NODE_SCHEMA,
    )
    edge_table = pa.Table.from_pylist(
        [dict(zip(EDGE_SCHEMA.names, edge, strict=True)) for edge in edges], schema=EDGE_SCHEMA
    )
    result = finish(nodes, edge_table, limit)
    log.debug(
        "neighbourhood read from the tables",
        seeds=len(seeds),
        depth=depth,
        direction=direction,
        edge_types=len(kinds),
        nodes=result.nodes.num_rows,
        edges=result.edges.num_rows,
        truncated=result.truncated,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return result


def links(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    uid: str,
    type_name: str,
    direction: str,
    edge_types: Sequence[str] | None,
) -> pa.Table:
    """The edges of one object, each with the object at its other end."""
    check_direction(direction)
    kinds = select_kinds(ontology, edge_types)
    found = _expand(store, ontology, at, kinds, {uid: type_name}, direction)
    others = [dst if src == uid else src for src, dst, _, _ in found]
    types = resolve_types(store, ontology, at, others, verify=False)
    rows = []
    for (src, dst, edge_type, link_uid), other in zip(found, others, strict=True):
        rows.append(
            {
                "direction": "out" if src == uid else "in",
                "edge_type": edge_type,
                "src": src,
                "dst": dst,
                "link_uid": link_uid,
                "neighbor": other,
                "neighbor_type": types.get(other),
            }
        )
    rows.sort(
        key=lambda row: (row["direction"], row["edge_type"], row["neighbor"], row["link_uid"] or "")
    )
    return pa.Table.from_pylist(rows, schema=LINK_SCHEMA)
