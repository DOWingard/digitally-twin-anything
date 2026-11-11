"""Credentials, trace context, problem documents and the request log."""

from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path

import pytest
from api_testkit import TOKEN, Api, WaterHistory
from dta_core.errors import ConfigError, DtaError
from dta_core.store import Store
from dta_runtime.api import create_app
from dta_runtime.settings import RuntimeSettings
from dta_runtime.twin import Twin
from starlette.testclient import TestClient

PROBLEM_KEYS = {"type", "title", "status", "detail", "code", "trace_id"}


def problem_of(response) -> dict:  # noqa: ANN001
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert PROBLEM_KEYS <= set(body)
    assert body["status"] == response.status_code
    assert body["type"] == f"urn:dta:error:{body['code']}"
    return body


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_healthz_needs_no_token(app_client: TestClient) -> None:
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789abcdef"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
    ids=["missing", "wrong", "not-bearer", "empty", "longer"],
)
@pytest.mark.parametrize(
    "path", ["/api/v1/meta/health", "/api/v1/openapi.json", "/api/v1/no/such/route"]
)
def test_every_other_path_needs_the_token(
    app_client: TestClient, headers: dict[str, str], path: str
) -> None:
    response = app_client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert problem_of(response)["code"] == "api.unauthorized"


def test_the_token_is_never_logged(app_client: TestClient, log_lines: Path) -> None:
    app_client.get("/api/v1/meta/health", headers={"Authorization": f"Bearer {TOKEN}"})
    app_client.get("/api/v1/meta/health", headers={"Authorization": f"Bearer {TOKEN}-nope"})
    app_client.get("/api/v1/meta/health", headers={"Authorization": f"Basic {TOKEN}"})
    text = log_lines.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "Bearer" not in text
    assert "authorization" not in text.lower()
    refused = [r for r in records(log_lines) if r["message"].startswith("request refused")]
    assert [r["data"]["reason"] for r in refused] == ["token invalid", "token invalid"]
    assert all(r["level"] == "warn" for r in refused)


def test_request_log_names_route_status_and_version(api: Api, log_lines: Path) -> None:
    response = api.get("/api/v1/objects/Junction/node:J2", params={"at": "#5"})
    assert response.status_code == 200
    trace_id = response.headers["traceparent"].split("-")[1]
    served = [
        r
        for r in records(log_lines)
        if r["message"] == "request served" and r["trace_id"] == trace_id
    ]
    assert len(served) == 1
    data = served[0]["data"]
    assert data["route"] == "/api/v1/objects/{type}/{uid}"
    assert data["status"] == 200
    assert data["version_seq"] == 5
    assert data["branch"] == "main"
    assert data["duration_ms"] >= 0
    assert "node:J2" not in json.dumps(data)


