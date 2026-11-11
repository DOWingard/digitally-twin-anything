"""A small water network with a known history, built once and read by the API and graph tests.

Topology (link: start -> end):

    PU1: R1 -> J1     P1: J1 -> J2     P2: J2 -> J3     P3: J3 -> J4
    P4:  J2 -> T1     P5: J4 -> J5     P6: T1 -> J3     V1: J1 -> J3 (removed at #10)
    P7:  J5 -> J6 (added at #18)       on branch `scenario`: P9: J6 -> J9

Sensors S1 (measures J2) and S2 (measures T1); readings of S1 refer to it.
"""

from __future__ import annotations

import dataclasses
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.twin import Bundle, Twin, install_twin
from httpx import Response
from runtime_testkit import grow_ontology

with warnings.catch_warnings():
    # Starlette suggests another HTTP client package for its test client; the
    # one installed works, and adding a package is not wanted for tests.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

T0 = datetime(2026, 1, 5, tzinfo=UTC)
TOKEN = "test-token-0123456789abcdefghij"
SCENARIO = "scenario"

# seq of the notable versions of the live branch
NETWORK_SEQ = 2
SENSORS_SEQ = 3
VALVE_REMOVED_SEQ = 10
BACKFILL_SEQS = (13, 14)
MIGRATION_SEQ = 15
MATERIAL_SEQ = 17
J6_SEQ = 18
BRANCH_FROM_SEQ = 20
HEAD_SEQ = 21
DEMAND_SEQS = (4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 19, 20, 21)
READING_SEQS = (4, 5, 6, 7, 8, 9)

JUNCTIONS = ("J1", "J2", "J3", "J4", "J5")
LENGTHS = {"P1": 100.0, "P2": 200.0, "P3": 150.0, "P4": 50.0, "P5": 300.0, "P6": 60.0}
ELEVATIONS = {"J1": 50.0, "J2": 45.0, "J3": 40.0, "J4": 35.0, "J5": 30.0, "J6": 25.0}
PIPE_ENDS = {
    "P1": ("J1", "J2"),
    "P2": ("J2", "J3"),
    "P3": ("J3", "J4"),
    "P4": ("J2", "T1"),
    "P5": ("J4", "J5"),
    "P6": ("T1", "J3"),
}
MATERIALS = {"P1": "PVC", "P2": "PVC", "P3": "iron"}


def node(key: str) -> str:
    return f"node:{key}"


def link(key: str) -> str:
    return f"link:{key}"


def time_of(seq: int) -> datetime:
    """Version time of a commit of the live branch after installation."""
    return T0 + timedelta(hours=seq)


def demand(junction: str, seq: int) -> float:
    return float(int(junction[1:])) + 0.5 * seq


def level(seq: int) -> float:
    return round(2.0 + 0.1 * seq, 6)


def manifest_for(ontology_text: str, revision: int = 1) -> Manifest:
    document = {
        "twin": {"id": "water", "name": "Water network", "revision": revision},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/water_network.yaml",
            "type_signature": type_signature(parse_ontology(ontology_text).ontology),
        },
    }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


def database_name(server: StoreSettings, purpose: str) -> str:
    return f"{server.database}_{purpose}_{uuid.uuid4().hex[:8]}"


@dataclass
class WaterHistory:
    settings: StoreSettings
    twin: Twin
    versions: dict[int, VersionRef] = field(default_factory=dict)
    scenario: VersionRef | None = None

    @property
    def head(self) -> VersionRef:
        return self.versions[HEAD_SEQ]


def _prov(source: str, t: datetime, kind: FactKind = FactKind.observed) -> Provenance:
    return Provenance(source=source, kind=kind, t_valid=t)


def _commit(
    history: WaterHistory,
    seq: int,
    changes: ChangeSet,
    *,
    cause: str = "sync",
    actor: str = "scada",
    **more: Any,
) -> None:
    version = history.twin.store.commit(
        changes, CommitMeta(cause=cause, actor=actor, t=time_of(seq), **more)
    )
    assert version.seq == seq, (version.seq, seq)
    history.versions[seq] = version


