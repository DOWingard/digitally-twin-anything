"""Listings: sorted by any property whatever their size, with their total, and the search for
objects of any type by uid.

The sorting tests run on a catalogue of two kinds of item under one abstract class, with
properties of every kind that can be sorted by, text in mixed case and outside ASCII, ties
and missing values; the expected orders are computed here from the values written.
"""

from __future__ import annotations

import io
import random
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from api_testkit import (
    JUNCTIONS,
    MIGRATION_SEQ,
    TOKEN,
    VALVE_REMOVED_SEQ,
    Api,
    WaterHistory,
    database_name,
    node,
    with_database,
)
from dta_core.manifest import parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.api import create_app
from dta_runtime.api.objects import read_objects, shape_of, store_order
from dta_runtime.settings import RuntimeSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from starlette.testclient import TestClient

CATALOGUE = """\
id: https://example.org/catalogue
name: catalogue
description: Items of two kinds, with properties of every kind to sort them by.
prefixes:
  linkml: https://w3id.org/linkml/
  ca: https://example.org/catalogue/
default_prefix: ca
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
      note:
        annotations:
          dta_long: true
      grade:
        range: Grade
      rank:
        range: integer
      weight:
        range: float
      seen_at:
        range: datetime
      made_on:
        range: date
      active:
        range: boolean
      tags:
        multivalued: true

  Widget:
    is_a: Item

  Gadget:
    is_a: Item
    attributes:
      volts:
        range: float

enums:
  Grade:
    description: Declared in an order that is not the order of the text.
    permissible_values:
      zeta: {}
      beta: {}
      Alpha: {}
      delta: {}
      Gamma: {}
"""

T0 = datetime(2026, 4, 6, tzinfo=UTC)
TEXT = [
    "apple", "Apple", "APPLE", "éclair", "Eclair", "eclair", "zebra", "Zebra", "日本", "日本語",
    "😀 grin", "", "a b", "ab", "a", "a ", "ñu", "Ñu", "Ω", "~", None, None,
]  # fmt: skip
POOLS: dict[str, list[Any]] = {
    "label": TEXT,
    "note": TEXT,
    "grade": ["zeta", "beta", "Alpha", "delta", "Gamma", None],
    "rank": [-3, 0, 0, 2, 2, 7, 10, 2**40, None],
    "weight": [-1.5, -0.001, 0.0, 0.25, 3.0, 1e6, None],
    "seen_at": [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 0, 500_000, tzinfo=UTC),
        datetime(2025, 12, 31, 23, tzinfo=timezone(timedelta(hours=-2))),
        datetime(2026, 6, 1, 12, tzinfo=timezone(timedelta(hours=5))),
        None,
    ],
    "made_on": [date(2026, 1, 1), date(2025, 2, 28), date(1999, 12, 31), None],
    "active": [True, False, None],
}
SORTABLE = list(POOLS)
ITEMS = 70


def catalogue_manifest() -> Any:
    document = {
        "twin": {"id": "catalogue", "name": "Catalogue", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/catalogue.yaml",
            "type_signature": type_signature(parse_ontology(CATALOGUE).ontology),
        },
    }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


class Catalogue:
    def __init__(self, twin: Twin, version: VersionRef, rows: dict[str, dict[str, Any]]) -> None:
        self.twin = twin
        self.version = version
        # uid -> values written, with `_type`
        self.rows = rows

    def expected(self, type_name: str, key: str, descending: bool) -> list[str]:
        """uids in the store's order: by the key, missing values last, ties by uid."""
        members = {"Item": ("Widget", "Gadget")}.get(type_name, (type_name,))
        rows = sorted(
            (uid for uid, row in self.rows.items() if row["_type"] in members),
        )
        if key == "uid":
            return sorted(rows, reverse=descending)
        present = [uid for uid in rows if self.rows[uid][key] is not None]
        missing = [uid for uid in rows if self.rows[uid][key] is None]

        def value(uid: str) -> Any:
            found = self.rows[uid][key]
            return found.encode("utf-8") if isinstance(found, str) else found

        # A stable sort keeps the uid order among equal values, whichever the direction.
        present.sort(key=value, reverse=descending)
        return present + missing


