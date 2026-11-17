"""A water network with a known history for the scenario tests, the models registered
on it, and a predictor that predicts in process over the real store.

The network, on the water ontology (valves and the pipe run start -> end):

    V1: J1 -> T1 (open, setting 1.0)    V2: J1 -> T2 (open, setting 2.0)    P1: J1 -> J2

Version i is stored at step i (15 minutes each) after T0, for i = 1..LAST:

    T1 level   4.0 + CYCLE[i % 4]        T2 level   9.0 + 2 * CYCLE[i % 4]
    J1 demand  10.0 + i                  J2 demand  20.0, never changed

One more version, at the time of the last, registers the models. It is the base of the
tests' scenarios: its time is step LAST, so grid time j of a scenario is step LAST + j.

The models (`twin_model:<key>`):

    level-naive      seasonal_naive with a season of 4 on Tank.level: repeats the cycle
    demand-last      persistence on Junction.demand: the last demand, carried forward
    level-balance    the twin's own, interventional, on Tank.level: a tank gains INFLOW
                     a step while the valve that feeds it is open and loses OUTFLOW
    level-again      persistence on Tank.level (the target of level-naive, again)
    level-candidate  seasonal_naive on Tank.level, not active
    level-unfitted   active, without a fitted state
    lost-target      active, predicts a property the ontology does not have
    tank-name        active, predicts a property that is not a number
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import (
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    Provenance,
    StoreSettings,
    VersionRef,
)
from dta_runtime.protocol import FitRequest, FrameRef, PredictRequest
from dta_runtime.protocol.worker import InterventionRef
from dta_runtime.scenarios import ScenarioRequest
from dta_runtime.twin import Bundle, Twin, install_twin

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
LAST = 24
CYCLE = (0.0, 1.0, 0.5, -0.5)
INFLOW, OUTFLOW = 0.5, 0.2
TRACE = "0af7651916cd43dd8448eb211c80319c"
TIME = pa.timestamp("us", tz="UTC")
STATE_MEDIA_TYPE = "application/octet-stream"

Row = dict[str, Any]

# A tank's level follows what flows in less what flows out; closing the valve that
# feeds it stops the inflow. The mechanism is modelled, so it holds under a change of
# the valve that the history never showed.
PLANT_MODULE = "scenario_plant"
PLANT_CODE = """
import pandas as pd


class Balance:
    interventional = True

    def __init__(self, step):
        self.step = step

    def fit(self, frame, params, *, horizon):
        return {
            "inflow": float(params["inflow"]),
            "outflow": float(params["outflow"]),
            "feeds": dict(params["feeds"]),
        }

    def predict(self, state, history, horizon, *, levels=(), drivers=None, interventions=()):
        rows = []
        for uid, series in history.groupby("unique_id", sort=True):
            level, moment = float(series["y"].iloc[-1]), series["ds"].iloc[-1]
            valves = [valve for valve, tank in state["feeds"].items() if tank == uid]
            for _ in range(horizon):
                moment = moment + self.step
                closed = any(
                    found.target == "Valve.status"
                    and found.uid in valves
                    and found.value == "closed"
                    and found.start <= moment
                    and (found.end is None or moment < found.end)
                    for found in interventions
                )
                level += (0.0 if closed else state["inflow"]) - state["outflow"]
                rows.append({"unique_id": uid, "ds": moment, "mean": level})
        return pd.DataFrame(rows)

    def validity(self, frame):
        return {"y": (float(frame["y"].min()), float(frame["y"].max()))}


def balance(*, step, twin):
    return Balance(step)
