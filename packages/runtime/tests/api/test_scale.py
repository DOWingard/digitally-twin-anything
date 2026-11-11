"""Latency of the API on a full-size twin: 20,000 assets, 20,000 sensors, 60,000 links
and 200 versions of 400 changed rows each. Run with `-m scale`."""

from __future__ import annotations

import io
import os
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest
import yaml
from api_testkit import TOKEN, database_name, with_database
from dta_core.log import Log
from dta_core.manifest import parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings
from dta_runtime.api import create_app
from dta_runtime.settings import RuntimeSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from starlette.testclient import TestClient

pytestmark = pytest.mark.scale
log = Log("test.scale")

ASSETS = 20_000
SENSORS = 20_000
LINKS = 60_000
VERSIONS = 200
ROWS_PER_VERSION = 400
LOAD_CHUNK = 20_000
T0 = datetime(2026, 2, 2, tzinfo=UTC)
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

ONTOLOGY = """\
id: https://example.org/plant
name: plant
description: A large plant of connected assets, each watched by a sensor.
prefixes:
  linkml: https://w3id.org/linkml/
  pl: https://example.org/plant/
default_prefix: pl
default_range: string
imports:
  - linkml:types

classes:
  Asset:
    description: Equipment of the plant.
    attributes:
      uid:
        identifier: true
        required: true
      name:
        required: true
      kind:
        range: AssetKind
      capacity:
        range: float
      value:
        range: float
        annotations:
          dta_role: state
      temperature:
        range: float
        annotations:
          dta_role: state

  Sensor:
    description: An instrument on one asset.
    attributes:
      uid:
        identifier: true
        required: true
      asset:
        range: Asset
        required: true
      reading:
        range: float
        annotations:
          dta_role: state

  Connection:
    description: A connection from one asset to another.
    annotations:
      link_source: source
      link_target: target
    attributes:
      uid:
        identifier: true
        required: true
      source:
        range: Asset
        required: true
      target:
        range: Asset
        required: true
      length:
        range: float

enums:
  AssetKind:
    permissible_values:
      pump: {}
      valve: {}
      tank: {}
      pipe: {}
"""

THRESHOLDS_MS = {
    "one object p95": 100.0,
    "2-hop neighbours p95": 300.0,
    "node table 20,000 rows (Arrow)": 3_000.0,
    "subgraph depth 4, first": 5_000.0,
    "subgraph depth 4, cached p95": 500.0,
    "slice 200 x 3 x 200 (long state)": 5_000.0,
}


def asset(index: int) -> str:
    return f"asset:A{index:05d}"


@dataclass
class Plant:
    settings: StoreSettings
    twin: Twin
    first_update_seq: int
    last_update_seq: int
    measured: dict[str, float] = field(default_factory=dict)
    load_s: float = 0.0


def _manifest() -> object:
    document = {
        "twin": {"id": "plant", "name": "Plant", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/plant.yaml",
            "type_signature": type_signature(parse_ontology(ONTOLOGY).ontology),
        },
    }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="scale manifest")


