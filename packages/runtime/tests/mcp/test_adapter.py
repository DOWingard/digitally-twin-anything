"""The MCP adapter, served over HTTP in front of a twin's API served over HTTP, called
with FastMCP's own client as an agent calls it."""

from __future__ import annotations

import json
import secrets
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from api_testkit import TOKEN
from dta_core.errors import ConfigError
from dta_runtime.mcp import McpSettings
from dta_runtime.mcp.probe import probe, refusal
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from live_api_testkit import JOB_TIMEOUT_S, plant
from mcp_testkit import Served

AUTH = {"Authorization": f"Bearer {TOKEN}"}
LIST_LIMIT_S = 2.0
OVERHEAD_LIMIT_MS = 100.0
READ = "list_objects_api_v1_objects__type__get"
READ_PATH = "/api/v1/objects/Tank"
TEXT = ("application/json", "application/yaml", "text/")


def _client(url: str, headers: dict[str, str] | None = None) -> Client:
    return Client(StreamableHttpTransport(url, headers=dict(headers or {})))


def _list(url: str, headers: dict[str, str] | None = None) -> list[Any]:
    async def go() -> list[Any]:
        async with _client(url, headers) as client:
            return list(await client.list_tools())

    return anyio.run(go)


def _call(
    url: str, tool: str, arguments: dict[str, Any], headers: dict[str, str] | None = AUTH
) -> Any:
    async def go() -> Any:
        async with _client(url, headers) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return anyio.run(go)


def _text(result: Any) -> str:
    return " ".join(getattr(part, "text", "") or "" for part in result.content or [])


def _binary_only(media: list[str]) -> bool:
    return bool(media) and not any(m.startswith(TEXT) or m.endswith("+json") for m in media)


def test_the_tools_are_the_api_operations_but_those_carried_only_as_bytes(water: Served):
    document = water.api.get("/api/v1/openapi.json").json()
    expected, left_out, tabular = set(), set(), set()
    for item in document["paths"].values():
        for operation in item.values():
            body = list((operation.get("requestBody") or {}).get("content") or {})
            answers = [
                list(answer.get("content") or {})
                for status, answer in operation["responses"].items()
                if status.startswith("2")
            ]
            if _binary_only(body) or (answers and all(_binary_only(a) for a in answers)):
                left_out.add(operation["operationId"])
            else:
                expected.add(operation["operationId"])
            media = {m for answer in answers for m in answer}
            if any(m.startswith("application/vnd.apache") for m in media):
                tabular.add(operation["operationId"])

    started = time.perf_counter()
    tools = {tool.name: tool for tool in _list(water.mcp_url, AUTH)}
    listed_s = time.perf_counter() - started

    print(f"\ntool list: {len(tools)} tools in {listed_s * 1000:.1f} ms; left out {left_out}")
    # Each tool carries its operation's id whole, however long.
    assert set(tools) == expected
    assert sorted(water.tools) == sorted(expected)
    assert left_out and not left_out & set(tools)
    assert any(len(name) > 56 and "__" in name for name in tools)
    # Operations that also answer with Arrow or Parquet are tools that answer JSON only.
    assert tabular and tabular <= set(tools)
    for name in tabular:
        assert "format" not in tools[name].input_schema.get("properties", {}), name
    assert listed_s <= LIST_LIMIT_S


def test_a_read_tool_answers_what_the_api_answers(water: Served):
    head = water.api.get(READ_PATH).json()["version"]["seq"]
    params = {"at": f"#{head - 1}", "limit": 50}
    over_http = water.api.get(READ_PATH, params=params).json()

    result = _call(water.mcp_url, READ, {"type": "Tank", **params})

    assert not result.is_error, _text(result)
    assert result.structured_content["data"] == over_http["data"]
    assert result.structured_content["version"] == over_http["version"]
    assert len(over_http["data"]) > 0


def test_a_table_tool_answers_json_whatever_the_agent_asks(water: Served):
    result = _call(water.mcp_url, READ, {"type": "Tank", "format": "arrow"})

    assert not result.is_error, _text(result)
    assert isinstance(result.structured_content["data"], list)


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer wrong"}, "random"])
def test_without_the_token_a_tool_is_refused_as_the_api_refuses(water: Served, headers: Any):
    if headers == "random":
        headers = {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}
    over_http = httpx.get(f"{water.api_url}{READ_PATH}", headers=headers or {})

    result = _call(water.mcp_url, READ, {"type": "Tank"}, headers=headers)

    assert over_http.status_code == 401
    assert result.is_error
    assert refusal(_text(result)) == {
        "status": over_http.status_code,
        "code": over_http.json()["code"],
    }
    with pytest.raises(Exception, match="HTTP error 401"):
        _list(water.mcp_url, headers)


