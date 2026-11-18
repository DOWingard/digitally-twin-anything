"""The MCP adapter: the twin's HTTP API offered to agents as MCP tools, and nothing more.

FastMCP's OpenAPI integration turns the API's operations into tools (see
`document.py` for which). The adapter holds no token: each call to the API carries
the bearer token the agent sent with its own request, so the API's one token stays
the only credential and the API's refusals are the adapter's. Listing the tools is
refused the same way, by asking the API whether the agent's token is good.

The adapter logs as the other services do, into the stack's log volume, under the
trace id of the agent's request (from its `traceparent`, else a new one), and passes
that trace on to the API so both logs of one call share it. It never logs a header.
"""

from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
from dta_core.config import require_env
from dta_core.errors import ConfigError
from dta_core.log import Log, current_trace_id, new_trace_id, trace
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, to_mcp_error
from fastmcp.server.dependencies import get_http_headers, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import MCPType
from starlette.datastructures import Headers
from starlette.middleware import Middleware as AsgiMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..api.middleware import TRACEPARENT, parse_traceparent
from .document import FORMAT_PARAMETER, api_document, operations

log = Log("mcp")

ENV_API_URL = "DTA_API_URL"
ENV_LOGS_DIR = "DTA_LOGS_DIR"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
SERVER_NAME = "dta-twin"
# Hosts an agent may name in its requests: the loopback address the port is published
# on, and the service's own name inside the stack. Anything else is refused (421),
# which keeps a web page from reaching the adapter through a rebound DNS name.
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "mcp")
# The API answers long work with a job at once; a slow synchronous answer is a
# question or a function run, held to its own latency budget by the API.
UPSTREAM_TIMEOUT_S = 300.0
# Where the token is checked when the tools are listed: a route that needs the token
# and reads nothing from the store.
TOKEN_CHECK_PATH = "/api/v1/openapi.json"
_TRACE_STATE = "dta_trace_id"
# JSON-RPC's code for a request the server will not serve as it was sent.
JSONRPC_INVALID_REQUEST = -32600
INSTRUCTIONS = (
    "The tools are the operations of a digital twin's HTTP API, each named by its "
    "operation id. Send the twin's API token on every request as `Authorization: Bearer "
    "<token>`; this adapter holds none, and the API refuses what it would refuse over "
    "HTTP. Tables come back as JSON pages: follow `next_cursor`."
)

# What the API answered to the call being made, for the line that logs the tool call.
_upstream: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "dta_mcp_upstream", default=None
)


@dataclass(frozen=True)
class McpSettings:
    """Where the API is. Deliberately nothing else: no token, no store password."""

    api_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> McpSettings:
        values = require_env((ENV_API_URL, ENV_LOGS_DIR), environ)
        url = values[ENV_API_URL].rstrip("/")
        if not url.startswith(("http://", "https://")):
            log.error("the API address is not an http(s) URL", name=ENV_API_URL)
            raise ConfigError(
                f"{ENV_API_URL} must be an http or https URL",
                code="config.bad_value",
                detail={"name": ENV_API_URL},
            )
        return cls(api_url=url)


def _request_trace() -> str:
    """The trace id of the agent's request being served."""
    found = current_trace_id()
    if found:
        return found
    try:
        state = get_http_request().scope.get("state") or {}
    except RuntimeError:
        state = {}
    return str(state.get(_TRACE_STATE) or new_trace_id())


class CallerCredentials(httpx2.Auth):
    """Gives each request to the API the agent's own token and the trace of its call.

    The token is taken from the agent's request as it is, never stored and never
    logged. A request without one goes on without one, and the API refuses it.
    The API is always asked for JSON: a tool's result has to be JSON.
    """

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        given = get_http_headers(include={"authorization"}).get("authorization")
        if given:
            request.headers["Authorization"] = given
        elif "Authorization" in request.headers:
            del request.headers["Authorization"]
        request.headers[TRACEPARENT] = f"00-{_request_trace()}-{secrets.token_hex(8)}-01"
        request.headers["Accept"] = "application/json"
        if FORMAT_PARAMETER in request.url.params:
            request.url = request.url.copy_set_param(FORMAT_PARAMETER, "json")
        started = time.perf_counter()
        response = yield request
        found = _upstream.get()
        if found is not None:
            found["status"] = response.status_code
            found["api_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        log.debug(
            "API answered",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            authorized=bool(given),
        )


