"""Which objects and properties of a twin form the edges of its graph.

The store names the edges of a version the same way: a link type gives one
edge per link, from its source end to its target end, named after the type;
every other reference property gives an edge from the object holding it to
the object it refers to, named `<Type>.<property>`. Both traversal strategies
of this package derive their edges from the same description, so that a
neighbourhood read from the tables and one read from the cached graph agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pyarrow as pa
from dta_core.schema import Kind, Ontology
from dta_core.store import UnknownType

DIRECTIONS = ("out", "in", "both")

EDGE_SCHEMA = pa.schema(
    [
        pa.field("src", pa.string()),
        pa.field("dst", pa.string()),
        pa.field("edge_type", pa.string()),
        pa.field("link_uid", pa.string()),
    ]
)
NODE_SCHEMA = pa.schema(
    [
        pa.field("uid", pa.string()),
        pa.field("type", pa.string()),
        pa.field("depth", pa.int32()),
    ]
)


@dataclass(frozen=True)
class EdgeKind:
    name: str
    # The type whose rows hold the edge.
    holder: str
    # Property holding the source uid; None when the source is the row itself.
    source: str | None
    target: str
    # Concrete types the two ends may have.
    source_types: frozenset[str]
    target_types: frozenset[str]

    @property
    def is_link(self) -> bool:
        return self.source is not None


def _concrete(ontology: Ontology, name: str | None) -> frozenset[str]:
    if not name:
        return frozenset()
    try:
        return frozenset(ontology.concrete(name))
    except KeyError:
        return frozenset()


def edge_kinds(ontology: Ontology) -> dict[str, EdgeKind]:
    """Every edge type of the ontology, by name."""
    kinds: dict[str, EdgeKind] = {}
    for object_type in ontology.types:
        ends = object_type.link or ()
        if ends:
            source, target = ends
            kinds[object_type.name] = EdgeKind(
                name=object_type.name,
                holder=object_type.name,
                source=source,
                target=target,
                source_types=_concrete(ontology, object_type.field(source).target),
                target_types=_concrete(ontology, object_type.field(target).target),
            )
        for field in object_type.fields:
            if field.kind != Kind.ref or field.name in ends:
                continue
            name = f"{object_type.name}.{field.name}"
            kinds[name] = EdgeKind(
                name=name,
                holder=object_type.name,
                source=None,
                target=field.name,
                source_types=frozenset({object_type.name}),
                target_types=_concrete(ontology, field.target),
            )
    return kinds


def select_kinds(ontology: Ontology, names: Sequence[str] | None) -> list[EdgeKind]:
    """The edge types asked for, all of them when `names` is None or empty."""
    kinds = edge_kinds(ontology)
    if not names:
        return list(kinds.values())
    unknown = sorted({name for name in names if name not in kinds})
    if unknown:
        raise UnknownType(f"unknown edge type {unknown[0]!r}", detail={"edge_types": unknown})
    return [kinds[name] for name in dict.fromkeys(names)]


def node_types(kinds: Iterable[EdgeKind]) -> frozenset[str]:
    """The types whose objects can be an end of one of these edges."""
    found: set[str] = set()
    for kind in kinds:
        found |= kind.source_types | kind.target_types
    return frozenset(found)


def check_direction(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    return direction
