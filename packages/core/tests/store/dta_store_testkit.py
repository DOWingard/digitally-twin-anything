"""Shared pieces of the store tests: hand-built ontologies, a deterministic
evolving graph with its ground truth, and small helpers for raw SQL."""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pymysql
from dta_core.schema import FactKind, Field, Kind, ObjectType, Ontology, Role
from dta_core.store import Cause, ChangeSet, CommitMeta, Provenance, StoreSettings

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def prov(
    kind: FactKind | str = FactKind.observed,
    source: str = "scada",
    t_valid: datetime | None = None,
    confidence: float = 1.0,
) -> Provenance:
    return Provenance(source, FactKind(kind), t_valid or T0, confidence)


def meta(cause: Cause | str = Cause.sync, t: datetime | None = None, **more: Any) -> CommitMeta:
    return CommitMeta(cause=Cause(cause), actor=more.pop("actor", "test"), t=t or T0, **more)


def changes(
    upserts: dict[str, list[dict[str, Any]]] | None = None,
    deletes: dict[str, list[str]] | None = None,
    kind: FactKind | str = FactKind.observed,
    row_provenance: dict[str, Provenance] | None = None,
) -> ChangeSet:
    return ChangeSet(
        upserts=upserts or {},
        deletes=deletes or {},
        provenance=prov(kind),
        row_provenance=row_provenance or {},
    )


def raw_connection(settings: StoreSettings, branch: str | None = "main") -> Any:
    """A connection that bypasses the store, for doing what the store never does."""
    return pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=f"{settings.database}/{branch}" if branch else None,
        autocommit=True,
    )


def sql(connection: Any, statement: str, parameters: Any = None) -> tuple[tuple[Any, ...], ...]:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.fetchall()


# -- ontologies ----------------------------------------------------------------


def water_ontology() -> Ontology:
    """A small network with inheritance, a link type, structured values and a meta type."""
    junction = ObjectType(
        "Junction",
        (
            Field("name", Kind.string, required=True),
            Field("elevation", Kind.float, required=True, unit="m", minimum=0, maximum=9000),
            Field("zone", Kind.ref, target="Zone"),
        ),
        ancestors=("Node",),
    )
    tank = ObjectType(
        "Tank",
        (
            Field("name", Kind.string, required=True),
            Field("level", Kind.float, role=Role.state, unit="m"),
            Field("capacity", Kind.float, required=True, minimum=0),
            Field("status", Kind.enum, target="TankStatus"),
            Field("curve", Kind.float, multivalued=True),
            Field("tags", Kind.string, multivalued=True),
            Field("inspections", Kind.datetime, multivalued=True),
            Field("geometry", Kind.json),
            Field("commissioned", Kind.date),
            Field("sampled_at", Kind.datetime, role=Role.state),
            Field("sealed", Kind.boolean),
            Field("fill_count", Kind.integer, minimum=0),
            Field("notes", Kind.string, long_text=True),
            Field("code", Kind.string, pattern="^[A-Z]{2}-[0-9]+$"),
        ),
        ancestors=("Node",),
        checks=(("level_within_capacity", "level is null or level <= capacity"),),
    )
    zone = ObjectType("Zone", (Field("name", Kind.string, required=True),))
    pipe = ObjectType(
        "Pipe",
        (
            Field("start_node", Kind.ref, required=True, target="Node"),
            Field("end_node", Kind.ref, required=True, target="Node"),
            Field("diameter", Kind.float, minimum=0),
            Field("feeds_tank", Kind.ref, target="Tank"),
        ),
        ancestors=("Link",),
        link=("start_node", "end_node"),
    )
    run = ObjectType(
        "TwinSyncRun",
        (
            Field("status", Kind.enum, required=True, target="TwinRunStatus"),
            Field("sources", Kind.string, multivalued=True),
        ),
        meta=True,
        event=True,
    )
    return Ontology(
        types=(run, junction, pipe, tank, zone),
        abstract={"Node": ("Junction", "Tank"), "Link": ("Pipe",)},
        enums={
            "TankStatus": ("ok", "low", "out of service"),
            "TwinRunStatus": ("running", "succeeded", "failed"),
        },
        source="classes: {}\n",
    )


def graph_ontology() -> Ontology:
    """The typed graph of the evolving fixture: sites, assets, sensors and asset-to-asset links."""
    site = ObjectType(
        "Site",
        (Field("name", Kind.string, required=True), Field("region", Kind.string, required=True)),
    )
    asset = ObjectType(
        "Asset",
        (
            Field("name", Kind.string, required=True),
            Field("capacity", Kind.float, required=True, unit="kW", minimum=0),
            Field("load", Kind.float, required=True, role=Role.state, unit="kW", minimum=0),
            Field("status", Kind.enum, required=True, role=Role.state, target="AssetStatus"),
            Field("site", Kind.ref, required=True, target="Site"),
        ),
    )
    sensor = ObjectType(
        "Sensor",
        (
            Field("reading", Kind.float, required=True, role=Role.state),
            Field("monitors", Kind.ref, required=True, target="Asset"),
        ),
    )
    feeds = ObjectType(
        "Feeds",
        (
            Field("src", Kind.ref, required=True, target="Asset"),
            Field("dst", Kind.ref, required=True, target="Asset"),
            Field("weight", Kind.float, required=True, minimum=0),
        ),
        link=("src", "dst"),
    )
    return Ontology(
        types=(asset, feeds, sensor, site),
        enums={"AssetStatus": ("ok", "degraded", "down")},
        source="classes: {}\n",
    )


# -- evolving graph ------------------------------------------------------------

STATUSES = ("ok", "degraded", "down")
REGIONS = ("north", "south", "east", "west")