def _demands(seq: int, junctions: tuple[str, ...] = JUNCTIONS) -> list[dict[str, Any]]:
    return [{"uid": node(key), "demand": demand(key, seq)} for key in junctions]


def _state(history: WaterHistory, seq: int, **more: Any) -> None:
    junctions = JUNCTIONS + (("J6",) if seq > J6_SEQ else ())
    upserts: dict[str, list[dict[str, Any]]] = {
        "Junction": _demands(seq, junctions),
        "Tank": [{"uid": node("T1"), "level": level(seq)}],
    }
    if seq in READING_SEQS:
        upserts["Reading"] = [
            {
                "uid": f"reading:S1-{seq}",
                "sensor": "sensor:S1",
                "observed_at": time_of(seq),
                "value": 50.0 + seq,
                "quality": "good",
                "sequence": seq,
            }
        ]
    t_valid = time_of(seq) - timedelta(days=30) if seq in BACKFILL_SEQS else time_of(seq)
    _commit(
        history,
        seq,
        ChangeSet(upserts=upserts, provenance=_prov("scada", t_valid)),
        **more,
    )


def build_water_history(settings: StoreSettings, ontology_text: str) -> WaterHistory:
    """Install the water twin on a new database and write its whole history."""
    installed = install_twin(
        settings, Bundle(manifest=manifest_for(ontology_text), ontology_text=ontology_text), t=T0
    )
    twin = installed.twin
    history = WaterHistory(settings=settings, twin=twin)
    history.versions[0] = twin.store.resolve("#0")[0]
    history.versions[1] = installed.version

    nodes: dict[str, list[dict[str, Any]]] = {
        "Reservoir": [
            {"uid": node("R1"), "name": "R1", "elevation": 100.0, "head": 120.0, "x": 0.0, "y": 0.0}
        ],
        "Junction": [
            {
                "uid": node(key),
                "name": key,
                "elevation": ELEVATIONS[key],
                "base_demand": float(index),
                "x": 10.0 * index,
                "y": 5.0,
            }
            for index, key in enumerate(JUNCTIONS, start=1)
        ],
        "Tank": [
            {
                "uid": node("T1"),
                "name": "T1",
                "elevation": 60.0,
                "min_level": 1.0,
                "max_level": 10.0,
                "level": level(NETWORK_SEQ),
                "volume_curve": [0.0, 10.0, 25.0],
                "x": 20.0,
                "y": 20.0,
            }
        ],
        "Pipe": [
            {
                "uid": link(key),
                "start_node": node(start),
                "end_node": node(end),
                "length": LENGTHS[key],
                "diameter": 200.0,
                "roughness": 100.0,
                "lined": index % 2 == 0,
                "status": "open",
            }
            for index, (key, (start, end)) in enumerate(PIPE_ENDS.items())
        ],
        "Pump": [
            {
                "uid": link("PU1"),
                "start_node": node("R1"),
                "end_node": node("J1"),
                "curve": [{"flow": 0.0, "head": 50.0}, {"flow": 10.0, "head": 40.0}],
                "speed": 1.0,
                "status": "open",
            }
        ],
        "Valve": [
            {
                "uid": link("V1"),
                "start_node": node("J1"),
                "end_node": node("J3"),
                "valve_type": "prv",
                "setting": 30.0,
                "status": "open",
            }
        ],
    }
    _commit(
        history,
        NETWORK_SEQ,
        ChangeSet(upserts=nodes, provenance=_prov("gis", time_of(NETWORK_SEQ))),
        actor="gis-import",
        note="network loaded",
    )
    sensors = [
        {
            "uid": "sensor:S1",
            "measures": node("J2"),
            "quantity": "pressure",
            "serial": "SN-000001",
            "installed_on": date(2025, 12, 1),
            "active": True,
            "sample_interval": 60,
            "tags": ["north", "critical"],
            "notes": "mounted on the main",
        },
        {"uid": "sensor:S2", "measures": node("T1"), "quantity": "level", "active": True},
    ]
    _commit(
        history,
        SENSORS_SEQ,
        ChangeSet(upserts={"Sensor": sensors}, provenance=_prov("scada", time_of(SENSORS_SEQ))),
    )
    for seq in range(4, VALVE_REMOVED_SEQ):
        _state(history, seq, run="run-1" if seq < 7 else "run-2")
    _commit(
        history,
        VALVE_REMOVED_SEQ,
        ChangeSet(deletes={"Valve": [link("V1")]}),
        cause="manual",
        actor="operator",
        note="valve removed",
    )
    for seq in range(VALVE_REMOVED_SEQ + 1, MIGRATION_SEQ):
        backfill = {"extra": {"backfill": True}} if seq in BACKFILL_SEQS else {}
        _state(history, seq, **backfill)

    grown = grow_ontology(ontology_text)
    migrated = install_twin(
        settings, Bundle(manifest=manifest_for(grown, revision=2), ontology_text=grown)
    )
    assert migrated.actions == ("migrated", "registered"), migrated.actions
    twin.close()
    twin = migrated.twin
    history.twin = twin
    for version in twin.store.resolve(f"#{MIGRATION_SEQ}..head"):
        history.versions[version.seq] = version
    assert max(history.versions) == MIGRATION_SEQ + 1

    _commit(
        history,
        MATERIAL_SEQ,
        ChangeSet(
            upserts={
                "Pipe": [{"uid": link(key), "material": value} for key, value in MATERIALS.items()]
            },
            provenance=_prov("survey", time_of(MATERIAL_SEQ), FactKind.assumed),
        ),
        cause="manual",
        actor="engineer",
    )
    _commit(
        history,
        J6_SEQ,
        ChangeSet(
            upserts={
                "Junction": [
                    {"uid": node("J6"), "name": "J6", "elevation": ELEVATIONS["J6"], "x": 60.0}
                ],
                "Pipe": [
                    {
                        "uid": link("P7"),
                        "start_node": node("J5"),
                        "end_node": node("J6"),
                        "length": 80.0,
                        "diameter": 150.0,
                    }
                ],
            },
            provenance=_prov("gis", time_of(J6_SEQ)),
        ),
        actor="gis-import",
    )
    for seq in range(J6_SEQ + 1, BRANCH_FROM_SEQ + 1):
        _state(history, seq)

    store = twin.store
    store.branch_create(SCENARIO, at=f"#{BRANCH_FROM_SEQ}")
    history.scenario = store.commit(
        ChangeSet(
            upserts={
                "Junction": [
                    {"uid": node("J9"), "name": "J9", "elevation": 20.0},
                    {"uid": node("J1"), "demand": 99.0},
                ],
                "Pipe": [
                    {
                        "uid": link("P9"),
                        "start_node": node("J6"),
                        "end_node": node("J9"),
                        "length": 40.0,
                        "diameter": 100.0,
                    }
                ],
            },
            provenance=_prov("planner-model", time_of(BRANCH_FROM_SEQ), FactKind.simulated),
        ),
        CommitMeta(
            cause="scenario", actor="planner", t=time_of(BRANCH_FROM_SEQ) + timedelta(minutes=30)
        ),
        branch=SCENARIO,
    )
    _state(history, HEAD_SEQ)
    return history


def with_database(server: StoreSettings, name: str) -> StoreSettings:
    return dataclasses.replace(server, database=name)


class Api:
    """Requests with the token, against the session's API."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Response:
        return self.client.get(path, headers={**self.headers, **(headers or {})}, **kwargs)

    def post(self, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Response:
        return self.client.post(path, headers={**self.headers, **(headers or {})}, **kwargs)

    def ok(self, path: str, **kwargs: Any) -> Any:
        response = self.get(path, **kwargs)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()