def _load(settings: StoreSettings) -> Plant:
    started = time.perf_counter()
    installed = install_twin(settings, Bundle(manifest=_manifest(), ontology_text=ONTOLOGY), t=T0)
    twin = installed.twin
    store = twin.store
    rng = random.Random(7)
    kinds = ["pump", "valve", "tank", "pipe"]
    rows: list[tuple[str, dict]] = []
    rows += [
        (
            "Asset",
            {
                "uid": asset(i),
                "name": f"A{i:05d}",
                "kind": kinds[i % 4],
                "capacity": float(i % 97),
                "value": 0.0,
                "temperature": 20.0,
            },
        )
        for i in range(ASSETS)
    ]
    rows += [
        ("Sensor", {"uid": f"sensor:S{i:05d}", "asset": asset(i % ASSETS), "reading": 0.0})
        for i in range(SENSORS)
    ]
    for i in range(LINKS):
        source = i % ASSETS
        target = rng.randrange(ASSETS - 1)
        target += target >= source
        rows.append(
            (
                "Connection",
                {
                    "uid": f"connection:C{i:05d}",
                    "source": asset(source),
                    "target": asset(target),
                    "length": float(1 + rng.randrange(500)),
                },
            )
        )
    minute = 0
    for begin in range(0, len(rows), LOAD_CHUNK):
        minute += 1
        upserts: dict[str, list[dict]] = {}
        for type_name, row in rows[begin : begin + LOAD_CHUNK]:
            upserts.setdefault(type_name, []).append(row)
        t = T0 + timedelta(minutes=minute)
        chunk_started = time.perf_counter()
        store.commit(
            ChangeSet(upserts=upserts, provenance=Provenance("plant", FactKind.observed, t)),
            CommitMeta(cause="sync", actor="loader", t=t),
        )
        log.info(
            "scale fixture: loaded a chunk",
            rows=sum(len(part) for part in upserts.values()),
            elapsed_ms=round((time.perf_counter() - chunk_started) * 1000.0, 1),
        )
    first_update = None
    for version in range(VERSIONS):
        minute += 1
        t = T0 + timedelta(minutes=minute)
        changed = [
            {
                "uid": asset((version * ROWS_PER_VERSION + i) % ASSETS),
                "value": float(version),
                "temperature": 20.0 + version / 10.0,
            }
            for i in range(ROWS_PER_VERSION)
        ]
        ref = store.commit(
            ChangeSet(
                upserts={"Asset": changed}, provenance=Provenance("plant", FactKind.observed, t)
            ),
            CommitMeta(cause="sync", actor="loader", t=t),
        )
        first_update = first_update if first_update is not None else ref.seq
    elapsed = time.perf_counter() - started
    log.info(
        "scale fixture: loaded",
        database=settings.database,
        head_seq=store.head().seq,
        elapsed_s=round(elapsed, 1),
    )
    assert first_update is not None
    return Plant(
        settings=settings,
        twin=twin,
        first_update_seq=first_update,
        last_update_seq=store.head().seq,
        load_s=elapsed,
    )


@pytest.fixture(scope="module")
def plant(server_settings: StoreSettings, _databases: list[str]) -> Iterator[Plant]:
    name = database_name(server_settings, "scale")
    _databases.append(name)
    loaded = _load(with_database(server_settings, name))
    yield loaded
    loaded.twin.close()
    report = [f"scale fixture loaded in {loaded.load_s:.1f} s"]
    report += [
        f"{label}: {loaded.measured[label]:.1f} ms (threshold {limit:.0f} ms)"
        for label, limit in THRESHOLDS_MS.items()
        if label in loaded.measured
    ]
    report += [
        f"{label}: {value:.1f}"
        for label, value in loaded.measured.items()
        if label not in THRESHOLDS_MS
    ]
    one, five, fifteen = os.getloadavg()
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info("scale measurements", **{k: round(v, 1) for k, v in loaded.measured.items()})


def _settings(plant: Plant, tmp: Path) -> RuntimeSettings:
    return RuntimeSettings(store=plant.settings, api_token=TOKEN, artifacts_dir=tmp, logs_dir=tmp)


@pytest.fixture(scope="module")
def scale_client(plant: Plant, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(plant, tmp_path_factory.mktemp("scale")))) as client:
        yield client


def _timed(client: TestClient, method: str, path: str, **kwargs: object) -> tuple[float, object]:
    started = time.perf_counter()
    response = client.request(method, path, headers=HEADERS, **kwargs)  # type: ignore[arg-type]
    elapsed = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200, (path, response.text[:500])
    return elapsed, response


def _p95(samples: list[float]) -> float:
    return float(np.percentile(np.asarray(samples), 95))


def _check(plant: Plant, label: str, value: float) -> None:
    plant.measured[label] = value
    assert value <= THRESHOLDS_MS[label], f"{label}: {value:.1f} ms"


def test_one_object(plant: Plant, scale_client: TestClient) -> None:
    rng = random.Random(1)
    for _ in range(5):
        _timed(scale_client, "GET", f"/api/v1/objects/Asset/{asset(rng.randrange(ASSETS))}")
    samples = []
    for _ in range(100):
        elapsed, response = _timed(
            scale_client, "GET", f"/api/v1/objects/Asset/{asset(rng.randrange(ASSETS))}"
        )
        samples.append(elapsed)
    assert response.json()["data"]["type"] == "Asset"  # type: ignore[attr-defined]
    _check(plant, "one object p95", _p95(samples))


