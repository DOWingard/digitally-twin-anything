"""Conditions on links that decide which edges a traversal may follow.

A filter holds, for some link types, conditions on the properties of their
links, in the store's filter form. An edge of such a type is followed, and
returned, only when its link passes every condition of its type; edges of
other link types and reference edges are never affected by it. The reads
from the tables add a type's conditions to the read of that type's edges;
the cached graph leaves out the edges whose link does not pass, with a mask
over the version's edge rows.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import Store, VersionRef

from .snapshot import GraphSnapshot

log = Log("graph.filter")

Triple = tuple[str, str, Any]


def _frozen(value: Any) -> Any:
    """A condition's value in a hashable form the store still accepts (`in` takes a tuple)."""
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(value)
    return value


def _text(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no text form")


@dataclass(frozen=True)
class EdgeFilter:
    # (link type, its conditions as store filter triples), sorted by link type.
    conditions: tuple[tuple[str, tuple[Triple, ...]], ...]

    @classmethod
    def of(cls, by_type: Mapping[str, Sequence[Triple]]) -> EdgeFilter:
        return cls(
            tuple(
                (
                    name,
                    tuple(
                        (prop, operator, _frozen(value)) for prop, operator, value in by_type[name]
                    ),
                )
                for name in sorted(by_type)
                if by_type[name]
            )
        )

    def __bool__(self) -> bool:
        return bool(self.conditions)

    @property
    def link_types(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.conditions)

    def on(self, link_type: str) -> list[Triple]:
        """The conditions on links of one type; none for a type the filter does not name."""
        for name, triples in self.conditions:
            if name == link_type:
                return list(triples)
        return []

    @property
    def key(self) -> str:
        """Text that identifies the filter, the same for equal filters."""
        return json.dumps(self.conditions, default=_text, separators=(",", ":"))


def passing_links(
    store: Store, ontology: Ontology, at: VersionRef, edge_filter: EdgeFilter
) -> dict[str, pa.Array]:
    """uids of the links of each filtered type that pass its conditions; one read per type."""
    started = time.perf_counter()
    found: dict[str, pa.Array] = {}
    for name, triples in edge_filter.conditions:
        table = store.table(name, columns=[], filter=list(triples), at=at, ontology=ontology)
        found[name] = table.column("uid").combine_chunks()
    log.debug(
        "links passing an edge filter read",
        version=at.id,
        link_types=len(found),
        passing={name: len(uids) for name, uids in found.items()},
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return found


def keep_passing(edges: pa.Table, passing: Mapping[str, pa.Array]) -> pa.Table:
    """The edges of an edge table that may be followed: of a filtered link type, those whose
    link passes; every other edge."""
    if not passing or edges.num_rows == 0:
        return edges
    edge_type = edges.column("edge_type")
    link_uid = edges.column("link_uid")
    keep = pc.invert(pc.is_in(edge_type, value_set=pa.array(sorted(passing), pa.string())))
    for name, uids in passing.items():
        keep = pc.or_(
            keep,
            pc.and_(pc.equal(edge_type, name), pc.is_in(link_uid, value_set=uids)),
        )
    return edges.filter(pc.fill_null(keep, False))


def follow_mask(
    store: Store,
    ontology: Ontology,
    at: VersionRef,
    snapshot: GraphSnapshot,
    edge_filter: EdgeFilter,
) -> np.ndarray:
    """True for each edge row of the snapshot that the filter lets through.

    Kept with the snapshot, among its most recently used masks.
    """

    def compute() -> np.ndarray:
        mask = np.ones(snapshot.edge_count, dtype=bool)
        link_uids = snapshot.edges.column("link_uid")
        for name, uids in passing_links(store, ontology, at, edge_filter).items():
            if name not in snapshot.type_names:
                continue
            rows = np.nonzero(snapshot.type_codes == snapshot.type_names.index(name))[0]
            passes = pc.is_in(link_uids.take(pa.array(rows, pa.int64())), value_set=uids)
            mask[rows] = pc.fill_null(passes, False).to_numpy(zero_copy_only=False)
        return mask

    return snapshot.masks.get(edge_filter.key, compute)
