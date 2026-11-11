"""The stack's structured log lines, read back through the API."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from api_testkit import TOKEN, WaterHistory
from dta_runtime.api import create_app
from dta_runtime.settings import RuntimeSettings, log_file
from starlette.testclient import TestClient

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def record(minute: int, level: str, context: str, message: str, trace_id: str) -> str:
    return json.dumps(
        {
            "timestamp": f"2026-01-05T10:{minute:02d}:00.000+00:00",
            "level": level,
            "context": context,
            "message": message,
            "trace_id": trace_id,
            "error": None,
            "stack": None,
        }
    )


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "sync.jsonl").write_text(
        "\n".join(
            [
                record(0, "info", "sync.run", "run started", "t1"),
                record(5, "warn", "sync.source", "source slow; retrying", "t1"),
                "this line is not JSON",
                "[1, 2]",
                record(10, "error", "sync.run", "run failed", "t2"),
                json.dumps({"message": "no timestamp"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "worker.jsonl").write_text(
        "\n".join(
            [
                record(2, "debug", "worker.queue", "picked up", "t3"),
                record(7, "info", "worker.queue", "finished", "t3"),
            ]
        ),
        encoding="utf-8",
    )
    (directory / "notes.txt").write_text("not a log file", encoding="utf-8")
    return directory


@pytest.fixture
def client(
    logs: Path, water_history: WaterHistory, runtime_settings: RuntimeSettings
) -> Iterator[TestClient]:
    settings = RuntimeSettings(
        store=water_history.settings,
        api_token=TOKEN,
        artifacts_dir=runtime_settings.artifacts_dir,
        logs_dir=logs,
    )
    with TestClient(create_app(settings, water_history.twin)) as client:
        yield client


def messages(client: TestClient, **params: object) -> tuple[list[tuple[str, str]], int]:
    response = client.get("/api/v1/lifecycle/logs", params=params, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    return [(line["service"], line["message"]) for line in body["data"]], body["skipped"]


def test_lines_of_every_service_newest_first(client: TestClient) -> None:
    found, skipped = messages(client, service="sync,worker")
    assert found == [
        ("sync", "run failed"),
        ("worker", "finished"),
        ("sync", "source slow; retrying"),
        ("worker", "picked up"),
        ("sync", "run started"),
    ]
    assert skipped == 3


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"level": "warn"}, ["run failed", "source slow; retrying"]),
        ({"level": "info"}, ["run failed", "finished", "source slow; retrying", "run started"]),
        ({"since": "2026-01-05T10:04:00Z"}, ["run failed", "finished", "source slow; retrying"]),
        (
            {"since": "2026-01-05T11:04:00+01:00"},
            ["run failed", "finished", "source slow; retrying"],
        ),
        ({"context": "sync.run"}, ["run failed", "run started"]),
        ({"trace_id": "t1"}, ["source slow; retrying", "run started"]),
        ({"limit": 2}, ["run failed", "finished"]),
        ({"service": "worker"}, ["finished", "picked up"]),
        ({"service": ["worker", "nobody"]}, ["finished", "picked up"]),
    ],
)
def test_filters(client: TestClient, params: dict, expected: list[str]) -> None:
    params.setdefault("service", "sync,worker")
    found, _ = messages(client, **params)
    assert [message for _, message in found] == expected


@pytest.mark.parametrize("since", ["2026-01-05T10:04:00", "yesterday"])
def test_since_needs_a_time_with_an_offset(client: TestClient, since: str) -> None:
    response = client.get("/api/v1/lifecycle/logs", params={"since": since}, headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "api.bad_request"


def test_the_api_reads_its_own_lines(
    client: TestClient, logs: Path, log_to: Callable[[Path], Path]
) -> None:
    log_to(log_file(logs, "api"))
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    traced = {"traceparent": f"00-{trace_id}-b7ad6b7169203331-01", **HEADERS}
    assert client.get("/api/v1/objects/Junction", headers=traced).status_code == 200
    found, _ = messages(client, service="api", trace_id=trace_id)
    assert ("api", "request served") in found
    response = client.get(
        "/api/v1/lifecycle/logs", params={"service": "api", "trace_id": trace_id}, headers=HEADERS
    )
    served = next(line for line in response.json()["data"] if line["message"] == "request served")
    assert served["data"]["route"] == "/api/v1/objects/{type}"
    assert served["timestamp"].endswith("+00:00")
    assert TOKEN not in response.text


def test_a_missing_logs_directory_reads_as_empty(
    water_history: WaterHistory, runtime_settings: RuntimeSettings, tmp_path: Path
) -> None:
    settings = RuntimeSettings(
        store=water_history.settings,
        api_token=TOKEN,
        artifacts_dir=runtime_settings.artifacts_dir,
        logs_dir=tmp_path / "missing",
    )
    with TestClient(create_app(settings, water_history.twin)) as client:
        response = client.get("/api/v1/lifecycle/logs", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"data": [], "skipped": 0}
