"""The commit log, single versions, differences and branches."""

from __future__ import annotations

import io

import pyarrow as pa
import pytest
from api_testkit import (
    BACKFILL_SEQS,
    HEAD_SEQ,
    MATERIAL_SEQ,
    MIGRATION_SEQ,
    SCENARIO,
    VALVE_REMOVED_SEQ,
    Api,
    WaterHistory,
    time_of,
)


def seqs(body: dict) -> list[int]:
    return [version["seq"] for version in body["data"]]


def test_the_whole_log(api: Api, water_history: WaterHistory) -> None:
    body = api.ok("/api/v1/history/versions")
    assert seqs(body) == list(range(HEAD_SEQ + 1))
    assert body["next_cursor"] is None
    assert body["version"]["seq"] == HEAD_SEQ
    first, network = body["data"][0], body["data"][2]
    assert first["cause"] == "install"
    assert network["actor"] == "gis-import" and network["note"] == "network loaded"
    assert network["t"] == "2026-01-05T02:00:00Z"
    assert network["parents"] == [water_history.versions[1].id]
    assert network["written_at"].endswith("Z")


def test_paging_through_the_log(api: Api) -> None:
    found: list[int] = []
    cursor = None
    pages = 0
    while True:
        params: dict = {"limit": 5, "order": "desc"}
        if cursor:
            params["cursor"] = cursor
        body = api.ok("/api/v1/history/versions", params=params)
        found.extend(seqs(body))
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert found == list(range(HEAD_SEQ, -1, -1))
    assert pages == 5


def test_a_cursor_belongs_to_its_query(api: Api) -> None:
    body = api.ok("/api/v1/history/versions", params={"limit": 2})
    response = api.get(
        "/api/v1/history/versions",
        params={"limit": 2, "cause": "sync", "cursor": body["next_cursor"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "api.bad_cursor"


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"cause": "manual"}, [VALVE_REMOVED_SEQ, MATERIAL_SEQ]),
        ({"cause": "migration"}, [MIGRATION_SEQ]),
        ({"cause": "registration"}, [1, MIGRATION_SEQ + 1]),
        ({"actor": "gis-import"}, [2, 18]),
        ({"run": "run-1"}, [4, 5, 6]),
        ({"backfill": "true"}, list(BACKFILL_SEQS)),
        ({"from": "#3", "to": "#6"}, [3, 4, 5, 6]),
        ({"from": "2026-01-05T03:30:00Z", "to": "#6"}, [3, 4, 5, 6]),
        ({"from": "#3", "to": "#6", "order": "desc"}, [6, 5, 4, 3]),
        ({"from": "#10", "cause": "sync", "actor": "scada"}, [11, 12, 13, 14, 19, 20, 21]),
    ],
)
def test_filters(api: Api, params: dict, expected: list[int]) -> None:
    assert seqs(api.ok("/api/v1/history/versions", params=params)) == expected


def test_not_backfilled(api: Api) -> None:
    found = seqs(api.ok("/api/v1/history/versions", params={"backfill": "false"}))
    assert found == [seq for seq in range(HEAD_SEQ + 1) if seq not in BACKFILL_SEQS]


def test_log_of_a_branch(api: Api, water_history: WaterHistory) -> None:
    body = api.ok("/api/v1/history/versions", params={"branch": SCENARIO, "from": "#19"})
    assert seqs(body) == [19, 20, 21]
    last = body["data"][-1]
    assert last["id"] == water_history.scenario.id
    assert last["cause"] == "scenario" and last["branch"] == SCENARIO
    assert body["data"][0]["id"] == water_history.versions[19].id