class ToolLog(Middleware):
    """Logs every tool call and listing, and refuses a listing the API would refuse."""

    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.client = client

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        name = getattr(context.message, "name", None)
        found: dict[str, Any] = {}
        token = _upstream.set(found)
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            status = found.get("status")
            if isinstance(status, int) and status < 500:
                log.warn(
                    "tool call refused by the API",
                    tool=name,
                    status=status,
                    duration_ms=elapsed,
                )
            else:
                log.error(
                    "tool call failed",
                    tool=name,
                    status=status,
                    error_type=type(exc).__name__,
                    duration_ms=elapsed,
                )
            raise
        finally:
            _upstream.reset(token)
        log.info(
            "tool called",
            tool=name,
            status=found.get("status"),
            api_ms=found.get("api_ms"),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return result

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        started = time.perf_counter()
        status, problem = await self._token_status()
        if status != 200:
            log.warn("tool listing refused by the API", status=status, code=problem)
            # Only an MCP error reaches the agent with its message; FastMCP masks others.
            raise to_mcp_error(
                ToolError(
                    f"HTTP error {status}: the API refused the token sent with this request "
                    f'("code": "{problem}"); send the twin\'s API token as '
                    "`Authorization: Bearer <token>`"
                ),
                default_code=JSONRPC_INVALID_REQUEST,
            )
        tools = await call_next(context)
        log.info(
            "tools listed",
            tools=len(tools),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return tools

    async def _token_status(self) -> tuple[int, str | None]:
        """What the API answers the agent's token with, on a route that reads nothing:
        its status and, when it refuses, its problem code."""
        headers = {TRACEPARENT: f"00-{_request_trace()}-{secrets.token_hex(8)}-01"}
        given = get_http_headers(include={"authorization"}).get("authorization")
        if given:
            headers["Authorization"] = given
        try:
            response = await self.client.get(TOKEN_CHECK_PATH, headers=headers, auth=None)
        except httpx2.HTTPError as exc:
            log.error("the API does not answer; tools cannot be listed", error=type(exc).__name__)
            raise to_mcp_error(
                ToolError(f"the twin's API does not answer: {type(exc).__name__}")
            ) from None
        if response.status_code == 200:
            return 200, None
        try:
            problem = response.json().get("code")
        except ValueError:
            problem = None
        return response.status_code, problem


class RequestTrace:
    """Binds the trace of each request to the adapter's log and logs the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = parse_traceparent(Headers(scope=scope).get(TRACEPARENT))
        trace_id = given[0] if given is not None else new_trace_id()
        scope.setdefault("state", {})[_TRACE_STATE] = trace_id
        state = {"status": 500}
        started = time.perf_counter()

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            await send(message)

        with trace(trace_id):
            try:
                await self.app(scope, receive, sending)
            except Exception as exc:
                log.error("request failed unexpectedly", exc=exc, path=scope.get("path"))
                raise
            finally:
                fields = {
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": state["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
                if scope.get("path") == HEALTH_PATH:
                    log.debug("request served", **fields)
                else:
                    log.info("request served", **fields)


def build_server(
    settings: McpSettings,
    document: Mapping[str, Any] | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[FastMCP, list[str]]:
    """The adapter's server over `document` (by default the API's own) and its tool names.

    `transport` replaces the network between the adapter and the API, for tests.
    """
    started = time.perf_counter()
    spec = dict(document) if document is not None else api_document()
    found = list(operations(spec))
    excluded = {entry.operation_id: entry.excluded for entry in found if entry.excluded}
    tabular = {entry.operation_id for entry in found if entry.tabular and not entry.excluded}
    names = [entry.operation_id for entry in found if not entry.excluded]
    client = httpx2.AsyncClient(
        base_url=settings.api_url,
        auth=CallerCredentials(),
        timeout=httpx2.Timeout(UPSTREAM_TIMEOUT_S),
        **({"transport": transport} if transport is not None else {}),
    )

    def route_type(route: Any, default: MCPType) -> MCPType:
        return MCPType.EXCLUDE if route.operation_id in excluded else MCPType.TOOL

    def shape(route: Any, component: Any) -> None:
        # FastMCP shortens names at their first `__` and at 56 characters; a tool
        # carries the operation id whole.
        component.name = route.operation_id
        if route.operation_id in tabular:
            schema = dict(component.parameters)
            properties = dict(schema.get("properties") or {})
            properties.pop(FORMAT_PARAMETER, None)
            schema["properties"] = properties
            if "required" in schema:
                schema["required"] = [n for n in schema["required"] if n != FORMAT_PARAMETER]
            component.parameters = schema

    server = FastMCP.from_openapi(
        dict(spec),
        client=client,
        name=SERVER_NAME,
        route_map_fn=route_type,
        mcp_component_fn=shape,
        instructions=INSTRUCTIONS,
        middleware=[ToolLog(client)],
    )
    log.info(
        "MCP adapter built",
        tools=len(names),
        excluded=excluded,
        without_format=sorted(tabular),
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return server, names


async def _listed(server: FastMCP) -> list[str]:
    return [tool.name for tool in await server.list_tools(run_middleware=False)]


def http_app(server: FastMCP, names: Sequence[str]) -> Any:
    """The adapter's ASGI application: streamable HTTP at `MCP_PATH`, health at `HEALTH_PATH`."""

    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "mcp", "tools": len(names)})

    return server.http_app(
        path=MCP_PATH,
        middleware=[AsgiMiddleware(RequestTrace)],
        json_response=True,
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=list(ALLOWED_HOSTS),
    )


def serve(settings: McpSettings, host: str, port: int) -> None:
    """Build the adapter and serve it until the process is stopped."""
    import anyio
    import uvicorn

    server, names = build_server(settings)
    listed = anyio.run(_listed, server)
    if sorted(listed) != sorted(names):
        # FastMCP swallows an error in the component hook; a tool must not go by another name.
        log.error(
            "the tools are not the operations they were made from",
            missing=sorted(set(names) - set(listed)),
            unexpected=sorted(set(listed) - set(names)),
        )
        raise ConfigError(
            "the MCP adapter's tools do not match the API's operations",
            code="mcp.tools_mismatch",
            detail={
                "missing": sorted(set(names) - set(listed)),
                "unexpected": sorted(set(listed) - set(names)),
            },
        )
    log.info("MCP adapter starting", host=host, port=port, path=MCP_PATH, tools=len(names))
    uvicorn.run(
        http_app(server, names),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