@pytest.fixture(scope="module")
def catalogue(server_settings: StoreSettings, _databases: list[str]) -> Iterator[Catalogue]:
    name = database_name(server_settings, "catalogue")
    _databases.append(name)
    installed = install_twin(
        with_database(server_settings, name),
        Bundle(manifest=catalogue_manifest(), ontology_text=CATALOGUE),
        t=T0,
    )
    twin = installed.twin
    rng = random.Random(3)
    rows: dict[str, dict[str, Any]] = {}
    upserts: dict[str, list[dict[str, Any]]] = {"Widget": [], "Gadget": []}
    for index in range(ITEMS):
        kind = rng.choice(("Widget", "Gadget"))
        row: dict[str, Any] = {"uid": f"item:K{index:03d}"}
        row.update({prop: rng.choice(pool) for prop, pool in POOLS.items()})
        row["tags"] = ["x", "y"][: rng.randrange(3)]
        if kind == "Gadget":
            row["volts"] = float(rng.randrange(5))
        upserts[kind].append(row)
        rows[row["uid"]] = {**row, "_type": kind}
    version = twin.store.commit(
        ChangeSet(upserts=upserts, provenance=Provenance("lab", FactKind.observed, T0)),
        CommitMeta(cause="sync", actor="loader", t=T0),
    )
    yield Catalogue(twin, version, rows)
    twin.close()