def test_a_tool_that_changes_the_twin_starts_a_sync_run(
    live: Callable[[dict[str, Any]], Served], plant_dir: Path
):
    sources, _ = plant(plant_dir, "registry", "lab")
    served = live(sources)

    result = _call(served.mcp_url, "start_run_api_v1_sync_runs_post", {})

    assert not result.is_error, _text(result)
    job = result.structured_content["job"]
    assert job["kind"] == "sync"
    deadline = time.monotonic() + JOB_TIMEOUT_S
    while True:
        done = served.api.get(f"/api/v1/lifecycle/jobs/{job['id']}").json()["data"]
        if done["state"] in ("succeeded", "failed") or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert done["state"] == "succeeded", done
    readings = served.api.get("/api/v1/objects/Reading").json()["data"]
    assert sorted(row["uid"] for row in readings) == ["reading:R1", "reading:R2"]


def test_the_adapter_logs_each_call_under_the_agent_trace_and_never_the_token(
    water: Served, log_to: Callable[[Path], Path], tmp_path: Path
):
    lines = log_to(tmp_path / "mcp.jsonl")
    trace_id = secrets.token_hex(16)
    headers = {**AUTH, "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}

    assert not _call(water.mcp_url, READ, {"type": "Tank"}, headers=headers).is_error
    _call(water.mcp_url, READ, {"type": "Tank"}, headers=None)

    text = lines.read_text(encoding="utf-8")
    assert TOKEN not in text
    records = [json.loads(line) for line in text.splitlines()]
    tool_lines = [r for r in records if r["message"] == "tool called"]
    api_lines = [r for r in records if r["context"].startswith("api") and r["trace_id"] == trace_id]
    assert any(r["trace_id"] == trace_id and r["data"]["tool"] == READ for r in tool_lines)
    # The API's own lines of the call carry the agent's trace.
    assert api_lines
    assert any(r["message"] == "tool call refused by the API" for r in records)


def test_a_read_through_the_adapter_adds_little_to_the_same_read_on_the_api(water: Served):
    params = {"type": "Tank", "limit": 10}

    async def go() -> tuple[list[float], list[float]]:
        direct, adapted = [], []
        async with (
            _client(water.mcp_url, AUTH) as client,
            httpx.AsyncClient(base_url=water.api_url, headers=AUTH) as api,
        ):
            for _ in range(3):
                await client.call_tool(READ, params)
                await api.get(READ_PATH, params={"limit": 10})
            for _ in range(20):
                started = time.perf_counter()
                await api.get(READ_PATH, params={"limit": 10})
                direct.append((time.perf_counter() - started) * 1000.0)
                started = time.perf_counter()
                await client.call_tool(READ, params)
                adapted.append((time.perf_counter() - started) * 1000.0)
        return direct, adapted

    direct, adapted = anyio.run(go)
    added = statistics.median(adapted) - statistics.median(direct)
    print(
        f"\nread on the API: median {statistics.median(direct):.1f} ms; "
        f"through the adapter: median {statistics.median(adapted):.1f} ms; added {added:.1f} ms"
    )
    assert added <= OVERHEAD_LIMIT_MS


def test_the_probe_reports_what_an_agent_finds(water: Served):
    report = anyio.run(probe, water.mcp_url, water.api_url, TOKEN)

    assert report["tools"] == report["expected"] == len(water.tools)
    assert (report["missing"], report["unexpected"]) == ([], [])
    assert report["with_token"]["ok"] and report["with_token"]["same_as_api"]
    assert report["without_token"] == {
        "ok": False,
        "status": 401,
        "code": "api.unauthorized",
        "ms": report["without_token"]["ms"],
    }
    assert report["wrong_token"]["status"] == 401
    assert TOKEN not in json.dumps(report)


def test_a_host_the_adapter_does_not_answer_to_is_refused(water: Served):
    answer = httpx.post(
        water.mcp_url,
        headers={**AUTH, "Host": "attacker.example", "Accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert answer.status_code == 421


def test_the_adapter_needs_the_api_address_and_never_a_token():
    with pytest.raises(ConfigError) as raised:
        McpSettings.from_env({})

    assert raised.value.detail["missing"] == ["DTA_API_URL", "DTA_LOGS_DIR"]
    assert list(McpSettings.__dataclass_fields__) == ["api_url"]
    with pytest.raises(ConfigError, match="DTA_API_URL"):
        McpSettings.from_env({"DTA_API_URL": "api:8000", "DTA_LOGS_DIR": "/x"})
