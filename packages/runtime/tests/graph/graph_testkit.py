"""Helpers of the graph tests: plain-Python answers to the graph questions, computed
from the store's edge list, and a handler that keeps log records."""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable, Sequence

Edge = tuple[str, str, str, str | None]  # src, dst, edge_type, link_uid


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
