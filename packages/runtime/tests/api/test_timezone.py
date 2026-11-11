"""Every time the API hands out is UTC, whatever the time zone of the process."""

from __future__ import annotations

import io
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pyarrow as pa
import pytest
from api_testkit import TOKEN, WaterHistory
from dta_runtime.api import create_app
from dta_runtime.clock import FixedClock
from dta_runtime.settings import RuntimeSettings
from starlette.testclient import TestClient

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ELSEWHERE = "America/Los_Angeles"

JSON_READS = [
    ("/api/v1/meta/health", {}),
    ("/api/v1/history/versions", {"from": "#2", "to": "#6"}),
    ("/api/v1/history/versions/2026-01-05T05:30:00-08:00", {}),
    ("/api/v1/objects/Junction", {"at": "2026-01-05T05:30:00Z", "include": "provenance"}),
    ("/api/v1/objects/Reading", {"include": "provenance"}),
    ("/api/v1/object/sensor:S1", {"include": "provenance"}),
    ("/api/v1/objects/Tank/node:T1/history", {"include": "provenance"}),
    ("/api/v1/block/slice/Tank", {"range": "#2..head"}),
    ("/api/v1/block/slice/Junction", {"range": "#2..head", "mode": "changes"}),
    ("/api/v1/block/slice/Tank", {"range": "#2..head", "layout": "tensor"}),
    ("/api/v1/history/diff", {"from": "#3", "to": "#9", "detail": "rows", "types": "Reading"}),
]
ARROW_READS = [
    ("/api/v1/objects/Reading", {"format": "arrow"}),
    ("/api/v1/block/slice/Tank", {"range": "#2..head", "format": "arrow"}),
    ("/api/v1/block/slice/Reading", {"range": "#2..head", "format": "parquet"}),
    ("/api/v1/objects/Tank/node:T1/history", {"format": "arrow"}),
]


def read_everything(settings: RuntimeSettings, history: WaterHistory) -> dict:
    """What a fresh API answers; the twin is opened anew, so are its connections. Its clock
    stands still, at a time given with an offset of its own."""
    seen: dict = {}
    clock = FixedClock(datetime(2026, 1, 5, 13, 30, tzinfo=timezone(timedelta(hours=-8))))
    with TestClient(create_app(settings, clock=clock)) as client:
        for path, params in JSON_READS:
            response = client.get(path, params=params, headers=HEADERS)
            assert response.status_code == 200, (path, response.text)
            seen[(path, str(params))] = (response.json(), response.headers["x-dta-version-t"])
        for path, params in ARROW_READS:
            response = client.get(path, params=params, headers=HEADERS)
            assert response.status_code == 200, (path, response.text)
            if params["format"] == "arrow":
                table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
            else:
                import pyarrow.parquet as pq

                table = pq.read_table(io.BytesIO(response.content))
            seen[(path, str(params))] = (
                [str(field.type) for field in table.schema],
                table.to_pylist(),
                response.headers["x-dta-version-t"],
            )
    return seen


@pytest.fixture
def elsewhere() -> Iterator[None]:
    before = os.environ.get("TZ")
    os.environ["TZ"] = ELSEWHERE
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_times_do_not_depend_on_the_process_time_zone(
    runtime_settings: RuntimeSettings, water_history: WaterHistory, request: pytest.FixtureRequest
) -> None:
    here = read_everything(runtime_settings, water_history)
    request.getfixturevalue("elsewhere")
    assert time.localtime(datetime(2026, 1, 5, tzinfo=UTC).timestamp()).tm_gmtoff == -8 * 3600
    there = read_everything(runtime_settings, water_history)
    assert there == here

    health, version_t = there[("/api/v1/meta/health", "{}")]
    assert version_t == "2026-01-05T21:00:00Z" == health["head"]["t"]
    assert health["clock"]["now"] == "2026-01-05T21:30:00Z"
    versions, _ = there[("/api/v1/history/versions", str({"from": "#2", "to": "#6"}))]
    assert [v["t"] for v in versions["data"]] == [f"2026-01-05T0{h}:00:00Z" for h in range(2, 7)]
    local_selector, _ = there[("/api/v1/history/versions/2026-01-05T05:30:00-08:00", "{}")]
    assert local_selector["data"]["seq"] == 13
    readings, _ = there[("/api/v1/objects/Reading", str({"include": "provenance"}))]
    first = readings["data"][0]
    assert first["observed_at"] == "2026-01-05T04:00:00Z"
    assert first["_prov"]["*"]["t_valid"] == "2026-01-05T04:00:00Z"
    sensor, _ = there[("/api/v1/object/sensor:S1", str({"include": "provenance"}))]
    assert sensor["data"]["values"]["installed_on"] == "2025-12-01"
    tank, _ = there[("/api/v1/block/slice/Tank", str({"range": "#2..head"}))]
    assert tank["data"][0]["_t"] == "2026-01-05T02:00:00Z"

    types, rows, _ = there[("/api/v1/objects/Reading", str({"format": "arrow"}))]
    assert "timestamp[us, tz=UTC]" in types
    assert rows[0]["observed_at"] == datetime(2026, 1, 5, 4, tzinfo=UTC)
    types, rows, _ = there[
        ("/api/v1/block/slice/Tank", str({"range": "#2..head", "format": "arrow"}))
    ]
    assert types[2] == "timestamp[us, tz=UTC]"
    assert rows[0]["_t"] == datetime(2026, 1, 5, 2, tzinfo=UTC)
