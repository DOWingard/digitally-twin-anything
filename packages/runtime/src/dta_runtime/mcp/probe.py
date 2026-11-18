"""A check of a running adapter from where an agent stands, with FastMCP's own client.

Used by the toolkit's acceptance: it lists the tools with the token, compares them
with the operations of the document the API serves, asks one read tool with the
token, without one and with a wrong one, and reports what it found. The token is
given to it on standard input and appears in nothing it prints or logs.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

import httpx2
from dta_core.log import Log
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .document import operations

log = Log("mcp.probe")

# The read every probe makes: the twin's health, which every twin answers.
READ_PATH = "/api/v1/meta/health"
OPENAPI_PATH = "/api/v1/openapi.json"
_STATUS = re.compile(r"HTTP error (\d{3})")
_CODE = re.compile(r'"code":\s*"([^"]+)"')


def refusal(text: str) -> dict[str, Any]:
    """The status and problem code of an error a tool reported, as far as it says them."""
    status = _STATUS.search(text)
    code = _CODE.search(text)
    return {
        "status": int(status.group(1)) if status else None,
        "code": code.group(1) if code else None,
    }


def _text(result: Any) -> str:
    return " ".join(getattr(part, "text", "") or "" for part in result.content or [])


async def _call(url: str, token: str | None, tool: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    started = time.perf_counter()
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        result = await client.call_tool(tool, {}, raise_on_error=False)
    found: dict[str, Any] = {
        "ok": not result.is_error,
        "ms": round((time.perf_counter() - started) * 1000.0, 1),
    }
    if result.is_error:
        found.update(refusal(_text(result)))
    else:
        found["data"] = result.structured_content
    return found


async def probe(mcp_url: str, api_url: str, token: str) -> dict[str, Any]:
    """What the adapter at `mcp_url` answers, against the API at `api_url`."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(base_url=api_url, headers=headers, timeout=60.0) as api:
        served = await api.get(OPENAPI_PATH)
        served.raise_for_status()
        document = served.json()
        health = await api.get(READ_PATH)
        health.raise_for_status()
    found = list(operations(document))
    expected = sorted(entry.operation_id for entry in found if not entry.excluded)
    read_tool = next(
        entry.operation_id for entry in found if entry.method == "GET" and entry.path == READ_PATH
    )
    started = time.perf_counter()
    async with Client(StreamableHttpTransport(mcp_url, headers=headers)) as client:
        listed = sorted(tool.name for tool in await client.list_tools())
    list_ms = round((time.perf_counter() - started) * 1000.0, 1)
    with_token = await _call(mcp_url, token, read_tool)
    same = (with_token.pop("data", None) or {}).get("twin_id") == health.json().get("twin_id")
    with_token["same_as_api"] = same
    report = {
        "tools": len(listed),
        "expected": len(expected),
        "missing": sorted(set(expected) - set(listed)),
        "unexpected": sorted(set(listed) - set(expected)),
        "excluded": {entry.operation_id: entry.excluded for entry in found if entry.excluded},
        "list_ms": list_ms,
        "read_tool": read_tool,
        "with_token": with_token,
        "without_token": await _call(mcp_url, None, read_tool),
        # A value of the token's shape that is not the token.
        "wrong_token": await _call(mcp_url, secrets.token_urlsafe(32), read_tool),
    }
    log.info(
        "adapter probed",
        tools=report["tools"],
        missing=len(report["missing"]),
        unexpected=len(report["unexpected"]),
        read_ok=with_token["ok"],
        list_ms=list_ms,
    )
    return report


def run_probe(mcp_url: str, api_url: str, stdin_text: str) -> dict[str, Any]:
    """The probe's report; the token is the JSON object `{"token": ...}` on standard input."""
    import anyio

    try:
        token = json.loads(stdin_text)["token"]
    except (ValueError, KeyError, TypeError) as exc:
        from dta_core.errors import ConfigError

        log.error("the probe was not given a token on standard input")
        raise ConfigError(
            'the probe reads {"token": "..."} on standard input', code="mcp.probe_input"
        ) from exc
    return anyio.run(probe, mcp_url, api_url, token)
