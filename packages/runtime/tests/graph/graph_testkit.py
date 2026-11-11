"""Helpers of the graph tests: plain-Python answers to the graph questions, computed
from the store's edge list, a network whose links are open or closed, and a handler
that keeps log records."""

from __future__ import annotations

import logging
import math
import random
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api_testkit import manifest_for
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.twin import Bundle, Twin, install_twin

Edge = tuple[str, str, str, str | None]  # src, dst, edge_type, link_uid

NETWORK_T0 = datetime(2026, 3, 2, tzinfo=UTC)
GENERATED_NODES = 300
# Links beyond the spanning tree, as a share of the nodes: a pipe network is sparse.
CHORDS = 0.5
STATUSES = ("open", "closed", None)
LINK_TYPES = ("Pipe", "Valve", "Pump")


@dataclass
class FilteredNetwork:
    """A water network whose links of every type are open, closed or of no status.

    A hand-made corner, where the short way from A1 to A3 is closed:

        PA1: A1 -> A2 (open)   VA1: A2 -> A3 (closed)
        PA2: A1 -> A4 (open)   PA3: A4 -> A5 (open)   PA4: A5 -> A3 (open)
        PA5: A1 -> A3 (closed, long)

    and a generated part of a few hundred junctions joined by a spanning tree
    and chords of random type and status, some watched by sensors.
    """

    twin: Twin
    version: VersionRef
    # link uid -> (type, row of the link's properties)
    links: dict[str, tuple[str, dict[str, Any]]]
    generated: list[str]


def _junction(key: str) -> dict[str, Any]:
    return {"uid": f"node:{key}", "name": key, "elevation": 10.0}


def _link(kind: str, key: str, start: str, end: str, status: str | None, length: float) -> dict:
    row: dict[str, Any] = {
        "uid": f"link:{key}",
        "start_node": f"node:{start}",
        "end_node": f"node:{end}",
        "status": status,
    }
    if kind == "Pipe":
        row.update(length=length, diameter=100.0)
    elif kind == "Valve":
        row.update(valve_type="prv", setting=length)
    else:
        row.update(speed=1.0)
    return row


def build_filtered_network(settings: StoreSettings, ontology_text: str) -> FilteredNetwork:
    installed = install_twin(
        settings,
        Bundle(manifest=manifest_for(ontology_text), ontology_text=ontology_text),
        t=NETWORK_T0,
    )
    twin = installed.twin
    rng = random.Random(11)
    junctions = [_junction(key) for key in ("A1", "A2", "A3", "A4", "A5")]
    upserts: dict[str, list[dict[str, Any]]] = {"Pipe": [], "Valve": [], "Pump": []}
    upserts["Pipe"] += [
        _link("Pipe", "PA1", "A1", "A2", "open", 10.0),
        _link("Pipe", "PA2", "A1", "A4", "open", 10.0),
        _link("Pipe", "PA3", "A4", "A5", "open", 10.0),
        _link("Pipe", "PA4", "A5", "A3", "open", 10.0),
        _link("Pipe", "PA5", "A1", "A3", "closed", 100.0),
    ]
    upserts["Valve"].append(_link("Valve", "VA1", "A2", "A3", "closed", 1.0))
    generated = [f"G{index:03d}" for index in range(GENERATED_NODES)]
    junctions += [_junction(key) for key in generated]
    pairs = [
        (generated[rng.randrange(index)], generated[index]) for index in range(1, len(generated))
    ]
    for _ in range(int(CHORDS * len(generated))):
        start, end = rng.sample(generated, 2)
        pairs.append((start, end))
    for number, (start, end) in enumerate(pairs):
        kind = rng.choices(LINK_TYPES, weights=(6, 3, 1))[0]
        status = rng.choices(STATUSES, weights=(6, 3, 1))[0]
        key = f"{kind[:2].upper()}{number:04d}"
        upserts[kind].append(_link(kind, key, start, end, status, float(rng.randrange(1, 100))))
    sensors = [
        {
            "uid": f"sensor:G{index:03d}",
            "measures": f"node:{rng.choice(generated)}",
            "quantity": "pressure",
            "active": True,
        }
        for index in range(30)
    ]
    upserts["Junction"] = junctions
    upserts["Sensor"] = sensors
    version = twin.store.commit(
        ChangeSet(upserts=upserts, provenance=Provenance("gis", FactKind.observed, NETWORK_T0)),
        CommitMeta(cause="sync", actor="gis-import", t=NETWORK_T0),
    )
    links = {row["uid"]: (kind, row) for kind in LINK_TYPES for row in upserts[kind]}
    return FilteredNetwork(twin=twin, version=version, links=links, generated=generated)