def test_two_hop_neighbours(plant: Plant, scale_client: TestClient) -> None:
    rng = random.Random(2)
    samples = []
    sizes = []
    for round_ in range(45):
        elapsed, response = _timed(
            scale_client,
            "GET",
            "/api/v1/graph/neighbors",
            params={"seed": asset(rng.randrange(ASSETS)), "depth": 2},
        )
        if round_ >= 5:
            samples.append(elapsed)
            sizes.append(len(response.json()["nodes"]))  # type: ignore[attr-defined]
    assert min(sizes) > 10
    _check(plant, "2-hop neighbours p95", _p95(samples))


def test_node_table(plant: Plant, scale_client: TestClient) -> None:
    samples = []
    for _ in range(3):
        elapsed, response = _timed(
            scale_client, "GET", "/api/v1/block/nodes/Asset", params={"format": "arrow"}
        )
        table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()  # type: ignore[attr-defined]
        assert table.num_rows == ASSETS
        samples.append(elapsed)
    _check(plant, "node table 20,000 rows (Arrow)", max(samples))


def test_subgraph_depth_four(plant: Plant, tmp_path: Path) -> None:
    rng = random.Random(3)
    with TestClient(create_app(_settings(plant, tmp_path))) as client:
        body = {"seeds": [asset(rng.randrange(ASSETS))], "depth": 4}
        first, response = _timed(client, "POST", "/api/v1/graph/subgraph", json=body)
        assert len(response.json()["nodes"]) == 1000  # type: ignore[attr-defined]
        samples = []
        for _ in range(20):
            body = {"seeds": [asset(rng.randrange(ASSETS))], "depth": 4}
            elapsed, response = _timed(client, "POST", "/api/v1/graph/subgraph", json=body)
            samples.append(elapsed)
    _check(plant, "subgraph depth 4, first", first)
    _check(plant, "subgraph depth 4, cached p95", _p95(samples))


def test_slice(plant: Plant, scale_client: TestClient) -> None:
    first, last = plant.first_update_seq, plant.last_update_seq
    assert last - first + 1 == VERSIONS
    params = {
        "range": f"#{first}..#{last}",
        "properties": "value,temperature,capacity",
        "uids": ",".join(asset(i) for i in range(200)),
        "format": "arrow",
    }
    samples = []
    for _ in range(3):
        elapsed, response = _timed(scale_client, "GET", "/api/v1/block/slice/Asset", params=params)
        table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()  # type: ignore[attr-defined]
        assert table.num_rows == 200 * VERSIONS
        assert table.column_names == ["_version", "_seq", "_t", "uid", "value", "temperature",
                                      "capacity"]  # fmt: skip
        samples.append(elapsed)
    rows = table.filter(pc.equal(table.column("uid"), asset(0))).to_pylist()
    # asset 0 changes in the first update and every 50 versions after it
    assert [row["value"] for row in rows[:51:50]] == [0.0, 50.0]
    _check(plant, "slice 200 x 3 x 200 (long state)", max(samples))


def test_paths_without_a_threshold(plant: Plant, scale_client: TestClient) -> None:
    """k shortest paths between random assets, measured for information only."""
    rng = random.Random(4)
    samples = []
    refused = 0
    for _ in range(20):
        params = {
            "source": asset(rng.randrange(ASSETS)),
            "target": asset(rng.randrange(ASSETS)),
            "k": 3,
            "max_depth": 6,
            "edge_types": "Connection",
        }
        started = time.perf_counter()
        response = scale_client.get("/api/v1/graph/paths", params=params, headers=HEADERS)
        elapsed = (time.perf_counter() - started) * 1000.0
        assert response.status_code in (200, 400), response.text[:500]
        if response.status_code == 400:
            assert response.json()["code"] == "graph.too_broad"
            refused += 1
        else:
            samples.append(elapsed)
    plant.measured["paths k=3 depth<=6 answered p95 (ms)"] = _p95(samples) if samples else 0.0
    plant.measured["paths refused as too broad (of 20)"] = float(refused)