"""
BALANCE = f"{PLANT_MODULE}:balance"
FEEDS = {"link:V1": "node:T1", "link:V2": "node:T2"}


def at(steps: float) -> datetime:
    """The time `steps` steps after T0."""
    return T0 + steps * STEP


def grid(j: float) -> datetime:
    """Grid time j of a scenario that starts at the base."""
    return at(LAST + j)


def node(key: str) -> str:
    return f"node:{key}"


def link(key: str) -> str:
    return f"link:{key}"


def model(key: str) -> str:
    return f"twin_model:{key}"


def level(key: str, step: int) -> float:
    """The level of a tank at step `step`: what was stored, and what repeating the cycle
    gives after the last version."""
    return {"T1": 4.0 + CYCLE[step % 4], "T2": 9.0 + 2 * CYCLE[step % 4]}[key]


def demand(key: str, step: int) -> float:
    return {"J1": 10.0 + min(step, LAST), "J2": 20.0}[key]


def manifest_for(ontology_text: str) -> Manifest:
    """A manifest of the water network in which the status of a valve can be set."""
    document = {
        "twin": {"id": "water", "name": "Water network", "revision": 1},
        "contract": {
            "tier": "static",
            "boundary": {
                "state": ["Tank.level"],
                "controls": ["Valve.status"],
                "observations": ["Tank.level", "Junction.demand"],
            },
        },
        "ontology": {
            "path": "ontology/water_network.yaml",
            "type_signature": type_signature(parse_ontology(ontology_text).ontology),
        },
    }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


def tank(key: str, value: float | None) -> Row:
    return {
        "uid": node(key),
        "name": key,
        "elevation": 60.0,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": value,
    }


def junction(key: str, value: float | None) -> Row:
    return {"uid": node(key), "name": key, "elevation": 40.0, "base_demand": 5.0, "demand": value}


def valve(key: str, start: str, end: str, setting: float) -> Row:
    return {
        "uid": link(key),
        "start_node": node(start),
        "end_node": node(end),
        "valve_type": "prv",
        "status": "open",
        "setting": setting,
    }


def commit(twin: Twin, t: datetime, upserts: Mapping[str, list[Row]]) -> VersionRef:
    """One version of observed facts on the live line, as a sync writes them."""
    changes = ChangeSet(
        upserts=dict(upserts),
        provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))


class Kept:
    """Artifacts in a dict, by the SHA-256 of their content."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def get(self, sha256: str) -> bytes:
        return self.files[sha256]

    def put(self, content: bytes, media_type: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self.files[digest] = content
        return digest


class Stored:
    """A reader of the twin over its store, as the worker's is over the API."""

    branch = LIVE_BRANCH

    def __init__(self, twin: Twin) -> None:
        self._twin = twin
        self.at = "head"

    def get(self, path: str, **params: Any) -> Any:
        raise AssertionError(f"a handler read {path}")

    def table(self, path: str, **params: Any) -> pa.Table:
        raise AssertionError(f"a handler read {path}")

    def frame(self, ref: FrameRef) -> pa.Table:
        from dta_runtime.training import frame

        return frame(self._twin.store, self._twin.ontology, ref)


def artifact_row(sha256: str, size: int, media_type: str = STATE_MEDIA_TYPE) -> Row:
    return {
        "uid": f"twin_artifact:{sha256}",
        "sha256": sha256,
        "media_type": media_type,
        "size_bytes": size,
        "location": f"blobs/{sha256[:2]}/{sha256}",
    }


def model_row(
    key: str,
    family: str,
    target: str,
    *,
    params: Mapping[str, Any] | None = None,
    fitted: str | None = None,
    code: str | None = None,
    interventional: bool = False,
    status: str = "active",
) -> Row:
    return {
        "uid": model(key),
        "spec": key,
        "candidate": family,
        "family": family,
        "code": None if code is None else f"twin_artifact:{code}",
        "params": json.dumps(dict(params or {}), sort_keys=True),
        "target": target,
        "interventional": interventional,
        "origin": "runtime",
        "status": status,
        "fitted": None if fitted is None else f"twin_artifact:{fitted}",
    }


def register(twin: Twin, rows: Mapping[str, list[Row]]) -> VersionRef:
    """Records of the twin about itself, written at the time of the live head."""
    head = twin.store.head()
    changes = ChangeSet(
        upserts=dict(rows),
        provenance=Provenance(source="test", kind=FactKind.inferred, t_valid=head.t),
    )
    return twin.store.commit(changes, CommitMeta(cause="registration", actor="test", t=head.t))


def family_ref(record: Mapping[str, Any]) -> dict[str, Any]:
    """A shipped family by its name; the twin's own by its code and entry."""
    if ":" not in record["family"]:
        return {"name": record["family"]}
    return {"artifact": record["code"].split(":", 1)[1], "entry": record["family"]}


@dataclass
class World:
    """A twin with its history, its models and what predicting needs."""

    twin: Twin
    versions: dict[int, VersionRef] = field(default_factory=dict)
    kept: Kept = field(default_factory=Kept)
    # The version the tests' scenarios start from: the one that registered the models.
    base: VersionRef | None = None
    names: int = 0

    @property
    def store(self):
        return self.twin.store

    @property
    def first(self) -> VersionRef:
        return self.versions[min(self.versions)]

    def name(self, stem: str = "what-if") -> str:
        """A scenario name no other test of the module has used."""
        self.names += 1
        return f"{stem} {self.names}"

    def request(self, models: Sequence[str] = ("level-naive",), **more: Any) -> ScenarioRequest:
        body = {
            "name": self.name(),
            "horizon": 8 * STEP,
            "models": [model(key) for key in models],
            **more,
        }
        return ScenarioRequest(**body)

    def fit(self, family: Mapping[str, Any], target: str, params: Mapping[str, Any]) -> str:
        """Fit a family on the whole history; the hash its state is kept under."""
        from dta_runtime.models import ops

        request = FitRequest(
            budget_s=120.0,
            trace_id=TRACE,
            family=dict(family),
            params=dict(params),
            frame={
                "target": target,
                "start": self.first.id,
                "end": self.versions[max(self.versions)].id,
                "step": STEP,
            },
            horizon=8,
        )
        return ops.fit(request, Stored(self.twin), self.kept).state


def setting(value: Any, start: datetime, end: datetime | None = None, key: str = "V1") -> Row:
    """The setting of a valve, held at a value: a property no model predicts and the
    manifest does not call a control."""
    return {
        "target": "Valve.setting",
        "uid": link(key),
        "value": value,
        "start": start,
        "end": end,
    }


def closed(start: datetime, end: datetime | None = None, target: str = "Valve.status") -> Row:
    """The valve that feeds T1, closed: its status is what the manifest says can be set."""
    return {"target": target, "uid": link("V1"), "value": "closed", "start": start, "end": end}


def create(world: World, request: ScenarioRequest) -> dict[str, Any]:
    """The scenario of a request, from the base of the tests on the tests' grid."""
    from dta_runtime import scenarios

    assert world.base is not None
    return scenarios.create(world.twin, request, at=world.base, step=STEP)


class InProcess:
    """The predictor of the tests: what the worker does for the API, done here. The state
    was fitted once, when the model was registered; it is applied to the history the
    store holds up to the base version."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        record: Mapping[str, Any],
        *,
        base: VersionRef,
        branch: str,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        from dta_runtime.models import ops

        self.calls.append(
            {
                "model": record["uid"],
                "base": base.id,
                "branch": branch,
                "horizon": horizon,
                "step": step,
                "interventions": list(interventions),
                "deadline_s": deadline_s,
            }
        )
        request = PredictRequest(
            budget_s=max(deadline_s, 0.001),
            trace_id=TRACE,
            family=family_ref(record),
            params=json.loads(record["params"] or "{}"),
            state=record["fitted"].split(":", 1)[1],
            history={
                "target": record["target"],
                "start": self.world.first.id,
                "end": base.id,
                "branch": LIVE_BRANCH,
                "step": step,
            },
            horizon=horizon,
            # A family that does not hold under interventions is never given any.
            interventions=[found.model_dump() for found in interventions]
            if record["interventional"]
            else [],
        )
        table, _ = ops.predict(request, Stored(self.world.twin), self.world.kept)
        return table


def build_world(settings: StoreSettings, ontology_text: str) -> World:
    """The history and the models of the module's docstring, in a new database."""
    installed = install_twin(
        settings, Bundle(manifest=manifest_for(ontology_text), ontology_text=ontology_text), t=T0
    )
    world = World(twin=installed.twin)
    twin = world.twin
    world.versions[1] = commit(
        twin,
        at(1),
        {
            "Junction": [junction("J1", demand("J1", 1)), junction("J2", demand("J2", 1))],
            "Tank": [tank("T1", level("T1", 1)), tank("T2", level("T2", 1))],
            "Valve": [valve("V1", "J1", "T1", 1.0), valve("V2", "J1", "T2", 2.0)],
            "Pipe": [
                {
                    "uid": link("P1"),
                    "start_node": node("J1"),
                    "end_node": node("J2"),
                    "length": 100.0,
                    "diameter": 200.0,
                    "roughness": 0.5,
                }
            ],
        },
    )
    for step in range(2, LAST + 1):
        world.versions[step] = commit(
            twin,
            at(step),
            {
                "Tank": [
                    {"uid": node("T1"), "level": level("T1", step)},
                    {"uid": node("T2"), "level": level("T2", step)},
                ],
                "Junction": [{"uid": node("J1"), "demand": demand("J1", step)}],
            },
        )

    season = {"season_length": 4}
    balance = {"inflow": INFLOW, "outflow": OUTFLOW, "feeds": FEEDS}
    code = world.kept.put(PLANT_CODE.encode("utf-8"), "text/x-python")
    naive = world.fit({"name": "seasonal_naive"}, "Tank.level", season)
    last = world.fit({"name": "persistence"}, "Junction.demand", {})
    again = world.fit({"name": "persistence"}, "Tank.level", {})
    own = world.fit({"artifact": code, "entry": BALANCE}, "Tank.level", balance)
    states = {naive, last, again, own}
    world.base = register(
        twin,
        {
            "TwinArtifact": [
                artifact_row(code, len(PLANT_CODE), "text/x-python"),
                *(artifact_row(sha, len(world.kept.files[sha])) for sha in sorted(states)),
            ],
            "TwinModel": [
                model_row(
                    "level-naive", "seasonal_naive", "Tank.level", params=season, fitted=naive
                ),
                model_row("demand-last", "persistence", "Junction.demand", fitted=last),
                model_row(
                    "level-balance",
                    BALANCE,
                    "Tank.level",
                    params=balance,
                    fitted=own,
                    code=code,
                    interventional=True,
                ),
                model_row("level-again", "persistence", "Tank.level", fitted=again),
                model_row(
                    "level-candidate",
                    "seasonal_naive",
                    "Tank.level",
                    params=season,
                    fitted=naive,
                    status="candidate",
                ),
                model_row("level-unfitted", "persistence", "Tank.level"),
                model_row("lost-target", "persistence", "Tank.depth", fitted=again),
                model_row("tank-name", "persistence", "Tank.name", fitted=again),
            ],
        },
    )
    return world


def write_plant_code(folder: Path) -> None:
    """Put the twin's own family where the worker would: in a directory on `sys.path`."""
    (folder / f"{PLANT_MODULE}.py").write_text(PLANT_CODE, encoding="utf-8")


# -- reading a branch the plain way ------------------------------------------------------


def cells(
    world: World,
    type_name: str,
    name: str,
    version: VersionRef | str = "head",
    branch: str = LIVE_BRANCH,
) -> dict[str, tuple[Any, dict[str, Any]]]:
    """Per object of a type at a version: the value of one property and its provenance
    as stored (`s` source, `k` kind, `c` confidence, `t` valid time)."""
    table = world.store.table(
        type_name,
        columns=[name],
        at=version,
        branch=branch,
        prov=True,
        ontology=world.twin.ontology,
    )
    found = {}
    for row in table.to_pylist():
        provenance = json.loads(row["_prov"])
        found[row["uid"]] = (row[name], provenance.get(name) or provenance["*"])
    return found


DOMAIN_TYPES = ("Junction", "Tank", "Valve", "Pipe")


def origins(world: World, version: VersionRef | str = "head", branch: str = LIVE_BRANCH) -> set:
    """(kind, source) of every value of the network's objects at a version."""
    found = set()
    for type_name in DOMAIN_TYPES:
        table = world.store.table(
            type_name,
            columns=[],
            at=version,
            branch=branch,
            prov=True,
            ontology=world.twin.ontology,
        )
        for text in table.column("_prov").to_pylist():
            found |= {(entry["k"], entry["s"]) for entry in json.loads(text).values()}
    return found


def branch_names(world: World) -> list[str]:
    return [found.name for found in world.store.branches()]


def branch_versions(world: World, branch: str) -> list[Any]:
    """The versions a branch has after the base of the tests' scenarios."""
    assert world.base is not None
    return [v for v in world.store.versions(branch=branch) if v.seq > world.base.seq]


class Counted:
    """Counts the reads made of a store while it is in place."""

    READS = ("table", "changes", "get", "history", "diff", "find", "edges", "count")

    def __init__(self, world: World) -> None:
        self.store = world.store
        self.calls: dict[str, int] = dict.fromkeys(self.READS, 0)

    def __enter__(self) -> Counted:
        for name in self.READS:
            method = getattr(self.store, name)

            def counting(*args: Any, _name: str = name, _method: Any = method, **kwargs: Any):
                self.calls[_name] += 1
                return _method(*args, **kwargs)

            setattr(self.store, name, counting)
        return self

    def __exit__(self, *exc: Any) -> None:
        for name in self.READS:
            delattr(self.store, name)


def prepared(
    series: Mapping[str, Callable[[int], float]], steps: int, base: datetime, step: timedelta = STEP
) -> pa.Table:
    """A prediction made without a model: per series, a value for each grid time."""
    uids, times, means = [], [], []
    for uid in sorted(series):
        for j in range(1, steps + 1):
            uids.append(uid)
            times.append(base + j * step)
            means.append(float(series[uid](j)))
    return pa.table(
        {
            "unique_id": pa.array(uids, pa.string()),
            "ds": pa.array(times, TIME),
            "mean": pa.array(means, pa.float64()),
        }
    )
