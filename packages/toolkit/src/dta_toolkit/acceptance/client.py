"""The twin's HTTP API as the toolkit reaches it: on the loopback address, with the token.

The port and the token come from the workspace's `.env`. Neither is ever
logged, printed or put into an error: requests are described by method and
path only, and a failed connection by the type of its error, because the text
of a connection error quotes the address.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest

log = Log("acceptance.client")

LOOPBACK = "127.0.0.1"
DEFAULT_TIMEOUT_S = 60.0
_RETRY_PAUSE_S = 1.0


class ApiUnavailable(DtaError):
    """The twin's API could not be reached or its address is not usable."""

    code = "api.unreachable"


def _port(values: Mapping[str, str], name: str) -> int:
    text = values.get(name, "")
    if not text:
        log.error("port variable not set", variable=name)
        raise ApiUnavailable(
            f"{name} is not set in .env", code="env.missing", detail={"missing": [name]}
        )
    if not text.isdigit() or not 0 < int(text) < 65536:
        log.error("port variable is not a port number", variable=name)
        raise ApiUnavailable(
            f"{name} in .env is not a port number", code="env.bad_value", detail={"variable": name}
        )
    return int(text)


def service_url(values: Mapping[str, str], port_env: str) -> str:
    """Base address of a service published on the loopback interface; never print it."""
    return f"http://{LOOPBACK}:{_port(values, port_env)}"


class TwinApi:
    """One client for the API of one twin. Use as a context manager."""

    def __init__(
        self,
        manifest: Manifest,
        values: Mapping[str, str],
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        interfaces = manifest.interfaces
        token = values.get(interfaces.token_env, "")
        if not token:
            log.error("token variable not set", variable=interfaces.token_env)
            raise ApiUnavailable(
                f"{interfaces.token_env} is not set in .env",
                code="env.missing",
                detail={"missing": [interfaces.token_env]},
            )
        self.base_url = service_url(values, interfaces.api_port_env)
        self.token = token
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self.requests = 0

    def __enter__(self) -> TwinApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """One request; an unreachable API raises ApiUnavailable naming the path only."""
        started = time.perf_counter()
        self.requests += 1
        extra: dict[str, Any] = {}
        if timeout is not None:
            extra["timeout"] = timeout
        try:
            response = self.client.request(
                method, path, params=params, json=json, content=content, headers=headers, **extra
            )
        except httpx.HTTPError as exc:
            log.error(
                "API request failed",
                method=method,
                path=path,
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )
            raise ApiUnavailable(
                f"{method} {path} did not get an answer: {type(exc).__name__}",
                detail={"method": method, "path": path, "error_type": type(exc).__name__},
            ) from None
        log.debug(
            "API request answered",
            method=method,
            path=path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def status_of(self, path: str) -> int:
        """The status a GET answers with, its body left unread: whether a large file is
        there is known without downloading it."""
        started = time.perf_counter()
        self.requests += 1
        try:
            with self.client.stream("GET", path) as response:
                status = response.status_code
        except httpx.HTTPError as exc:
            log.error(
                "API request failed",
                method="GET",
                path=path,
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )
            raise ApiUnavailable(
                f"GET {path} did not get an answer: {type(exc).__name__}",
                detail={"method": "GET", "path": path, "error_type": type(exc).__name__},
            ) from None
        log.debug(
            "API status read",
            path=path,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return status

    def wait_healthy(self, attempts: int = 30) -> httpx.Response:
        """`GET /healthz` until it answers; the service may still be binding its port."""
        last: ApiUnavailable | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.get("/healthz")
            except ApiUnavailable as exc:
                last = exc
                log.warn("API not answering yet; retrying", attempt=attempt, of=attempts)
                time.sleep(_RETRY_PAUSE_S)
        assert last is not None
        raise last


def probe_health(base_url: str, timeout: float = 10.0, attempts: int = 30) -> int:
    """Status of `GET /healthz` on a service, retried while it does not answer."""
    for attempt in range(1, attempts + 1):
        try:
            return httpx.get(f"{base_url}/healthz", timeout=timeout).status_code
        except httpx.HTTPError as exc:
            log.warn(
                "service not answering yet; retrying",
                attempt=attempt,
                of=attempts,
                error_type=type(exc).__name__,
            )
            if attempt == attempts:
                raise ApiUnavailable(
                    f"GET /healthz did not get an answer: {type(exc).__name__}",
                    detail={"path": "/healthz", "error_type": type(exc).__name__},
                ) from None
            time.sleep(_RETRY_PAUSE_S)
    raise AssertionError("unreachable")
