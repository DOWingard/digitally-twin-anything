"""Objects as of a version: listings, filters, sorting, paging, single objects, links, histories."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from api_testkit import (
    HEAD_SEQ,
    J6_SEQ,
    JUNCTIONS,
    MATERIAL_SEQ,
    SCENARIO,
    TOKEN,
    VALVE_REMOVED_SEQ,
    Api,
    WaterHistory,
    demand,
    level,
    node,
    time_of,
)
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance
from dta_runtime.api import create_app
from dta_runtime.settings import RuntimeSettings
from dta_runtime.twin import Twin
from starlette.testclient import TestClient


def uids(body: dict) -> list[str]:
    return [row["uid"] for row in body["data"]]


def test_junctions_at_head(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction")
    assert uids(body) == [node(key) for key in (*JUNCTIONS, "J6")]
    by_uid = {row["uid"]: row for row in body["data"]}
    for key in JUNCTIONS:
        assert by_uid[node(key)]["demand"] == demand(key, HEAD_SEQ)
    assert by_uid[node("J6")]["demand"] == demand("J6", HEAD_SEQ)
    assert by_uid[node("J2")]["pressure"] is None
    assert list(by_uid[node("J1")]) == [
        "uid", "name", "elevation", "x", "y", "base_demand", "demand", "pressure",
    ]  # fmt: skip
    assert body["version"]["seq"] == HEAD_SEQ


@pytest.mark.parametrize(
    "at",
    ["%235", "2026-01-05T05:30:00Z", "2026-01-05T06:30:00%2B01:00", "2026-01-05T05:00:00Z"],
)
def test_junctions_at_an_earlier_version(api: Api, water_history: WaterHistory, at: str) -> None:
    response = api.get(f"/api/v1/objects/Junction?at={at}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert uids(body) == [node(key) for key in JUNCTIONS]
    assert [row["demand"] for row in body["data"]] == [demand(key, 5) for key in JUNCTIONS]
    assert body["version"]["id"] == water_history.versions[5].id
    assert response.headers["x-dta-version-id"] == water_history.versions[5].id


def test_an_unencoded_plus_in_a_time_is_repaired(api: Api) -> None:
    response = api.get("/api/v1/objects/Junction?at=2026-01-05T06:30:00+01:00")
    assert response.status_code == 200, response.text
    assert response.json()["version"]["seq"] == 5


def test_a_naive_time_is_refused(api: Api) -> None:
    response = api.get("/api/v1/objects/Junction", params={"at": "2026-01-05T05:30:00"})
    assert response.status_code == 400
    assert response.json()["code"].startswith("store.")


def test_a_branch(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction", params={"branch": SCENARIO})
    by_uid = {row["uid"]: row for row in body["data"]}
    assert node("J9") in by_uid
    assert by_uid[node("J1")]["demand"] == 99.0
    assert body["version"]["branch"] == SCENARIO
    assert node("J9") not in uids(api.ok("/api/v1/objects/Junction"))


def test_an_abstract_class_lists_its_members(api: Api) -> None:
    body = api.ok("/api/v1/objects/Node", params={"columns": "elevation", "limit": 100})
    rows = {row["uid"]: row for row in body["data"]}
    assert set(rows) == {node(k) for k in ("R1", *JUNCTIONS, "J6", "T1")}
    assert rows[node("T1")] == {"uid": node("T1"), "_type": "Tank", "elevation": 60.0}
    assert rows[node("R1")]["_type"] == "Reservoir"
    assert uids(body) == sorted(rows)
    every = api.ok("/api/v1/objects/Node")["data"][0]
    assert set(every) == {"uid", "_type", "name", "elevation", "x", "y"}


def test_an_abstract_class_pages_in_uid_order(api: Api) -> None:
    found: list[str] = []
    cursor = None
    while True:
        params: dict = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = api.ok("/api/v1/objects/Node", params=params)
        found.extend(uids(body))
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert found == sorted(node(k) for k in ("R1", *JUNCTIONS, "J6", "T1"))


@pytest.mark.parametrize(
    "type_name, where, expected",
    [
        ("Junction", ["elevation:gt:40"], ["J1", "J2"]),
        ("Junction", ["elevation:ge:40"], ["J1", "J2", "J3"]),
        ("Junction", ["elevation:lt:35"], ["J5", "J6"]),
        ("Junction", ["elevation:le:35", "elevation:gt:26"], ["J4", "J5"]),
        ("Junction", ["name:eq:J3"], ["J3"]),
        ("Junction", ["name:ne:J3", "elevation:ge:40"], ["J1", "J2"]),
        ("Junction", ["name:in:J1,J4,J9"], ["J1", "J4"]),
        ("Junction", ["name:like:J%"], ["J1", "J2", "J3", "J4", "J5", "J6"]),
        ("Junction", ["base_demand:null"], ["J6"]),
        ("Junction", ["base_demand:notnull", "base_demand:gt:3"], ["J4", "J5"]),
        ("Pipe", ["lined:eq:true"], ["P1", "P3", "P5"]),
        ("Pipe", ["material:eq:PVC"], ["P1", "P2"]),
        ("Pipe", ["start_node:eq:node:J2"], ["P2", "P4"]),
        ("Sensor", ["installed_on:eq:2025-12-01"], ["S1"]),
        ("Sensor", ["quantity:in:level,flow"], ["S2"]),
        ("Reading", ["observed_at:ge:2026-01-05T08:00:00Z"], ["S1-8", "S1-9"]),
        ("Reading", ["observed_at:lt:2026-01-05T06:00:00%2B01:00"], ["S1-4"]),
    ],
)
def test_filters(api: Api, type_name: str, where: list[str], expected: list[str]) -> None:
    query = "&".join(f"where={clause}" for clause in where)
    response = api.get(f"/api/v1/objects/{type_name}?{query}")
    assert response.status_code == 200, response.text
    assert [uid.split(":", 1)[1] for uid in uids(response.json())] == expected


@pytest.mark.parametrize(
    "where, code",
    [
        ("elevation:between:1", "store.bad_filter"),
        ("elevation", "store.bad_filter"),
        ("colour:eq:red", "store.unknown_property"),
        ("elevation:null:3", "store.bad_filter"),
        ("lined:eq:maybe", "store.bad_filter"),
    ],
)
def test_bad_filters(api: Api, where: str, code: str) -> None:
    type_name = "Pipe" if where.startswith("lined") else "Junction"
    response = api.get(f"/api/v1/objects/{type_name}", params={"where": where})
    assert response.status_code in (400, 404)
    assert response.json()["code"] == code


def test_sorting(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction", params={"sort": "-elevation", "columns": "name"})
    assert uids(body) == [node(k) for k in ("J1", "J2", "J3", "J4", "J5", "J6")]
    assert list(body["data"][0]) == ["uid", "name"]
    body = api.ok("/api/v1/objects/Junction", params={"sort": "base_demand"})
    assert uids(body)[-1] == node("J6")  # nulls last
    first = api.ok("/api/v1/objects/Junction", params={"sort": "elevation", "limit": 4})
    assert uids(first) == [node(k) for k in ("J6", "J5", "J4", "J3")]
    rest = api.ok(
        "/api/v1/objects/Junction",
        params={"sort": "elevation", "limit": 4, "cursor": first["next_cursor"]},
    )
    assert uids(rest) == [node("J2"), node("J1")]
    assert rest["next_cursor"] is None


def test_provenance_of_a_listing(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction", params={"include": "provenance", "columns": "demand"})
    prov = body["data"][0]["_prov"]
    assert prov["*"] == {
        "source": "gis",
        "kind": "observed",
        "confidence": 1.0,
        "t_valid": "2026-01-05T02:00:00Z",
    }
    assert prov["demand"]["source"] == "scada"


def test_a_cursor_keeps_its_version(api: Api, water_history: WaterHistory) -> None:
    first = api.ok("/api/v1/objects/Junction", params={"at": f"#{J6_SEQ}", "limit": 4})
    second = api.get(
        "/api/v1/objects/Junction", params={"at": "#5", "limit": 4, "cursor": first["next_cursor"]}
    )
    assert uids(second.json()) == [node("J5"), node("J6")]
    assert second.headers["x-dta-version-seq"] == str(J6_SEQ)


def test_a_cursor_ignores_versions_written_after_the_first_page(
    make_twin: Callable[..., Twin], runtime_settings: RuntimeSettings
) -> None:
    twin = make_twin()
    store = twin.store
    t = store.head().t + timedelta(hours=1)

    def junctions(keys: range, seq: int) -> ChangeSet:
        return ChangeSet(
            upserts={
                "Junction": [
                    {"uid": f"node:N{key:02d}", "name": f"N{key:02d}", "elevation": float(seq)}
                    for key in keys
                ]
            },
            provenance=Provenance("gis", FactKind.observed, t),
        )

    store.commit(junctions(range(10), 1), CommitMeta(cause="sync", actor="t", t=t))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(runtime_settings, twin)) as client:
        first = client.get("/api/v1/objects/Junction?limit=6", headers=headers).json()
        store.commit(junctions(range(20), 2), CommitMeta(cause="sync", actor="t", t=t))
        rest = client.get(
            f"/api/v1/objects/Junction?limit=100&cursor={first['next_cursor']}", headers=headers
        ).json()
        fresh = client.get("/api/v1/objects/Junction?limit=100", headers=headers).json()
    assert uids(rest) == [f"node:N{key:02d}" for key in range(6, 10)]
    assert {row["elevation"] for row in rest["data"]} == {1.0}
    assert rest["version"]["seq"] == first["version"]["seq"]
    assert len(fresh["data"]) == 20 and fresh["version"]["seq"] == first["version"]["seq"] + 1


def test_sort_too_large_is_refused(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from dta_runtime.api import objects

    monkeypatch.setattr(objects, "SORT_MAX_ROWS", 3)
    response = api.get("/api/v1/objects/Junction", params={"sort": "elevation"})
    assert response.status_code == 400
    assert response.json()["code"] == "api.sort_too_large"


def test_json_limit(api: Api) -> None:
    response = api.get("/api/v1/objects/Junction", params={"limit": 10_001})
    assert response.status_code == 400
    assert response.json()["code"] == "api.bad_request"


def test_one_object(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction/node:J2", params={"at": "#4"})
    assert body["data"]["uid"] == "node:J2" and body["data"]["type"] == "Junction"
    assert body["data"]["values"]["demand"] == demand("J2", 4)
    assert "provenance" not in body["data"]
    same = api.ok("/api/v1/objects/Node/node:J2", params={"at": "#4"})
    assert same["data"] == body["data"]
    detailed = api.ok("/api/v1/objects/Tank/node:T1", params={"include": "provenance"})
    assert detailed["data"]["values"]["level"] == level(HEAD_SEQ)
    assert detailed["data"]["values"]["volume_curve"] == [0.0, 10.0, 25.0]
    assert detailed["data"]["provenance"]["level"]["t_valid"] == "2026-01-05T21:00:00Z"


def test_structured_values(api: Api) -> None:
    body = api.ok("/api/v1/objects/Pump/link:PU1")
    assert body["data"]["values"]["curve"] == [
        {"flow": 0.0, "head": 50.0},
        {"flow": 10.0, "head": 40.0},
    ]
    sensor = api.ok("/api/v1/object/sensor:S1")["data"]["values"]
    assert sensor["installed_on"] == "2025-12-01"
    assert sensor["tags"] == ["north", "critical"]
    reading = api.ok("/api/v1/object/reading:S1-5")["data"]["values"]
    assert reading["observed_at"] == "2026-01-05T05:00:00Z"


@pytest.mark.parametrize(
    "path, params",
    [
        ("/api/v1/objects/Pipe/node:J2", {}),
        ("/api/v1/objects/Junction/node:J6", {"at": f"#{J6_SEQ - 1}"}),
        ("/api/v1/objects/Junction/node:J9", {}),
        ("/api/v1/object/link:V1", {}),
    ],
)
def test_objects_that_are_not_there(api: Api, path: str, params: dict) -> None:
    response = api.get(path, params=params)
    assert response.status_code == 404
    assert response.json()["code"] == "api.unknown_object"


def test_any_object(api: Api) -> None:
    body = api.ok("/api/v1/object/link:V1", params={"at": f"#{VALVE_REMOVED_SEQ - 1}"})
    assert body["data"]["type"] == "Valve"
    assert api.ok("/api/v1/object/node:J9", params={"branch": SCENARIO})["data"]["type"] == (
        "Junction"
    )


def test_links(api: Api) -> None:
    body = api.ok("/api/v1/objects/Junction/node:J2/links")
    found = {(row["direction"], row["edge_type"], row["neighbor"]) for row in body["data"]}
    assert found == {
        ("in", "Pipe", "node:J1"),
        ("in", "Sensor.measures", "sensor:S1"),
        ("out", "Pipe", "node:J3"),
        ("out", "Pipe", "node:T1"),
    }
    sensor = next(row for row in body["data"] if row["edge_type"] == "Sensor.measures")
    assert sensor["link_uid"] is None and sensor["neighbor_type"] == "Sensor"
    out = api.ok("/api/v1/objects/Junction/node:J2/links", params={"direction": "out"})
    assert {row["link_uid"] for row in out["data"]} == {"link:P2", "link:P4"}
    pipes = api.ok("/api/v1/objects/Node/node:J2/links", params={"edge_types": "Pipe"})
    assert len(pipes["data"]) == 3
    early = api.ok("/api/v1/objects/Junction/node:J2/links", params={"at": "#2"})
    assert "Sensor.measures" not in {row["edge_type"] for row in early["data"]}


def test_links_through_a_removed_valve(api: Api) -> None:
    before = api.ok(
        "/api/v1/objects/Junction/node:J1/links", params={"at": f"#{VALVE_REMOVED_SEQ - 1}"}
    )
    assert "link:V1" in {row["link_uid"] for row in before["data"]}
    after = api.ok("/api/v1/objects/Junction/node:J1/links")
    assert "link:V1" not in {row["link_uid"] for row in after["data"]}


def test_links_page(api: Api) -> None:
    first = api.ok("/api/v1/objects/Junction/node:J2/links", params={"limit": 3})
    rest = api.ok(
        "/api/v1/objects/Junction/node:J2/links",
        params={"limit": 3, "cursor": first["next_cursor"]},
    )
    assert len(first["data"]) == 3 and len(rest["data"]) == 1
    assert rest["next_cursor"] is None
    bad = api.get("/api/v1/objects/Pipe/node:J2/links")
    assert bad.status_code == 404


def test_history_of_an_object(api: Api) -> None:
    body = api.ok("/api/v1/objects/Tank/node:T1/history", params={"properties": "level"})
    changed = [2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 19, 20, 21]
    assert [row["_seq"] for row in body["data"]] == changed
    assert [row["level"] for row in body["data"]] == [level(seq) for seq in changed]
    assert body["data"][0]["_change"] == "added"
    assert body["data"][1]["_t"] == time_of(4).isoformat().replace("+00:00", "Z")
    assert set(body["data"][0]) == {"_version", "_seq", "_t", "_change", "uid", "level"}
    static = api.ok("/api/v1/objects/Tank/node:T1/history", params={"properties": "elevation"})
    assert [row["_seq"] for row in static["data"]] == [2]


def test_history_over_a_range_with_a_step(api: Api) -> None:
    body = api.ok("/api/v1/objects/Node/node:T1/history", params={"range": "#4..#9/2"})
    assert [row["_seq"] for row in body["data"]] == [4, 6, 8]


def test_history_of_a_removed_object(api: Api) -> None:
    body = api.ok("/api/v1/objects/Valve/link:V1/history")
    assert [(row["_seq"], row["_change"]) for row in body["data"]] == [
        (2, "added"),
        (VALVE_REMOVED_SEQ, "removed"),
    ]


def test_history_across_the_migration(api: Api) -> None:
    body = api.ok(
        "/api/v1/objects/Pipe/link:P1/history",
        params={"properties": "material", "include": "provenance"},
    )
    assert [(row["_seq"], row["material"]) for row in body["data"]] == [
        (2, None),
        (MATERIAL_SEQ, "PVC"),
    ]
    assert body["data"][-1]["_prov"]["material"]["kind"] == "assumed"


def test_history_pages_keep_their_range(api: Api) -> None:
    first = api.ok("/api/v1/objects/Tank/node:T1/history", params={"limit": 5})
    rest = api.ok(
        "/api/v1/objects/Tank/node:T1/history",
        params={"limit": 100, "cursor": first["next_cursor"], "range": "#0..#3"},
    )
    found = [row["_seq"] for row in first["data"] + rest["data"]]
    assert found == [2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 19, 20, 21]


def test_history_of_nothing(api: Api) -> None:
    response = api.get("/api/v1/objects/Junction/node:J42/history")
    assert response.status_code == 404
    assert response.json()["code"] == "api.unknown_object"
    response = api.get("/api/v1/objects/Pipe/node:J1/history")
    assert response.status_code == 404
    response = api.get("/api/v1/objects/Junction/node:J1/history", params={"properties": "x,zz"})
    assert response.json()["code"] == "store.unknown_property"