@pytest.fixture(scope="module")
def shelf(catalogue: Catalogue, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    """The API of the catalogue."""
    folder = tmp_path_factory.mktemp("catalogue")
    settings = RuntimeSettings(
        store=catalogue.twin.store.settings,
        api_token=TOKEN,
        artifacts_dir=folder,
        logs_dir=folder,
    )
    with TestClient(create_app(settings, catalogue.twin), raise_server_exceptions=False) as client:
        yield Api(client, {"Authorization": f"Bearer {TOKEN}"})


def uids(body: dict) -> list[str]:
    return [row["uid"] for row in body["data"]]


def page_through(api: Api, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        body = api.ok(path, params={**params, **({"cursor": cursor} if cursor else {})})
        rows.extend(body["data"])
        cursor = body["next_cursor"]
        if not cursor:
            return rows


def arrow_of(response: Any) -> pa.Table:
    return pa.ipc.open_stream(io.BytesIO(response.content)).read_all()


# -- sorting -------------------------------------------------------------------


def test_the_catalogue_has_what_the_sort_tests_need(catalogue: Catalogue) -> None:
    kinds = {row["_type"] for row in catalogue.rows.values()}
    assert kinds == {"Widget", "Gadget"}
    for prop in SORTABLE:
        values = [row[prop] for row in catalogue.rows.values()]
        assert None in values, prop
        present = [value for value in values if value is not None]
        assert len(present) > len(set(present)), f"{prop} has no ties"
    labels = {row["label"] for row in catalogue.rows.values()}
    assert {"Apple", "apple", "éclair", "日本"} & labels and any(
        label and not label.isascii() for label in labels
    )


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("key", SORTABLE)
@pytest.mark.parametrize("type_name", ["Widget", "Gadget"])
def test_the_order_made_in_memory_is_the_stores(
    catalogue: Catalogue, type_name: str, key: str, descending: bool
) -> None:
    store = catalogue.twin.store
    ordered = store.table(
        type_name,
        columns=[key],
        at=catalogue.version,
        order_by=[(key, "desc" if descending else "asc")],
    )
    from_store = ordered.column("uid").to_pylist()
    shuffled = list(range(ordered.num_rows))
    random.Random(len(key)).shuffle(shuffled)
    in_memory = store_order(ordered.take(pa.array(shuffled)), key, descending)
    assert in_memory.column("uid").to_pylist() == from_store
    assert from_store == catalogue.expected(type_name, key, descending)


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("key", [*SORTABLE, "uid"])
@pytest.mark.parametrize("type_name", ["Widget", "Item"])
def test_sorted_listings_page_to_the_end(
    shelf: Api, catalogue: Catalogue, type_name: str, key: str, descending: bool
) -> None:
    sort = f"-{key}" if descending else key
    path = f"/api/v1/objects/{type_name}"
    rows = page_through(shelf, path, {"sort": sort, "limit": 7, "columns": "label"})
    found = [row["uid"] for row in rows]
    assert len(found) == len(set(found))
    assert found == catalogue.expected(type_name, key, descending)
    if type_name == "Item":
        assert {row["_type"] for row in rows} == {"Widget", "Gadget"}
        assert all(row["_type"] == catalogue.rows[row["uid"]]["_type"] for row in rows)
    whole = shelf.get(path, params={"sort": sort, "format": "arrow"})
    assert arrow_of(whole).column("uid").to_pylist() == found


def test_a_sorted_page_can_start_anywhere(shelf: Api, catalogue: Catalogue) -> None:
    expected = catalogue.expected("Item", "weight", True)
    first = shelf.ok("/api/v1/objects/Item", params={"sort": "-weight", "limit": 30})
    deep = shelf.get(
        "/api/v1/objects/Item",
        params={"sort": "-weight", "limit": 30, "cursor": first["next_cursor"], "format": "arrow"},
    )
    assert arrow_of(deep).column("uid").to_pylist() == expected[30:60]
    rest = shelf.get(
        "/api/v1/objects/Item",
        params={"sort": "-weight", "cursor": first["next_cursor"], "format": "parquet"},
    )
    assert pq.read_table(io.BytesIO(rest.content)).column("uid").to_pylist() == expected[30:]


def test_a_sort_with_filters_and_a_total(shelf: Api, catalogue: Catalogue) -> None:
    wanted = [uid for uid, row in catalogue.rows.items() if row["rank"] is not None]
    params = {"sort": "-made_on", "where": "rank:notnull", "limit": 6, "count": "true"}
    first = shelf.ok("/api/v1/objects/Item", params=params)
    assert first["total"] == len(wanted)
    rows = page_through(shelf, "/api/v1/objects/Item", params)
    expected = [uid for uid in catalogue.expected("Item", "made_on", True) if uid in wanted]
    assert [row["uid"] for row in rows] == expected
    # The key read only to sort by is not returned.
    assert set(rows[0]) == {"uid", "_type", *SORTABLE, "tags"}
    narrow = shelf.ok("/api/v1/objects/Item", params={"sort": "rank", "columns": "label"})
    assert set(narrow["data"][0]) == {"uid", "_type", "label"}


@pytest.mark.parametrize(
    "type_name, sort, status, code",
    [
        ("Item", "tags", 400, "api.bad_request"),
        ("Widget", "-tags", 400, "api.bad_request"),
        ("Item", "volts", 404, "store.unknown_property"),
        ("Gadget", "colour", 404, "store.unknown_property"),
    ],
)
def test_what_cannot_be_sorted_by(
    shelf: Api, type_name: str, sort: str, status: int, code: str
) -> None:
    response = shelf.get(f"/api/v1/objects/{type_name}", params={"sort": sort})
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_structured_values_of_the_water_network_cannot_be_sorted_by(api: Api) -> None:
    response = api.get("/api/v1/objects/Tank", params={"sort": "volume_curve"})
    assert response.status_code == 400
    assert response.json()["code"] == "api.bad_request"


def test_a_property_that_did_not_exist_at_an_older_version(
    api: Api, water_history: WaterHistory
) -> None:
    body = api.ok("/api/v1/objects/Pipe", params={"sort": "material", "columns": "material"})
    assert [(row["uid"], row["material"]) for row in body["data"]] == [
        ("link:P1", "PVC"),
        ("link:P2", "PVC"),
        ("link:P3", "iron"),
        ("link:P4", None),
        ("link:P5", None),
        ("link:P6", None),
        ("link:P7", None),
    ]
    # The API reads a version under its own ontology, which had no material yet.
    older = api.get("/api/v1/objects/Pipe", params={"sort": "material", "at": "#9"})
    assert older.status_code == 404 and older.json()["code"] == "store.unknown_property"
    # Read under the newer ontology, the older version has no value to order by.
    store = water_history.twin.store
    ontology = store.ontology_at(water_history.head)
    at = water_history.versions[MIGRATION_SEQ - 1]
    for sort in ("material", "-material"):
        listing = read_objects(
            store,
            ontology,
            at,
            shape_of(ontology, "Pipe"),
            columns=["material"],
            filters=[],
            prov=False,
            limit=4,
            position=None,
            sort=sort,
        )
        assert listing.table.column("uid").to_pylist() == [f"link:P{n}" for n in range(1, 5)]
        assert listing.table.column("material").to_pylist() == [None] * 4
        assert listing.next_position == 4


def test_uid_descending(api: Api) -> None:
    rows = page_through(api, "/api/v1/objects/Node", {"sort": "-uid", "limit": 3})
    expected = sorted([node(k) for k in ("R1", *JUNCTIONS, "J6", "T1")], reverse=True)
    assert [row["uid"] for row in rows] == expected


# -- totals --------------------------------------------------------------------


@pytest.fixture
def count_calls(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """The calls the API makes to its store's `count`, which still answers them."""
    store = app_client.app.state.services.store  # type: ignore[attr-defined]
    real = store.count
    calls: list[tuple] = []

    def counting(*args: Any, **kwargs: Any) -> int:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "count", counting)
    return calls


def test_no_count_unless_asked(api: Api, count_calls: list[tuple]) -> None:
    for params in ({}, {"where": "elevation:gt:30"}, {"sort": "elevation", "limit": 2}):
        response = api.get("/api/v1/objects/Junction", params=params)
        assert response.status_code == 200
        assert "total" not in response.json()
        assert "x-dta-total" not in response.headers
        arrow = api.get("/api/v1/objects/Node", params={**params, "format": "arrow"})
        assert arrow.status_code == 200 and "x-dta-total" not in arrow.headers
    assert count_calls == []


def test_the_total_of_a_listing(api: Api, count_calls: list[tuple]) -> None:
    params = {"where": "elevation:gt:30", "limit": 2, "count": "true"}
    first = api.get("/api/v1/objects/Junction", params=params)
    body = first.json()
    assert len(body["data"]) == 2 and body["total"] == 4
    assert first.headers["x-dta-total"] == "4"
    assert len(count_calls) == 1
    # On a later page, from the cursor's version, whatever `at` says.
    later = api.get(
        "/api/v1/objects/Junction",
        params={**params, "cursor": body["next_cursor"], "at": "#5"},
    )
    assert later.json()["total"] == 4 and uids(later.json()) == [node("J3"), node("J4")]
    # `count` is not part of the query: a cursor serves with and without it.
    plain = api.ok(
        "/api/v1/objects/Junction",
        params={"where": "elevation:gt:30", "limit": 2, "cursor": body["next_cursor"]},
    )
    assert "total" not in plain
    nodes = api.get("/api/v1/objects/Node", params={"count": "true", "format": "arrow"})
    assert nodes.headers["x-dta-total"] == "8" and arrow_of(nodes).num_rows == 8
    assert len(count_calls) == 2 + 3  # one per concrete type of Node
    early = api.ok("/api/v1/objects/Junction", params={"count": "true", "at": "#5"})
    assert early["total"] == len(JUNCTIONS)
    sorted_page = api.ok(
        "/api/v1/objects/Node", params={"count": "true", "sort": "-elevation", "limit": 3}
    )
    assert sorted_page["total"] == 8 and len(sorted_page["data"]) == 3


# -- search by uid -------------------------------------------------------------


def found(body: dict) -> list[tuple[str, str]]:
    return [(row["uid"], row["type"]) for row in body["data"]]


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("node:J%", [(node(k), "Junction") for k in (*JUNCTIONS, "J6")]),
        ("%:P_", [(f"link:P{n}", "Pipe") for n in range(1, 8)]),
        ("node:%1", [("node:J1", "Junction"), ("node:R1", "Reservoir"), ("node:T1", "Tank")]),
        ("sensor:S2", [("sensor:S2", "Sensor")]),
        ("reading:S1-_", [(f"reading:S1-{n}", "Reading") for n in range(4, 10)]),
        ("nothing%", []),
    ],
)
def test_find_by_pattern(api: Api, pattern: str, expected: list[tuple[str, str]]) -> None:
    body = api.ok("/api/v1/objects", params={"uid_like": pattern})
    assert found(body) == expected
    assert body["next_cursor"] is None and body["version"]["seq"] == 21
    assert "total" not in body


