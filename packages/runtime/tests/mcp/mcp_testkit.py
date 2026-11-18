"""The adapter served over HTTP in front of an API served over HTTP."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
from dta_runtime.mcp import McpSettings, build_server, http_app
from live_api_testkit import served as serve


@dataclass
class Served:
    """An API served over HTTP and the adapter in front of it."""

    api_url: str
    mcp_url: str
    api: httpx.Client
    tools: list[str]


@contextlib.contextmanager
def serve_adapter(api_url: str) -> Iterator[tuple[str, list[str]]]:
    """The adapter for the API at `api_url`, served by uvicorn in a thread."""
    server, names = build_server(McpSettings(api_url=api_url))
    with serve(http_app(server, names)) as base_url:
        yield f"{base_url}/mcp", names
