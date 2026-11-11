"""Latency of sorted listings, totals and the uid search on two types of 50,000 objects
each under one abstract class. Run with `-m scale`.

Each figure is the median of `SAMPLES` requests made after one that is not counted; the
largest of them is reported beside it.
"""

from __future__ import annotations

import io
import os
import random
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
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

PER_TYPE = 50_000
LOAD_CHUNK = 20_000
DEEP_OFFSET = 40_000
SAMPLES = 5
T0 = datetime(2026, 5, 4, tzinfo=UTC)
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

THRESHOLDS_MS = {
    "sorted first page, number": 500.0,
    "sorted first page, text": 500.0,
    "sorted page at offset 40,000, number": 1_500.0,
    "sorted page at offset 40,000, text": 1_500.0,
    "abstract sorted page at offset 40,000, number": 3_000.0,
    "abstract sorted page at offset 40,000, text": 3_000.0,
    "count, one type of 50,000": 200.0,
    "count, abstract class of 100,000": 200.0,
    "uid search first page, 10,000 matches": 200.0,
}

ONTOLOGY = """\
id: https://example.org/stock
name: stock
description: Two kinds of stock item, many of each.
prefixes:
  linkml: https://w3id.org/linkml/
  st: https://example.org/stock/
default_prefix: st
default_range: string
imports:
  - linkml:types

classes:
  Item:
    abstract: true
    attributes:
      uid:
        identifier: true
        required: true
      label: {}
      rank:
        range: integer
      weight:
        range: float

  Widget:
    is_a: Item

  Gadget:
    is_a: Item
"""
WORDS = ["alpha", "Bravo", "charlie", "Delta", "écho", "Foxtrot", "golf", "日本", "Ωmega", "zulu"]


@dataclass
class Stock:
    settings: StoreSettings
    twin: Twin
    measured: dict[str, float] = field(default_factory=dict)
    load_s: float = 0.0


def _manifest() -> Any:
    document = {
        "twin": {"id": "stock", "name": "Stock", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/stock.yaml",
            "type_signature": type_signature(parse_ontology(ONTOLOGY).ontology),
        },
    }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="scale manifest")


def _row(rng: random.Random, prefix: str, index: int) -> dict[str, Any]:
    return {
        "uid": f"item:{prefix}{index:05d}",
        "label": None if index % 50 == 0 else f"{rng.choice(WORDS)} {rng.randrange(10_000)}",
        "rank": None if index % 40 == 0 else rng.randrange(1_000),
        "weight": rng.random() * 100.0,
    }


def _load(settings: StoreSettings) -> Stock:
    started = time.perf_counter()
    installed = install_twin(settings, Bundle(manifest=_manifest(), ontology_text=ONTOLOGY), t=T0)
    twin = installed.twin
    rng = random.Random(9)
    rows = [("Widget", _row(rng, "W", index)) for index in range(PER_TYPE)]
    rows += [("Gadget", _row(rng, "G", index)) for index in range(PER_TYPE)]
    for number, begin in enumerate(range(0, len(rows), LOAD_CHUNK), start=1):
        upserts: dict[str, list[dict[str, Any]]] = {}
        for type_name, row in rows[begin : begin + LOAD_CHUNK]:
            upserts.setdefault(type_name, []).append(row)
        t = T0 + timedelta(minutes=number)
        chunk_started = time.perf_counter()
        twin.store.commit(
            ChangeSet(upserts=upserts, provenance=Provenance("stock", FactKind.observed, t)),
            CommitMeta(cause="sync", actor="loader", t=t),
        )
        log.info(
            "scale fixture: loaded a chunk",
            rows=sum(len(part) for part in upserts.values()),
            elapsed_ms=round((time.perf_counter() - chunk_started) * 1000.0, 1),
        )
    return Stock(settings=settings, twin=twin, load_s=time.perf_counter() - started)


@pytest.fixture(scope="module")
def stock(server_settings: StoreSettings, _databases: list[str]) -> Iterator[Stock]:
    name = database_name(server_settings, "stock")
    _databases.append(name)
    loaded = _load(with_database(server_settings, name))
    yield loaded
    loaded.twin.close()
    report = [f"listing scale fixture loaded in {loaded.load_s:.1f} s"]
    for label, value in loaded.measured.items():
        limit = THRESHOLDS_MS.get(label.removesuffix(" (max)"))
        report.append(
            f"{label}: {value:.1f} ms" + (f" (threshold {limit:.0f} ms)" if limit else "")
        )
    one, five, fifteen = os.getloadavg()
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info("scale measurements", **{k: round(v, 1) for k, v in loaded.measured.items()})