def test_find_within_types(api: Api) -> None:
    every = {"uid_like": "%"}
    junctions = api.ok("/api/v1/objects", params={**every, "types": "Junction"})
    assert {row["type"] for row in junctions["data"]} == {"Junction"} and len(
        junctions["data"]
    ) == 6
    nodes = api.ok("/api/v1/objects", params={**every, "types": "Node"})
    assert found(nodes) == sorted(
        [(node("R1"), "Reservoir"), (node("T1"), "Tank")]
        + [(node(k), "Junction") for k in (*JUNCTIONS, "J6")]
    )
    mixed = api.ok("/api/v1/objects", params={**every, "types": ["Link", "Sensor"]})
    assert {row["type"] for row in mixed["data"]} == {"Pipe", "Pump", "Sensor"}
    same = api.ok("/api/v1/objects", params={**every, "types": "Link,Sensor"})
    assert same["data"] == mixed["data"]
    unknown = api.get("/api/v1/objects", params={**every, "types": "Node,Hose"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "store.unknown_type"


def test_the_twins_own_records_are_left_out_unless_asked(api: Api) -> None:
    domain = api.ok("/api/v1/objects", params={"uid_like": "%", "limit": 1000})
    assert "twin_manifest:current" not in {row["uid"] for row in domain["data"]}
    assert not any(row["type"].startswith("Twin") for row in domain["data"])
    every = api.ok(
        "/api/v1/objects", params={"uid_like": "%", "limit": 1000, "include_meta": "true"}
    )
    assert ("twin_manifest:current", "TwinManifest") in found(every)
    assert len(every["data"]) > len(domain["data"])
    named = api.ok("/api/v1/objects", params={"uid_like": "twin%", "types": "TwinManifest"})
    assert found(named) == [("twin_manifest:current", "TwinManifest")]


def test_find_pages_to_the_end(api: Api) -> None:
    whole = api.ok("/api/v1/objects", params={"uid_like": "%", "limit": 1000})["data"]
    assert len(whole) > 20 and [row["uid"] for row in whole] == sorted(r["uid"] for r in whole)
    rows = page_through(api, "/api/v1/objects", {"uid_like": "%", "limit": 4})
    assert rows == whole
    first = api.ok("/api/v1/objects", params={"uid_like": "%", "limit": 4})
    other = api.get(
        "/api/v1/objects", params={"uid_like": "node:%", "limit": 4, "cursor": first["next_cursor"]}
    )
    assert other.status_code == 400 and other.json()["code"] == "api.bad_cursor"


def test_find_at_a_past_version_and_on_a_branch(api: Api) -> None:
    before = api.ok("/api/v1/objects", params={"uid_like": "link:V%", "at": "#9"})
    assert found(before) == [("link:V1", "Valve")] and before["version"]["seq"] == 9
    assert api.ok("/api/v1/objects", params={"uid_like": "link:V%"})["data"] == []
    early = api.ok("/api/v1/objects", params={"uid_like": "node:J%", "at": f"#{VALVE_REMOVED_SEQ}"})
    assert len(early["data"]) == len(JUNCTIONS)
    branch = api.ok("/api/v1/objects", params={"uid_like": "node:J9", "branch": "scenario"})
    assert found(branch) == [("node:J9", "Junction")]


def test_find_as_arrow_and_parquet(api: Api) -> None:
    whole = api.ok("/api/v1/objects", params={"uid_like": "%", "limit": 1000})["data"]
    arrow = api.get("/api/v1/objects", params={"uid_like": "%", "format": "arrow"})
    table = arrow_of(arrow)
    assert table.column_names == ["uid", "type"] and table.to_pylist() == whole
    assert "x-dta-next-cursor" not in arrow.headers
    parquet = api.get(
        "/api/v1/objects",
        params={"uid_like": "%", "limit": 5},
        headers={"Accept": "application/vnd.apache.parquet"},
    )
    page = pq.read_table(io.BytesIO(parquet.content))
    assert page.to_pylist() == whole[:5] and "x-dta-next-cursor" in parquet.headers
    rest = api.get(
        "/api/v1/objects",
        params={"uid_like": "%", "cursor": parquet.headers["x-dta-next-cursor"], "format": "arrow"},
    )
    assert arrow_of(rest).to_pylist() == whole[5:]


def test_the_total_of_a_search(api: Api) -> None:
    every = api.ok("/api/v1/objects", params={"uid_like": "%", "limit": 1000})["data"]
    first = api.get("/api/v1/objects", params={"uid_like": "%", "limit": 4, "count": "true"})
    body = first.json()
    assert body["total"] == len(every) and first.headers["x-dta-total"] == str(len(every))
    last = api.ok(
        "/api/v1/objects",
        params={"uid_like": "%", "limit": 1000, "cursor": body["next_cursor"], "count": "true"},
    )
    assert last["total"] == len(every) and len(last["data"]) == len(every) - 4
    arrow = api.get(
        "/api/v1/objects", params={"uid_like": "node:J%", "count": "true", "format": "arrow"}
    )
    assert arrow.headers["x-dta-total"] == "6" and arrow_of(arrow).num_rows == 6
    none = api.ok("/api/v1/objects", params={"uid_like": "nothing%", "count": "true"})
    assert none["total"] == 0 and none["data"] == []
    plain = api.get("/api/v1/objects", params={"uid_like": "%", "limit": 4})
    assert "total" not in plain.json() and "x-dta-total" not in plain.headers


@pytest.mark.parametrize(
    "params",
    [{}, {"uid_like": ""}, {"uid_like": "x" * 201}, {"uid_like": "%", "limit": 0}],
)
def test_bad_searches(api: Api, params: dict) -> None:
    response = api.get("/api/v1/objects", params=params)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "api.bad_request"


def test_the_document_describes_the_new_parameters(api: Api) -> None:
    document = api.ok("/api/v1/openapi.json")
    search = document["paths"]["/api/v1/objects"]["get"]
    names = {p["name"]: p for p in search["parameters"]}
    assert {"uid_like", "types", "include_meta", "at", "branch", "limit", "cursor", "format",
            "count"} <= set(names)  # fmt: skip
    assert names["uid_like"]["required"] is True
    assert names["uid_like"]["schema"]["maxLength"] == 200
    assert search["tags"] == ["objects"]
    assert "x-dta-total" in search["responses"]["200"]["headers"]
    schema = search["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/FoundPage")
    listing = document["paths"]["/api/v1/objects/{type}"]["get"]
    assert "count" in {p["name"] for p in listing["parameters"]}
    assert "x-dta-total" in listing["responses"]["200"]["headers"]
    sort = next(p for p in listing["parameters"] if p["name"] == "sort")
    assert "api.sort_too_large" in sort["description"]