@pytest.mark.parametrize(
    "selector, seq",
    [
        ("%233", 3),
        ("head", HEAD_SEQ),
        ("2026-01-05T03:30:00Z", 3),
        ("2026-01-05T04:30:00%2B01:00", 3),
    ],
)
def test_one_version(api: Api, selector: str, seq: int) -> None:
    response = api.get(f"/api/v1/history/versions/{selector}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["seq"] == seq
    assert response.headers["x-dta-version-seq"] == str(seq)
    assert body["data"]["t"] == time_of(seq).isoformat().replace("+00:00", "Z")


def test_one_version_by_id_and_branch(api: Api, water_history: WaterHistory) -> None:
    version = water_history.versions[7]
    assert api.ok(f"/api/v1/history/versions/{version.id}")["data"]["run"] == "run-2"
    body = api.ok("/api/v1/history/versions/head", params={"branch": SCENARIO})
    assert body["data"]["id"] == water_history.scenario.id


def test_unknown_versions(api: Api) -> None:
    assert api.get("/api/v1/history/versions/%23999").json()["code"] == "store.unknown_version"
    response = api.get("/api/v1/history/versions/2020-01-01T00:00:00Z")
    assert response.status_code == 404
    response = api.get("/api/v1/history/versions/%232..%235")
    assert response.status_code == 400


def test_diff_summary(api: Api) -> None:
    body = api.ok("/api/v1/history/diff", params={"from": "#9", "to": "#10"})
    assert body["summary"] == [{"type": "Valve", "added": 0, "modified": 0, "removed": 1}]
    assert body["from"]["seq"] == 9 and body["to"]["seq"] == 10
    assert "rows" not in body


def test_diff_rows(api: Api) -> None:
    body = api.ok("/api/v1/history/diff", params={"from": "#9", "to": "#10", "detail": "rows"})
    (row,) = body["rows"]["Valve"]
    assert row["_change"] == "removed" and row["uid"] == "link:V1"
    assert row["_before"]["valve_type"] == "prv"
    assert body["truncated"] == {"Valve": False}


def test_diff_across_the_migration(api: Api) -> None:
    body = api.ok(
        "/api/v1/history/diff",
        params={
            "from": f"#{MIGRATION_SEQ - 1}",
            "to": f"#{MATERIAL_SEQ}",
            "types": "Link",
            "detail": "rows",
            "limit": 2,
        },
    )
    assert body["summary"] == [{"type": "Pipe", "added": 0, "modified": 3, "removed": 0}]
    assert [row["uid"] for row in body["rows"]["Pipe"]] == ["link:P1", "link:P2"]
    assert body["rows"]["Pipe"][0]["material"] == "PVC"
    assert body["rows"]["Pipe"][0]["_before"]["material"] is None
    assert body["truncated"] == {"Pipe": True}


def test_diff_between_branches(api: Api, water_history: WaterHistory) -> None:
    body = api.ok(
        "/api/v1/history/diff",
        params={"from": "#20", "to": "head", "branch": SCENARIO, "types": "Node,Pipe"},
    )
    by_type = {entry["type"]: entry for entry in body["summary"]}
    assert by_type["Junction"] == {"type": "Junction", "added": 1, "modified": 1, "removed": 0}
    assert by_type["Pipe"]["added"] == 1


def test_diff_as_arrow(api: Api) -> None:
    response = api.get(
        "/api/v1/history/diff", params={"from": "#9", "to": "#10", "format": "arrow"}
    )
    assert response.status_code == 200
    table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
    assert table.to_pylist() == [{"type": "Valve", "added": 0, "modified": 0, "removed": 1}]
    response = api.get(
        "/api/v1/history/diff",
        params={"from": "#9", "to": "#10", "format": "arrow", "detail": "rows", "types": "Valve"},
    )
    rows = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
    assert rows.column("_change").to_pylist() == ["removed"]
    assert rows.column("_before").to_pylist()[0]["setting"] == 30.0
    empty = api.get(
        "/api/v1/history/diff",
        params={"from": "#9", "to": "#10", "format": "arrow", "detail": "rows", "types": "Tank"},
    )
    assert pa.ipc.open_stream(io.BytesIO(empty.content)).read_all().num_rows == 0
    refused = api.get(
        "/api/v1/history/diff",
        params={"from": "#9", "to": "#10", "format": "arrow", "detail": "rows"},
    )
    assert refused.status_code == 400 and refused.json()["code"] == "api.bad_request"


def test_branches(api: Api, water_history: WaterHistory) -> None:
    body = api.ok("/api/v1/history/branches")
    heads = {branch["name"]: branch["head"] for branch in body["data"]}
    assert heads["main"]["id"] == water_history.head.id
    assert heads[SCENARIO]["id"] == water_history.scenario.id
    assert heads[SCENARIO]["t"] == "2026-01-05T20:30:00Z"
