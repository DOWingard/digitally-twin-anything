"""Deterministic synthetic graph that evolves over a sequence of commits.

The generator also keeps the ground truth needed to verify an engine: full
state at a few sampled versions, and the complete change history of a few
tracked assets. A fast answer that disagrees with the ground truth is a failure.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field

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
class ChangeSet:
    upserts: dict[str, list[dict]] = field(default_factory=dict)
    deletes: dict[str, list[str]] = field(default_factory=dict)

    def size(self) -> int:
        return sum(len(rows) for rows in self.upserts.values()) + sum(
            len(uids) for uids in self.deletes.values()
        )


class Fixture:
    def __init__(self, params: FixtureParams) -> None:
        self.params = params
        self._rng = random.Random(params.seed)
        self.state: dict[str, dict[str, dict]] = {"Site": {}, "Asset": {}, "Sensor": {}, "Feeds": {}}
        self.snapshots: dict[int, dict[str, dict[str, dict]]] = {}
        self.tracked: dict[str, list[tuple[int, float]]] = {}
        self._feed_counter = 0
        step = max(1, (params.commits - 1) // max(1, params.samples - 1))
        self.sample_indices = sorted({*range(0, params.commits, step), params.commits - 1})

    # -- generation ---------------------------------------------------------

    def _new_feed(self, asset_ids: list[str]) -> dict:
        src, dst = self._rng.sample(asset_ids, 2)
        self._feed_counter += 1
        return {
            "uid": f"f{self._feed_counter}",
            "src": src,
            "dst": dst,
            "weight": round(self._rng.uniform(0, 1), 4),
        }

    def _initial(self) -> ChangeSet:
        p, rng = self.params, self._rng
        sites = [
            {"uid": f"s{i}", "name": f"Site {i}", "region": rng.choice(REGIONS)}
            for i in range(p.sites)
        ]
        assets = []
        for i in range(p.assets):
            capacity = round(rng.uniform(10, 1000), 3)
            assets.append(
                {
                    "uid": f"a{i}",
                    "name": f"Asset {i}",
                    "capacity": capacity,
                    "load": round(rng.uniform(0, capacity), 3),
                    "status": "ok",
                    "site": f"s{rng.randrange(p.sites)}",
                }
            )
        asset_ids = [a["uid"] for a in assets]
        sensors = [
            {
                "uid": f"n{i}",
                "reading": round(rng.uniform(-50, 150), 3),
                "monitors": rng.choice(asset_ids),
            }
            for i in range(p.sensors)
        ]
        feeds = [self._new_feed(asset_ids) for _ in range(p.feeds)]
        for uid in asset_ids[: p.tracked]:
            self.tracked[uid] = []
        return ChangeSet(upserts={"Site": sites, "Asset": assets, "Sensor": sensors, "Feeds": feeds})

    def _step(self, index: int) -> ChangeSet:
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
            row = dict(self.state["Asset"][uid])
            row["load"] = round(rng.uniform(0, row["capacity"]), 3)
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
        return ChangeSet(
            upserts={"Asset": assets, "Sensor": sensors, "Feeds": added},
            deletes={"Feeds": removed},
        )

    def _apply(self, index: int, changes: ChangeSet) -> None:
        for type_name, rows in changes.upserts.items():
            for row in rows:
                self.state[type_name][row["uid"]] = row
                if type_name == "Asset" and row["uid"] in self.tracked:
                    self.tracked[row["uid"]].append((index, row["load"]))
        for type_name, uids in changes.deletes.items():
            for uid in uids:
                del self.state[type_name][uid]
        if index in self.sample_indices:
            self.snapshots[index] = {
                type_name: {uid: dict(row) for uid, row in rows.items()}
                for type_name, rows in self.state.items()
            }

    def commits(self) -> Iterator[tuple[int, ChangeSet]]:
        """Yield (version index, change set); index 0 is the initial load."""
        for index in range(self.params.commits):
            changes = self._initial() if index == 0 else self._step(index)
            self._apply(index, changes)
            yield index, changes
