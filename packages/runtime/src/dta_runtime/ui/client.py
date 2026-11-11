"""The interface's only way to the twin: one HTTP client for the twin's API.

The bearer token is held here, on the server, and goes nowhere else: not into
a page, a callback response, a log line or an error. Every failure becomes an
`ApiProblem` carrying what the user is shown: the problem's title, code,
detail and trace id, as the API reported them or as this client names a
failure to reach the API at all.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx
from dta_core.log import Log, current_trace_id, new_trace_id

log = Log("ui.client")

API_PREFIX = "/api/v1"
TIMEOUT = httpx.Timeout(20.0, connect=3.0)
SLOW_CALL_MS = 2_000.0

Params = Mapping[str, str | int | Sequence[str] | None]


class ApiProblem(Exception):
    """A call to the API that did not succeed, as it is shown to the user.

    A plain exception rather than a frozen dataclass: the interpreter and
    contextlib assign `__traceback__` and `__cause__` on exceptions they handle.
    """

    def __init__(
        self,
        *,
        code: str,
        title: str,
        detail: str,
        status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(f"{title} ({code})")
        self.code = code
        self.title = title
        self.detail = detail
        self.status = status
        self.trace_id = trace_id


def problem_from_response(response: httpx.Response) -> ApiProblem:
    """The API's RFC 9457 problem document, or a problem naming an unexpected answer."""
    document: Any = None
    try:
        document = response.json()
    except ValueError:
        pass
    if isinstance(document, dict) and isinstance(document.get("code"), str):
        return ApiProblem(
            code=document["code"],
            title=str(document.get("title") or document["code"]),
            detail=str(document.get("detail") or ""),
            status=response.status_code,
            trace_id=document.get("trace_id") or response_trace(response),
        )
    return ApiProblem(
        code="ui.bad_response",
        title="Unexpected answer from the API",
        detail=f"the API answered {response.status_code} without a problem document",
        status=response.status_code,
        trace_id=response_trace(response),
    )


def response_trace(response: httpx.Response) -> str | None:
    parts = response.headers.get("traceparent", "").split("-")
    return parts[1] if len(parts) == 4 else None


def query_pairs(params: Params | None) -> list[tuple[str, str]]:
    """Query pairs, repeated keys for sequences, absent values left out."""
    pairs: list[tuple[str, str]] = []
    for name, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, str | int):
            pairs.append((name, str(value)))
        else:
            pairs.extend((name, str(item)) for item in value)
    return pairs


class ApiClient:
    """Calls to the twin's API with the bearer token; one connection pool for all threads."""

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        timeout: httpx.Timeout = TIMEOUT,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.api_url + API_PREFIX,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def get(
        self, route: str, *, path: Mapping[str, str] | None = None, params: Params | None = None
    ) -> dict[str, Any]:
        return self._call("GET", route, path, params, None)

    def post(
        self,
        route: str,
        *,
        path: Mapping[str, str] | None = None,
        params: Params | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._call("POST", route, path, params, body)

    def _call(
        self,
        method: str,
        route: str,
        path: Mapping[str, str] | None,
        params: Params | None,
        body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """One request to `route`, a template such as `/objects/{type}` filled from `path`.

        Logs name the route template, never the filled-in path or query values,
        which may hold uids and filter values typed by the user.
        """
        url = route.format(**{name: quote(value, safe="") for name, value in (path or {}).items()})
        trace_id = current_trace_id() or new_trace_id()
        headers = {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}
        started = time.perf_counter()
        names = sorted({name for name, _ in query_pairs(params)})
        try:
            response = self._http.request(
                method, url, params=query_pairs(params), json=body, headers=headers
            )
        except httpx.TimeoutException as exc:
            elapsed = _ms(started)
            log.error(
                "API call timed out",
                method=method,
                route=route,
                params=names,
                duration_ms=elapsed,
                error=type(exc).__name__,
            )
            raise ApiProblem(
                code="ui.api_timeout",
                title="The API did not answer in time",
                detail=f"no answer from the API within {elapsed / 1000.0:.1f} s; retry later",
                trace_id=trace_id,
            ) from exc
        except httpx.HTTPError as exc:
            elapsed = _ms(started)
            log.error(
                "API call failed: the API cannot be reached",
                method=method,
                route=route,
                params=names,
                duration_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise ApiProblem(
                code="ui.api_unreachable",
                title="The API cannot be reached",
                detail="the interface cannot connect to the twin's API; retry later",
                trace_id=trace_id,
            ) from exc
        elapsed = _ms(started)
        if response.status_code >= 400:
            problem = problem_from_response(response)
            fields = {
                "method": method,
                "route": route,
                "params": names,
                "status": response.status_code,
                "code": problem.code,
                "duration_ms": elapsed,
                "api_trace_id": problem.trace_id,
            }
            if response.status_code >= 500:
                log.error("API call failed", **fields)
            else:
                log.warn("API call refused", **fields)
            raise problem
        try:
            document = response.json()
        except ValueError as exc:
            log.error(
                "API answer is not JSON",
                method=method,
                route=route,
                status=response.status_code,
                duration_ms=elapsed,
                content_type=response.headers.get("content-type"),
            )
            raise ApiProblem(
                code="ui.bad_response",
                title="Unexpected answer from the API",
                detail="the API's answer could not be read as JSON",
                status=response.status_code,
                trace_id=response_trace(response) or trace_id,
            ) from exc
        if not isinstance(document, dict):
            log.error("API answer is not a JSON object", method=method, route=route)
            raise ApiProblem(
                code="ui.bad_response",
                title="Unexpected answer from the API",
                detail="the API's answer is not a JSON object",
                status=response.status_code,
                trace_id=trace_id,
            )
        if elapsed > SLOW_CALL_MS:
            log.warn("slow API call", method=method, route=route, duration_ms=elapsed)
        else:
            log.debug("API call", method=method, route=route, params=names, duration_ms=elapsed)
        return document


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)