def network_edges(
    network: FilteredNetwork, keep: Callable[[str, dict[str, Any]], bool] | None = None
) -> list[Edge]:
    """The store's edge list at the network's version, less the links `keep` refuses."""
    table = network.twin.store.edges(None, at=network.version)
    found = []
    for row in table.to_pylist():
        edge = (row["src"], row["dst"], row["edge_type"], row["link_uid"])
        if keep is not None and row["link_uid"] is not None:
            kind, link = network.links[row["link_uid"]]
            if not keep(kind, link):
                continue
        found.append(edge)
    return found


def steps(edges: Iterable[Edge], direction: str) -> dict[str, list[tuple[str, Edge]]]:
    """node -> (neighbour, edge) for every edge followed in `direction`."""
    found: dict[str, list[tuple[str, Edge]]] = {}
    for edge in edges:
        src, dst = edge[0], edge[1]
        if direction in ("out", "both"):
            found.setdefault(src, []).append((dst, edge))
        if direction in ("in", "both"):
            found.setdefault(dst, []).append((src, edge))
    return found


def distances(edges: Sequence[Edge], seeds: Iterable[str], depth: int, direction: str) -> dict:
    adjacency = steps(edges, direction)
    found = dict.fromkeys(seeds, 0)
    queue = deque(found)
    while queue:
        current = queue.popleft()
        if found[current] >= depth:
            continue
        for neighbour, _ in adjacency.get(current, ()):
            if neighbour not in found:
                found[neighbour] = found[current] + 1
                queue.append(neighbour)
    return found


def reached_edges(edges: Sequence[Edge], found: dict[str, int], depth: int, direction: str) -> set:
    """Edges leaving a node nearer than `depth`, in the direction followed, to a found node."""
    kept = set()
    for edge in edges:
        src, dst = edge[0], edge[1]
        if direction in ("out", "both") and found.get(src, depth) < depth and dst in found:
            kept.add(edge)
        if direction in ("in", "both") and found.get(dst, depth) < depth and src in found:
            kept.add(edge)
    return kept


def simple_paths(
    edges: Sequence[Edge],
    source: str,
    target: str,
    max_depth: int,
    direction: str,
    weight: dict[Edge, float] | None = None,
) -> list[tuple[float, tuple[str, ...]]]:
    """(cost, nodes) of every simple path, cheapest edge between consecutive nodes, ranked."""
    adjacency = steps(edges, direction)

    def cost(edge: Edge) -> float:
        if weight is None:
            return 1.0
        return weight.get(edge, math.nan)

    best: dict[tuple[str, str], float] = {}
    for node, pairs in adjacency.items():
        for neighbour, edge in pairs:
            value = cost(edge)
            if math.isnan(value):
                continue
            key = (node, neighbour)
            best[key] = min(best.get(key, math.inf), value)
    found: list[tuple[float, tuple[str, ...]]] = []
    if source == target:
        return [(0.0, (source,))]

    def walk(path: list[str], total: float) -> None:
        if len(path) - 1 == max_depth:
            return
        for neighbour in {n for n, _ in adjacency.get(path[-1], ())}:
            step = best.get((path[-1], neighbour))
            if step is None or neighbour in path:
                continue
            if neighbour == target:
                found.append((total + step, (*path, neighbour)))
            else:
                walk([*path, neighbour], total + step)

    walk([source], 0.0)
    found.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return found


class Records(logging.Handler):
    """Keeps the records logged while it is attached."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def named(self, message: str) -> list[dict]:
        return [
            getattr(record, "data", {}) for record in self.records if record.getMessage() == message
        ]