@pytest.fixture(scope="module")
def client(stock: Stock, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    folder: Path = tmp_path_factory.mktemp("stock")
    settings = RuntimeSettings(
        store=stock.settings, api_token=TOKEN, artifacts_dir=folder, logs_dir=folder
    )
    with TestClient(create_app(settings, stock.twin)) as opened:
        yield opened


def _get(client: TestClient, path: str, params: dict[str, Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    response = client.get(path, params=params, headers=HEADERS)
    elapsed = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200, (path, params, response.text[:500])
    return elapsed, response


def _measure(stock: Stock, label: str, client: TestClient, path: str, params: dict) -> Any:
    """The response of the last request; the median is checked against the label's limit."""
    _get(client, path, params)
    samples = []
    for _ in range(SAMPLES):
        elapsed, response = _get(client, path, params)
        samples.append(elapsed)
    median = statistics.median(samples)
    stock.measured[label] = median
    stock.measured[f"{label} (max)"] = max(samples)
    if label in THRESHOLDS_MS:
        assert median <= THRESHOLDS_MS[label], f"{label}: {median:.1f} ms"
    return response


def _deep_cursor(client: TestClient, path: str, sort: str) -> str:
    """The cursor of the page that starts at `DEEP_OFFSET` of a sorted listing."""
    _, response = _get(client, path, {"sort": sort, "format": "arrow", "limit": DEEP_OFFSET})
    return response.headers["x-dta-next-cursor"]


def _sorted_values(stock: Stock, type_names: tuple[str, ...], key: str) -> list[tuple]:
    """(value is missing, value as bytes or number, uid) of every row, in the store's order."""
    rows = []
    for type_name in type_names:
        table = stock.twin.store.table(type_name, columns=[key])
        for uid, value in zip(
            table.column("uid").to_pylist(), table.column(key).to_pylist(), strict=True
        ):
            text = value.encode("utf-8") if isinstance(value, str) else value
            rows.append((value is None, text if value is not None else 0, uid))
    return sorted(rows)


@pytest.mark.parametrize("key, kind", [("rank", "number"), ("label", "text")])
def test_a_sorted_type(stock: Stock, client: TestClient, key: str, kind: str) -> None:
    path = "/api/v1/objects/Widget"
    expected = [uid for _, _, uid in _sorted_values(stock, ("Widget",), key)]
    first = _measure(stock, f"sorted first page, {kind}", client, path, {"sort": key})
    assert [row["uid"] for row in first.json()["data"]] == expected[:100]
    cursor = _deep_cursor(client, path, key)
    deep = _measure(
        stock,
        f"sorted page at offset 40,000, {kind}",
        client,
        path,
        {"sort": key, "cursor": cursor},
    )
    assert [row["uid"] for row in deep.json()["data"]] == expected[DEEP_OFFSET : DEEP_OFFSET + 100]


@pytest.mark.parametrize("key, kind", [("rank", "number"), ("label", "text")])
def test_a_sorted_abstract_class(stock: Stock, client: TestClient, key: str, kind: str) -> None:
    path = "/api/v1/objects/Item"
    expected = [uid for _, _, uid in _sorted_values(stock, ("Widget", "Gadget"), key)]
    first = _measure(stock, f"abstract sorted first page, {kind}", client, path, {"sort": key})
    assert [row["uid"] for row in first.json()["data"]] == expected[:100]
    cursor = _deep_cursor(client, path, key)
    deep = _measure(
        stock,
        f"abstract sorted page at offset 40,000, {kind}",
        client,
        path,
        {"sort": key, "cursor": cursor},
    )
    assert [row["uid"] for row in deep.json()["data"]] == expected[DEEP_OFFSET : DEEP_OFFSET + 100]


def test_counts(stock: Stock, client: TestClient) -> None:
    one = _measure(
        stock, "count, one type of 50,000", client, "/api/v1/objects/Widget",
        {"count": "true", "limit": 1},
    )  # fmt: skip
    assert one.json()["total"] == PER_TYPE and one.headers["x-dta-total"] == str(PER_TYPE)
    both = _measure(
        stock, "count, abstract class of 100,000", client, "/api/v1/objects/Item",
        {"count": "true", "limit": 1},
    )  # fmt: skip
    assert both.json()["total"] == 2 * PER_TYPE
    plain = _measure(
        stock, "first page in uid order, abstract class, no count", client,
        "/api/v1/objects/Item", {"limit": 1},
    )  # fmt: skip
    assert "total" not in plain.json()


def test_uid_search(stock: Stock, client: TestClient) -> None:
    path = "/api/v1/objects"
    first = _measure(
        stock, "uid search first page, 10,000 matches", client, path, {"uid_like": "item:W1%"}
    )
    body = first.json()
    assert [row["uid"] for row in body["data"]] == [f"item:W1{n:04d}" for n in range(100)]
    counted = _measure(
        stock,
        "uid search with count, 10,000 matches",
        client,
        path,
        {"uid_like": "item:W1%", "count": "true"},
    )
    assert counted.json()["total"] == 10_000
    everything = _measure(
        stock,
        "uid search with count, every one of 100,000 objects",
        client,
        path,
        {"uid_like": "%", "count": "true"},
    )
    assert everything.json()["total"] == 2 * PER_TYPE
    # The count of a search is a read of every row it matches: measured on the store alone.
    store = stock.twin.store
    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        found = store.find("%", types=["Widget", "Gadget"])
        samples.append((time.perf_counter() - started) * 1000.0)
    assert found.num_rows == 2 * PER_TYPE
    stock.measured["store.find of 100,000 objects, no limit"] = statistics.median(samples)
    arrow = _measure(
        stock, "uid search, 10,000 matches as Arrow", client, path,
        {"uid_like": "item:W1%", "format": "arrow"},
    )  # fmt: skip
    assert pa.ipc.open_stream(io.BytesIO(arrow.content)).read_all().num_rows == 10_000
