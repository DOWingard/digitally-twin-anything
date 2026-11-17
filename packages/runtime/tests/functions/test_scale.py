"""How long the built-ins take on a full-size twin: 10,000 objects of one type and 200
versions, 5 % of the objects changing in each. Run with `-m scale`."""

from __future__ import annotations

import logging
import os
import random
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from api_testkit import database_name, with_database
from dta_core.log import Log
from dta_core.manifest import parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings
from dta_runtime.protocol import Answer
from dta_runtime.twin import Bundle, install_twin
from functions_testkit import History, ask

pytestmark = pytest.mark.scale
log = Log("test.scale")

OBJECTS = 10_000
VERSIONS = 200
CHANGED = OBJECTS // 20
STEP = timedelta(minutes=15)
T0 = datetime(2026, 2, 2, tzinfo=UTC)
KINDS = ("pump", "valve", "tank", "pipe")
RUNS = 3

ONTOLOGY = """\
id: https://example.org/plant
name: plant
description: A plant of assets, each with one value that changes.
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
      value:
        range: float
        unit:
          ucum_code: m
        annotations:
          dta_role: state

enums:
  AssetKind:
    permissible_values:
      pump: {}
      valve: {}
      tank: {}
      pipe: {}
"""

# Fixed before anything was measured.
THRESHOLDS_S = {
    "latest": 0.5,
    "below over the whole range": 3.0,
    "integrate by day over the whole range": 3.0,
    "series with a step for 100 uids": 1.0,
    "aggregate with group_by": 0.5,
}


def asset(index: int) -> str:
    return f"asset:A{index:05d}"


@dataclass
class Plant:
    history: History
    first: int
    last: int
    load_s: float
    # label -> seconds of each run
    measured: dict[str, list[float]] = field(default_factory=dict)
    # label -> seconds one run spent in reads of the store
    store_s: dict[str, float] = field(default_factory=dict)


def _load(settings: StoreSettings) -> Plant:
    started = time.perf_counter()
    document = {
        "twin": {"id": "plant", "name": "Plant", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/plant.yaml",
            "type_signature": type_signature(parse_ontology(ONTOLOGY).ontology),
        },
    }
    manifest = parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="scale manifest")
    installed = install_twin(settings, Bundle(manifest=manifest, ontology_text=ONTOLOGY), t=T0)
    history = History(twin=installed.twin)
    store = installed.twin.store
    rng = random.Random(5)

    def commit(t: datetime, rows: list[dict[str, Any]]) -> int:
        version = store.commit(
            ChangeSet(
                upserts={"Asset": rows}, provenance=Provenance("plant", FactKind.observed, t)
            ),
            CommitMeta(cause="sync", actor="loader", t=t),
        )
        history.versions[version.seq] = version
        return version.seq

    first = commit(
        T0 + STEP,
        [
            {
                "uid": asset(index),
                "name": f"A{index:05d}",
                "kind": KINDS[index % len(KINDS)],
                "value": round(rng.uniform(0, 100), 3),
            }
            for index in range(OBJECTS)
        ],
    )
    last = first
    for version in range(VERSIONS):
        last = commit(
            T0 + (version + 2) * STEP,
            [
                {
                    "uid": asset((version * CHANGED + index) % OBJECTS),
                    # One change in fifty takes the value away.
                    "value": None if rng.random() < 0.02 else round(rng.uniform(0, 100), 3),
                }
                for index in range(CHANGED)
            ],
        )
    elapsed = time.perf_counter() - started
    log.info("scale fixture: loaded", versions=last - first, elapsed_s=round(elapsed, 1))
    return Plant(history=history, first=first, last=last, load_s=elapsed)