def test_slow_requests_are_logged_as_warnings(
    runtime_settings: RuntimeSettings, water_history: WaterHistory, log_lines: Path
) -> None:
    app = create_app(runtime_settings, water_history.twin)
    middleware = next(m for m in app.user_middleware if m.cls.__name__ == "ApiMiddleware")
    middleware.kwargs["slow_ms"] = 0.0
    with TestClient(app) as client:
        response = client.get("/api/v1/meta/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    slow = [r for r in records(log_lines) if r["message"] == "slow request"]
    assert slow and slow[0]["level"] == "warn"
    assert slow[0]["data"]["route"] == "/api/v1/meta/health"
    started = [r for r in records(log_lines) if r["message"] == "API started"]
    assert started and "token" not in json.dumps(started[0]).lower()


def test_trace_context_is_continued(api: Api) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    given = f"00-{trace_id}-00f067aa0ba902b7-01"
    response = api.get("/api/v1/objects/Junction/node:nope", headers={"traceparent": given})
    assert response.status_code == 404
    returned = response.headers["traceparent"].split("-")
    assert returned[0] == "00" and returned[1] == trace_id and returned[3] == "01"
    assert returned[2] != "00f067aa0ba902b7"
    assert problem_of(response)["trace_id"] == trace_id


def test_invalid_trace_context_starts_a_new_trace(api: Api) -> None:
    response = api.get("/api/v1/meta/health", headers={"traceparent": "00-" + "0" * 32 + "-x-01"})
    assert response.status_code == 200
    version, trace_id, span, flags = response.headers["traceparent"].split("-")
    assert len(trace_id) == 32 and set(trace_id) != {"0"} and len(span) == 16


def test_unknown_route_and_method(api: Api) -> None:
    response = api.get("/api/v1/nothing/here")
    assert response.status_code == 404
    assert problem_of(response)["code"] == "api.not_found"
    response = api.client.delete("/api/v1/meta/health", headers=api.headers)
    assert response.status_code == 405
    assert problem_of(response)["code"] == "api.method_not_allowed"


@pytest.mark.parametrize(
    "path, params",
    [
        ("/api/v1/graph/neighbors", {"seed": "node:J1", "depth": 99}),
        ("/api/v1/graph/neighbors", {}),
        ("/api/v1/objects/Junction", {"limit": 0}),
        ("/api/v1/objects/Junction", {"format": "csv"}),
        ("/api/v1/graph/paths", {"source": "node:J1", "target": "node:J2", "k": "many"}),
    ],
)
def test_invalid_parameters_are_400_problems(api: Api, path: str, params: dict) -> None:
    response = api.get(path, params=params)
    assert response.status_code == 400
    body = problem_of(response)
    assert body["code"] == "api.bad_request"
    assert body["errors"] and {"loc", "msg"} <= set(body["errors"][0])


def test_invalid_body_is_a_400_problem(api: Api) -> None:
    response = api.post("/api/v1/graph/subgraph", json={"seeds": [], "depth": 2})
    assert response.status_code == 400
    assert problem_of(response)["code"] == "api.bad_request"
    response = api.post("/api/v1/graph/subgraph", json={"seeds": ["node:J1"], "colour": 1})
    assert response.status_code == 400


def test_store_errors_keep_their_codes(api: Api) -> None:
    cases = [
        ("/api/v1/objects/Nope", {}, 404, "store.unknown_type"),
        ("/api/v1/objects/Junction", {"columns": "colour"}, 404, "store.unknown_property"),
        ("/api/v1/objects/Junction", {"at": "#999"}, 404, "store.unknown_version"),
        ("/api/v1/objects/Junction", {"branch": "nope"}, 404, "store.unknown_branch"),
        ("/api/v1/objects/Junction", {"at": "yesterday"}, 400, "store.bad_selector"),
        ("/api/v1/objects/Junction", {"at": "#1..#3"}, 400, "store.bad_selector"),
        ("/api/v1/objects/Junction", {"where": "elevation:gt:high"}, 400, "store.bad_filter"),
        ("/api/v1/objects/Junction", {"cursor": "garbage"}, 400, "api.bad_cursor"),
        ("/api/v1/object/node:nope", {}, 404, "api.unknown_object"),
    ]
    for path, params, status, code in cases:
        response = api.get(path, params=params)
        assert response.status_code == status, (path, params, response.text)
        assert problem_of(response)["code"] == code, (path, params)


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_store_outage_is_a_503_problem(
    runtime_settings: RuntimeSettings, water_history: WaterHistory, log_lines: Path
) -> None:
    unreachable = dataclasses.replace(
        water_history.settings, host="127.0.0.1", port=_closed_port(), connect_timeout_s=1
    )
    twin = Twin(store=Store(unreachable, water_history.twin.ontology))
    with TestClient(create_app(runtime_settings, twin)) as client:
        response = client.get(
            "/api/v1/objects/Junction", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    twin.close()
    assert response.status_code == 503
    body = problem_of(response)
    assert body["code"] == "store.unavailable"
    assert unreachable.password not in response.text
    errors = [r for r in records(log_lines) if r["level"] == "error"]
    assert errors and unreachable.password not in log_lines.read_text(encoding="utf-8")


def test_startup_fails_when_the_twin_cannot_be_opened(
    runtime_settings: RuntimeSettings, log_lines: Path
) -> None:
    store = dataclasses.replace(runtime_settings.store, port=_closed_port(), connect_timeout_s=1)
    app = create_app(dataclasses.replace(runtime_settings, store=store))
    with pytest.raises(DtaError), TestClient(app):
        pass
    assert any("cannot start" in r["message"] for r in records(log_lines))


def test_a_short_token_is_refused_at_creation(runtime_settings: RuntimeSettings) -> None:
    with pytest.raises(ConfigError, match="DTA_API_TOKEN"):
        create_app(dataclasses.replace(runtime_settings, api_token="short"))


def test_concurrent_requests_beyond_the_pool_all_succeed(api: Api) -> None:
    from concurrent.futures import ThreadPoolExecutor

    paths = [
        "/api/v1/objects/Junction",
        "/api/v1/graph/neighbors?seed=node:J2&depth=2",
        "/api/v1/history/versions",
        "/api/v1/objects/Tank/node:T1/history",
    ] * 10
    with ThreadPoolExecutor(max_workers=40) as pool:
        statuses = list(pool.map(lambda path: api.get(path).status_code, paths))
    assert statuses == [200] * len(paths)
