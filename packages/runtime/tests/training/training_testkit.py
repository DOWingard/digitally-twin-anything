"""Twins with a known history for the frame tests, and the same cells read the plain way.

The small history (times in steps of 15 minutes after T0; pipes run start -> end):

    a @1    T1 level 1.0, T2 level 2.0; J1 demand 10, J2 demand 20;
            P1: T1 -> J1, P2: J2 -> T1, P3: T2 -> J1
    b @2    T1 level 1.5; J1 demand 11
    c @2    T1 level 1.6                       (same time as b: c is in force at 2)
    d @3.5  T2 level null; T3 appears, level 3.0
    e @5    T2 and P3 removed; P2 removed
    f @6    T1 level 1.8; J2 demand 25; T2 back, level 2.5, joined to nothing
    g @7    J1 demand 12; P4: T3 -> J2
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, VersionRef
from dta_runtime.twin import Twin

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)


def at(steps: float) -> datetime:
    return T0 + steps * STEP


def node(key: str) -> str:
    return f"node:{key}"


def link(key: str) -> str:
    return f"link:{key}"


def tank(key: str, level: float | None) -> dict[str, Any]:
    return {
        "uid": node(key),
        "name": key,
        "elevation": 60.0,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": level,
    }


def junction(key: str, demand: float | None) -> dict[str, Any]:
    return {"uid": node(key), "name": key, "elevation": 40.0, "demand": demand}


def pipe(key: str, start: str, end: str, lined: bool | None = None) -> dict[str, Any]:
    return {
        "uid": link(key),
        "start_node": node(start),
        "end_node": node(end),
        "length": 100.0,
        "diameter": 200.0,
        "lined": lined,
    }


def commit(
    twin: Twin,
    t: datetime,
    upserts: dict[str, list[dict[str, Any]]] | None = None,
    deletes: dict[str, list[str]] | None = None,
    **meta: Any,
) -> VersionRef:
    changes = ChangeSet(
        upserts=upserts or {},
        deletes=deletes or {},
        provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t, **meta))


@dataclass
class History:
    twin: Twin
    versions: dict[str, VersionRef] = field(default_factory=dict)

    def id(self, name: str) -> str:
        return self.versions[name].id


def build_small(twin: Twin) -> History:
    history = History(twin)
    history.versions["a"] = commit(
        twin,
        at(1),
        {
            "Tank": [tank("T1", 1.0), tank("T2", 2.0)],
            "Junction": [junction("J1", 10.0), junction("J2", 20.0)],
            "Pipe": [
                pipe("P1", "T1", "J1", lined=True),
                pipe("P2", "J2", "T1", lined=False),
                pipe("P3", "T2", "J1"),
            ],
        },
    )
    history.versions["b"] = commit(
        twin,
        at(2),
        {
            "Tank": [{"uid": node("T1"), "level": 1.5}],
            "Junction": [{"uid": node("J1"), "demand": 11.0}],
        },
    )
    history.versions["c"] = commit(twin, at(2), {"Tank": [{"uid": node("T1"), "level": 1.6}]})
    history.versions["d"] = commit(
        twin, at(3.5), {"Tank": [{"uid": node("T2"), "level": None}, tank("T3", 3.0)]}
    )
    history.versions["e"] = commit(
        twin, at(5), deletes={"Tank": [node("T2")], "Pipe": [link("P3"), link("P2")]}
    )
    history.versions["f"] = commit(
        twin,
        at(6),
        {
            "Tank": [{"uid": node("T1"), "level": 1.8}, tank("T2", 2.5)],
            "Junction": [{"uid": node("J2"), "demand": 25.0}],
        },
    )
    history.versions["g"] = commit(
        twin,
        at(7),
        {"Junction": [{"uid": node("J1"), "demand": 12.0}], "Pipe": [pipe("P4", "T3", "J2")]},
    )
    return history


# -- a generated history ---------------------------------------------------------------

TANKS = [f"T{index:02d}" for index in range(24)]
JUNCTIONS = [f"J{index:02d}" for index in range(8)]
PIPES = [f"P{index:02d}" for index in range(40)]
GENERATED_VERSIONS = 84


def build_generated(twin: Twin, seed: int = 20261116) -> History:
    """Tanks whose levels change, become null, disappear and come back; junctions whose
    demands change; pipes between them that are removed, added and moved to other ends.
    One version in six has the time of the one before it; one in seven falls between
    grid times."""
    rng = random.Random(seed)
    history = History(twin)
    present = set(TANKS[:20])
    ends: dict[str, tuple[str, str]] = {}

    def new_pipe(key: str) -> dict[str, Any]:
        tank_key = rng.choice(sorted(present))
        other = rng.choice(JUNCTIONS)
        start, end = (tank_key, other) if rng.random() < 0.5 else (other, tank_key)
        ends[key] = (start, end)
        return pipe(key, start, end)

    moment = 1.0
    history.versions["v0"] = commit(
        twin,
        at(moment),
        {
            "Tank": [tank(key, round(rng.uniform(1, 9), 2)) for key in sorted(present)],
            "Junction": [junction(key, round(rng.uniform(5, 50), 1)) for key in JUNCTIONS],
            "Pipe": [new_pipe(key) for key in PIPES[:30]],
        },
    )
    for index in range(1, GENERATED_VERSIONS):
        if index % 6 != 5:
            moment += 0.5 if index % 7 == 3 else 1.0
        tanks, junctions, pipes = [], [], []
        gone_tanks, gone_pipes = [], []
        for key in TANKS:
            draw = rng.random()
            if key in present and draw < 0.03 and len(present) > 12:
                gone_tanks.append(key)
            elif key not in present and draw < 0.25:
                tanks.append(tank(key, round(rng.uniform(1, 9), 2)))
            elif key in present and draw < 0.5:
                level = None if rng.random() < 0.12 else round(rng.uniform(1, 9), 2)
                tanks.append({"uid": node(key), "level": level})
        present -= set(gone_tanks)
        for key in JUNCTIONS:
            if rng.random() < 0.4:
                demand = None if rng.random() < 0.1 else round(rng.uniform(5, 50), 1)
                junctions.append({"uid": node(key), "demand": demand})
        for key in PIPES:
            draw = rng.random()
            if key in ends and (draw < 0.04 or set(ends[key]) & set(gone_tanks)):
                gone_pipes.append(key)
                del ends[key]
            elif key not in ends and draw < 0.15:
                pipes.append(new_pipe(key))
            elif key in ends and draw < 0.1:
                # The same pipe, moved to other ends.
                moved = new_pipe(key)
                pipes.append(
                    {
                        "uid": moved["uid"],
                        "start_node": moved["start_node"],
                        "end_node": moved["end_node"],
                    }  # fmt: skip
                )
        present |= {row["name"] for row in tanks if "name" in row}
        upserts = {
            name: rows
            for name, rows in (("Tank", tanks), ("Junction", junctions), ("Pipe", pipes))
            if rows
        }
        deletes = {
            name: uids
            for name, uids in (
                ("Tank", [node(key) for key in gone_tanks]),
                ("Pipe", [link(key) for key in gone_pipes]),
            )
            if uids
        }
        if not upserts and not deletes:
            upserts = {"Junction": [{"uid": node(JUNCTIONS[0]), "demand": float(index)}]}
        history.versions[f"v{index}"] = commit(twin, at(moment), upserts, deletes)
    return history


class Plain:
    """The cells of a frame worked out one version at a time, by plain reads of the store."""

    def __init__(self, twin: Twin, versions: list[VersionRef]) -> None:
        self.twin = twin
        self.versions = versions
        self._tables: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}

    def version_at(self, moment: datetime) -> VersionRef | None:
        """The last version of the range whose time is at or before the moment."""
        found = [version for version in self.versions if version.t <= moment]
        return max(found, key=lambda version: version.seq) if found else None

    def known(self, moment: datetime) -> bool:
        return self.versions[0].t <= moment <= self.versions[-1].t

    def table(self, version: VersionRef, type_name: str) -> dict[str, dict[str, Any]]:
        key = (version.seq, type_name)
        if key not in self._tables:
            rows = self.twin.store.table(type_name, at=version, ontology=self.twin.ontology)
            self._tables[key] = {row["uid"]: row for row in rows.to_pylist()}
        return self._tables[key]

    def value(self, uid: str, type_name: str, name: str, moment: datetime) -> float | None:
        if not self.known(moment):
            return None
        version = self.version_at(moment)
        assert version is not None
        row = self.table(version, type_name).get(uid)
        return None if row is None or row[name] is None else float(row[name])

    def exists(self, uid: str, type_name: str, moment: datetime) -> bool:
        version = self.version_at(min(moment, self.versions[-1].t))
        return version is not None and uid in self.table(version, type_name)

    def neighbours(self, uid: str, link_type: str, direction: str, moment: datetime) -> set[str]:
        version = self.version_at(moment)
        assert version is not None
        found = set()
        for row in self.table(version, link_type).values():
            if direction in ("out", "both") and row["start_node"] == uid:
                found.add(row["end_node"])
            if direction in ("in", "both") and row["end_node"] == uid:
                found.add(row["start_node"])
        return found

    def aggregate(
        self,
        uid: str,
        link_type: str,
        direction: str,
        type_name: str,
        name: str,
        agg: str,
        moment: datetime,
    ) -> float | None:
        if not self.known(moment):
            return None
        values = [
            value
            for neighbour in self.neighbours(uid, link_type, direction, moment)
            if (value := self.value(neighbour, type_name, name, moment)) is not None
        ]
        if agg == "count":
            return float(len(values))
        if not values:
            return None
        return {
            "mean": lambda: sum(values) / len(values),
            "sum": lambda: sum(values),
            "min": lambda: min(values),
            "max": lambda: max(values),
        }[agg]()