@dataclass(frozen=True)
class FixtureParams:
    name: str
    sites: int
    assets: int
    sensors: int
    feeds: int
    commits: int  # includes the initial load
    changed_per_commit: int  # assets and sensors updated in each later commit
    edge_churn: int  # links removed and added in each later commit
    seed: int = 7
    tracked: int = 5
    samples: int = 6


FIXTURES = {
    "smoke": FixtureParams("smoke", 3, 60, 60, 150, 12, 5, 2),
    "wide": FixtureParams("wide", 50, 20_000, 20_000, 60_000, 200, 200, 20),
    "long": FixtureParams("long", 10, 1_000, 1_000, 3_000, 2_000, 10, 2),
}


@dataclass
class Step:
    """One commit of the fixture. Asset updates are partial rows, the rest whole rows."""

    upserts: dict[str, list[dict]] = field(default_factory=dict)
    deletes: dict[str, list[str]] = field(default_factory=dict)

    def size(self) -> int:
        return sum(len(rows) for rows in self.upserts.values()) + sum(
            len(uids) for uids in self.deletes.values()
        )


class Fixture:
    """Deterministic graph that changes commit by commit, with the truth needed
    to check a store: the full state at a few sampled versions, and the complete
    history of `load` for a few tracked assets."""

    def __init__(self, params: FixtureParams) -> None:
        self.params = params
        self._rng = random.Random(params.seed)
        self.state: dict[str, dict[str, dict]] = {
            "Site": {},
            "Asset": {},
            "Sensor": {},
            "Feeds": {},
        }
        self.snapshots: dict[int, dict[str, dict[str, dict]]] = {}
        self.tracked: dict[str, list[tuple[int, float]]] = {}
        self._feed_counter = 0
        step = max(1, (params.commits - 1) // max(1, params.samples - 1))
        self.sample_indices = sorted({*range(0, params.commits, step), params.commits - 1})

    def _new_feed(self, asset_ids: list[str]) -> dict:
        src, dst = self._rng.sample(asset_ids, 2)
        self._feed_counter += 1
        return {
            "uid": f"feeds:f{self._feed_counter}",
            "src": src,
            "dst": dst,
            "weight": round(self._rng.uniform(0, 1), 4),
        }

    def _initial(self) -> Step:
        p, rng = self.params, self._rng
        sites = [
            {"uid": f"site:s{i}", "name": f"Site {i}", "region": rng.choice(REGIONS)}
            for i in range(p.sites)
        ]
        assets = []
        for i in range(p.assets):
            capacity = round(rng.uniform(10, 1000), 3)
            assets.append(
                {
                    "uid": f"asset:a{i}",
                    "name": f"Asset {i}",
                    "capacity": capacity,
                    "load": round(rng.uniform(0, capacity), 3),
                    "status": "ok",
                    "site": f"site:s{rng.randrange(p.sites)}",
                }
            )
        asset_ids = [a["uid"] for a in assets]
        sensors = [
            {
                "uid": f"sensor:n{i}",
                "reading": round(rng.uniform(-50, 150), 3),
                "monitors": rng.choice(asset_ids),
            }
            for i in range(p.sensors)
        ]
        feeds = [self._new_feed(asset_ids) for _ in range(p.feeds)]
        for uid in asset_ids[: p.tracked]:
            self.tracked[uid] = []
        return Step(upserts={"Site": sites, "Asset": assets, "Sensor": sensors, "Feeds": feeds})

    def _step(self, index: int) -> Step:
        p, rng = self.params, self._rng
        asset_ids = list(self.state["Asset"])
        chosen = set(rng.sample(asset_ids, min(p.changed_per_commit, len(asset_ids))))
        # Tracked assets change on a fixed cadence so each has a long, known history.
        for position, uid in enumerate(self.tracked):
            if index % 3 == position % 3:
                chosen.add(uid)
            else:
                chosen.discard(uid)
        assets = []
        for uid in sorted(chosen):
            row = {
                "uid": uid,
                "load": round(rng.uniform(0, self.state["Asset"][uid]["capacity"]), 3),
            }
            if rng.random() < 0.1:
                row["status"] = rng.choice(STATUSES)
            assets.append(row)
        sensor_ids = list(self.state["Sensor"])
        sensors = []
        for uid in rng.sample(sensor_ids, min(p.changed_per_commit, len(sensor_ids))):
            row = dict(self.state["Sensor"][uid])
            row["reading"] = round(rng.uniform(-50, 150), 3)
            sensors.append(row)
        removed = rng.sample(list(self.state["Feeds"]), min(p.edge_churn, len(self.state["Feeds"])))
        added = [self._new_feed(asset_ids) for _ in range(p.edge_churn)]
        return Step(
            upserts={"Asset": assets, "Sensor": sensors, "Feeds": added},
            deletes={"Feeds": removed},
        )

    def _apply(self, index: int, step: Step) -> None:
        for type_name, rows in step.upserts.items():
            for row in rows:
                merged = {**self.state[type_name].get(row["uid"], {}), **row}
                self.state[type_name][row["uid"]] = merged
                if type_name == "Asset" and row["uid"] in self.tracked:
                    self.tracked[row["uid"]].append((index, merged["load"]))
        for type_name, uids in step.deletes.items():
            for uid in uids:
                del self.state[type_name][uid]
        if index in self.sample_indices:
            self.snapshots[index] = {
                type_name: {uid: dict(row) for uid, row in rows.items()}
                for type_name, rows in self.state.items()
            }

    def commits(self) -> Iterator[tuple[int, Step]]:
        """Yield (index, step); index 0 is the initial load."""
        for index in range(self.params.commits):
            step = self._initial() if index == 0 else self._step(index)
            self._apply(index, step)
            yield index, step