@pytest.fixture(scope="module")
def plant(server_settings: StoreSettings, _databases: list[str]) -> Iterator[Plant]:
    name = database_name(server_settings, "fn_scale")
    _databases.append(name)
    loaded = _load(with_database(server_settings, name))
    yield loaded
    loaded.history.twin.close()
    report = [f"scale fixture loaded in {loaded.load_s:.1f} s"]
    report += [
        f"{label}: slowest {max(runs):.3f} s, median {statistics.median(runs):.3f} s, "
        f"fastest {min(runs):.3f} s of {len(runs)} (threshold {THRESHOLDS_S[label]:.1f} s); "
        f"reading the store {loaded.store_s.get(label, 0.0):.3f} s of one more run"
        for label, runs in loaded.measured.items()
    ]
    one, five, fifteen = os.getloadavg()
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info(
        "scale measurements",
        **{label: [round(run, 3) for run in runs] for label, runs in loaded.measured.items()},
    )


class _StoreReads(logging.Handler):
    """Adds up what the store says each of its reads took."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seconds = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "context", "") == "store.read":
            self.seconds += getattr(record, "data", {}).get("elapsed_ms", 0.0) / 1000.0


def _store_seconds(plant: Plant, entry: str, call: dict[str, Any]) -> float:
    """How much of one more run, not counted among the measured ones, is spent reading
    the store: what is left is the function's own work."""
    logger = logging.getLogger("dta")
    handler, level = _StoreReads(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        ask(plant.history, entry, call)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    return handler.seconds


def _timed(plant: Plant, label: str, entry: str, call: dict[str, Any]) -> Answer:
    """The answer, asked for several times; every run has to be within the threshold."""
    runs = []
    for _ in range(RUNS):
        started = time.perf_counter()
        answer = ask(plant.history, entry, call)
        runs.append(time.perf_counter() - started)
    plant.measured[label] = runs
    plant.store_s[label] = _store_seconds(plant, entry, call)
    assert max(runs) <= THRESHOLDS_S[label], f"{label}: {[round(run, 3) for run in runs]} s"
    return answer


def _whole(plant: Plant) -> dict[str, Any]:
    return {"property": "Asset.value", "from": f"#{plant.first}", "to": "head"}


def test_the_twin_is_of_the_size_stated(plant: Plant):
    assert plant.last - plant.first == VERSIONS
    store = plant.history.twin.store
    assert store.count("Asset", at=plant.history.head) == OBJECTS
    changes = store.changes("Asset", f"#{plant.first + 1}..#{plant.last}", columns=[])
    assert changes.num_rows == VERSIONS * CHANGED == VERSIONS * OBJECTS // 20


def test_latest(plant: Plant):
    answer = _timed(plant, "latest", "latest", {"property": "Asset.value"})
    assert answer.value.num_rows == OBJECTS


def test_below_over_the_whole_range(plant: Plant):
    answer = _timed(
        plant, "below over the whole range", "below", {**_whole(plant), "threshold": 50}
    )
    # Values are spread evenly over 0 to 100, so about half of what is in force is below.
    assert answer.value.num_rows > OBJECTS
    assert answer.value.column("extreme").null_count == 0


def test_integrate_by_day_over_the_whole_range(plant: Plant):
    answer = _timed(
        plant,
        "integrate by day over the whole range",
        "integrate",
        {**_whole(plant), "period": "P1D"},
    )
    # 200 steps of 15 minutes from 00:15 on: three days, the last one cut.
    assert answer.value.num_rows == OBJECTS * 3
    covered = sum(answer.value.column("covered_s").to_pylist())
    assert covered <= OBJECTS * VERSIONS * STEP.total_seconds()
    assert covered > 0.9 * OBJECTS * VERSIONS * STEP.total_seconds()


def test_series_with_a_step_for_some_objects(plant: Plant):
    uids = [asset(index) for index in range(0, OBJECTS, OBJECTS // 100)]
    answer = _timed(
        plant,
        "series with a step for 100 uids",
        "series",
        {**_whole(plant), "step": "PT15M", "uids": uids},
    )
    assert len(uids) == 100
    assert answer.value.num_rows == 100 * (VERSIONS + 1)


def test_aggregate_with_group_by(plant: Plant):
    answer = _timed(
        plant,
        "aggregate with group_by",
        "aggregate",
        {"property": "Asset.value", "op": "mean", "group_by": "kind"},
    )
    assert answer.value.column("group").to_pylist() == sorted(KINDS)
    assert sum(answer.value.column("count").to_pylist()) > 0.9 * OBJECTS
