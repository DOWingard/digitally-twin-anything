"""The outermost layer of the API: credentials, trace context, the request log
and the last line of defence against unexpected errors.

It runs before routing, so every path but the open ones needs the token,
including paths that match no route and the OpenAPI document.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from typing import Any

from dta_core.log import Log, new_trace_id, trace
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context import RequestInfo, begin_request, end_request
from .problems import internal_error_response, problem

log = Log("api.request")

# W3C trace context, version 00. An all-zero trace or parent id is invalid.
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
TRACEPARENT = "traceparent"
SLOW_REQUEST_MS = 2_000.0


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """(trace id, flags) of a valid `traceparent` header, else None."""
    if not value:
        return None
    match = _TRACEPARENT.match(value.strip().lower())
    if not match or set(match.group(1)) == {"0"} or set(match.group(2)) == {"0"}:
        return None
    return match.group(1), match.group(3)


class ApiMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        open_paths: frozenset[str],
        slow_ms: float = SLOW_REQUEST_MS,
    ) -> None:
        self.app = app
        self._token = token.encode("utf-8")
        self._open = open_paths
        self._slow_ms = slow_ms

    def _credentials(self, headers: Headers) -> str:
        """`ok`, `missing` or `invalid`; only the log is told which."""
        value = headers.get("authorization")
        if not value:
            return "missing"
        scheme, _, given = value.partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return "invalid"
        if hmac.compare_digest(given.strip().encode("utf-8"), self._token):
            return "ok"
        return "invalid"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        headers = Headers(scope=scope)
        given = parse_traceparent(headers.get(TRACEPARENT))
        trace_id, flags = given if given is not None else (new_trace_id(), "01")
        traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"
        state: dict[str, Any] = {"status": 500, "started": False}

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                MutableHeaders(scope=message)[TRACEPARENT] = traceparent
            await send(message)

        with trace(trace_id):
            info, token = begin_request()
            try:
                if scope["path"] not in self._open:
                    credentials = self._credentials(headers)
                    if credentials != "ok":
                        log.warn(
                            "request refused: no valid bearer token",
                            method=scope["method"],
                            reason=f"token {credentials}",
                        )
                        refusal = problem(
                            401,
                            "api.unauthorized",
                            "a valid bearer token is required",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                        await refusal(scope, receive, send_with_trace)
                        return
                await self.app(scope, receive, send_with_trace)
            except Exception as exc:
                if state["started"]:
                    log.error(
                        "request failed after its response had begun",
                        exc=exc,
                        method=scope["method"],
                        route=_route(scope),
                    )
                    raise
                log.error(
                    "request failed unexpectedly",
                    exc=exc,
                    method=scope["method"],
                    route=_route(scope),
                )
                await internal_error_response()(scope, receive, send_with_trace)
            finally:
                self._log(scope, state["status"], started, info)
                end_request(token)

    def _log(self, scope: Scope, status: int, started: float, info: RequestInfo) -> None:
        fields: dict[str, Any] = {
            "method": scope["method"],
            "route": _route(scope),
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
        if info.version is not None:
            fields["version_seq"] = info.version.seq
            fields["version"] = info.version.id
            fields["branch"] = info.version.branch
        log.debug("request path", path=scope["path"])
        if fields["duration_ms"] > self._slow_ms:
            log.warn("slow request", slow_ms=self._slow_ms, **fields)
        else:
            log.info("request served", **fields)


def _route(scope: Scope) -> str:
    """The route template that matched, never the raw path with its uids."""
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"
