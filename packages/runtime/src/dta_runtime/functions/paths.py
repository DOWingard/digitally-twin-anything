"""`paths`: the paths between two objects over the links of the twin.

The search is the graph package's, on the cached graph of the pinned version;
this module only says which links are followed and shapes the answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dta_core.store import InvalidInput

from ..filters import parse_edge_where
from ..graph import (
    BadEdgeFilter,
    BadWeight,
    edge_kinds,
    edge_weights,
    follow_mask,
    select_kinds,
    shortest_paths,
)
from ..protocol import Answer
from .reads import Reads

# The bounds the graph routes of the API put on the same search.
MAX_PATHS = 100
MAX_HOPS = 64
# Hops searched when a call names no `max_depth`. The search counts walks, which grow
# with every hop wherever links form a cycle, and refuses when there are too many: a
# deep default would refuse searches that a call can still ask for by naming a depth.
DEFAULT_HOPS = 6


def _edge_types(reads: Reads, names: Sequence[str] | None) -> list[str]:
    """The edge types followed: every link type unless some are named. A class
    stands for the link types under it."""
    ontology = reads.context.ontology
    if not names:
        return [object_type.name for object_type in ontology.link_types()]
    found: list[str] = []
    for name in names:
        if name in ontology.abstract:
            members = [
                member
                for member in ontology.abstract[name]
                if ontology.has_type(member) and ontology.type(member).link is not None
            ]
            if not members:
                raise reads.bad("edge_types", f"no link type is under {name}", type=name)
            found.extend(members)
        else:
            found.append(name)
    # Refuses a name that is no edge type, with the store's own code.
    return [kind.name for kind in select_kinds(ontology, found)]


def _weight_unit(reads: Reads, weight: str | None) -> str | None:
    """The unit of a path's weight, when every link type that holds it agrees on one."""
    if weight is None:
        return None
    ontology = reads.context.ontology
    units = {
        ontology.type(kind.holder).field(weight).unit
        for kind in edge_kinds(ontology).values()
        if kind.is_link and ontology.type(kind.holder).has_field(weight)
    }
    return units.pop() if len(units) == 1 else None


def _exists(reads: Reads, parameter: str, uid: str) -> None:
    reads.check()
    context = reads.context
    if context.store.get(uid, at=context.version, ontology=context.ontology) is None:
        raise reads.bad(
            parameter,
            f"no object {uid!r} exists at version #{context.version.seq} of "
            f"{context.version.branch}",
            uid=uid,
        )


def paths(reads: Reads, params: Mapping[str, Any]) -> Answer:
    context = reads.context
    ontology, version = context.ontology, context.version
    source, target = params["source"], params["target"]
    weight, default_weight = params.get("weight"), params.get("default_weight")
    k, max_depth = params.get("k", 1), params.get("max_depth", DEFAULT_HOPS)
    if k > MAX_PATHS:
        raise reads.bad("k", f"k is at most {MAX_PATHS}", most=MAX_PATHS)
    if max_depth > MAX_HOPS:
        raise reads.bad("max_depth", f"max_depth is at most {MAX_HOPS}", most=MAX_HOPS)
    if default_weight is not None and weight is None:
        raise reads.bad("default_weight", "default_weight is the weight of a link without `weight`")
    kinds = _edge_types(reads, params.get("edge_types"))
    try:
        edge_filter = parse_edge_where(params.get("edge_where"), ontology)
    except (InvalidInput, BadEdgeFilter) as exc:
        raise reads.bad("edge_where", exc.message) from exc
    if not kinds:
        # No link type to follow. Asking the graph for no edge type would follow them all.
        return Answer(value=[])
    reads.check()
    snapshot = context.graphs.get(version, ontology)
    for parameter, uid in (("source", source), ("target", target)):
        # The graph holds every object that can be an end of an edge; one it does not
        # hold is either of another type, with no path to it, or not there at all.
        if uid not in snapshot.index:
            _exists(reads, parameter, uid)
    weights = None
    if weight is not None:
        reads.check()
        try:
            weights = edge_weights(
                context.store, ontology, version, snapshot, weight, default_weight
            )
        except BadWeight as exc:
            raise reads.bad("weight", exc.message) from exc
    mask: dict[str, Any] = {}
    if edge_filter:
        reads.check()
        mask = {
            "mask": follow_mask(context.store, ontology, version, snapshot, edge_filter),
            "mask_key": edge_filter.key,
        }
    found = shortest_paths(
        snapshot,
        source,
        target,
        k,
        max_depth,
        params.get("direction", "both"),
        kinds,
        weights,
        weight_key=(weight, default_weight),
        **mask,
    )
    value = [
        {
            "hops": path.hops,
            # The sum of the weight property along the path; none was asked for otherwise.
            "weight": path.cost if weight is not None else None,
            "nodes": list(path.nodes),
            "edges": [
                {"link_uid": link_uid, "edge_type": edge_type, "source": src, "target": dst}
                for src, dst, edge_type, link_uid in path.edges
            ],
        }
        for path in found
    ]
    return Answer(value=value, unit=_weight_unit(reads, weight))
