"""Read-only connector to an interface that answers GET requests with JSON.

Each dataset is one path whose responses hold a list of rows (JSON objects),
spread over pages that dlt's REST client follows. A live twin polls such a
dataset every few minutes with `since`, which is sent to the source when the
dataset names a parameter for it and is always applied to the rows returned.

A connector serves one read at a time: the attempt count it logs is kept per
connector, not per request.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from dlt.common import jsonpath
from dlt.sources.helpers.rest_client import RESTClient
from requests import Response, Session
from requests import exceptions as rq

from ...sources import (
    SourceAuthFailed,
    SourceConfigError,
    SourceError,
    SourceRateLimited,
    SourceReadError,
    SourceUnavailable,
)
from ..base import BaseConnector, Piece, ReadRequest, Redactor, _elapsed_ms
from ..normalize import as_utc_datetime
from .client import (
    RETRIED_ERRORS,
    ForeignTarget,
    Origin,
    QuietRESTClient,
    make_paginator,
    make_session,
    origin_of,
    response_guard,
    retry_after_s,
    url_of,
)
from .settings import HttpDataset, HttpOptions
from .tables import PageTables, RowsError

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CAUSE_CHARS = 500


class HttpConnector(BaseConnector):
    kind = "http"

    def __init__(
        self, source_id: str, location: str, redactor: Redactor, settings: HttpOptions
    ) -> None:
        super().__init__(source_id, location, redactor)
        self._settings = settings
        self._client_or_none: RESTClient | None = None
        self._session: Session | None = None
        self._origin = Origin("", "", None)
        self._hooks: dict[str, list[Any]] = {}
        # Attempts of the request in flight, and of the last one finished.
        self._failed_attempts = 0
        self._attempts = 0

    # -- connecting ----------------------------------------------------------

    def _connect(self, resolved: str) -> None:
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise self._error(
                SourceConfigError, "the location is not an http:// or https:// address"
            )
        if parts.query or parts.fragment:
            raise self._error(
                SourceConfigError,
                "the location must not hold a query or fragment; put parameters under "
                "datasets.<name>.params",
            )
        try:
            parts.port  # noqa: B018 - parsing the port is the check
        except ValueError:
            raise self._error(SourceConfigError, "the location's port is not a number") from None
        self._origin = origin_of(resolved)
        try:
            self._session = make_session(self._settings, self._should_retry)
        except Exception as exc:
            raise self._translate(exc, None, "creating the HTTP session") from None
        self._hooks = {"response": [response_guard(self._origin)]}
        self._client_or_none = QuietRESTClient(
            base_url=resolved, headers=dict(self._settings.headers), session=self._session
        )
        self._log.info(
            "connector opened",
            source=self.source_id,
            kind=self.kind,
            location=self.location,
            scheme=parts.scheme,
            datasets=len(self._settings.datasets),
            headers=sorted(self._settings.headers),
            clock=self._settings.clock is not None,
            timeout_s=self._settings.timeout_s,
            max_attempts=self._settings.max_attempts,
            max_wait_s=self._settings.max_wait_s,
        )

    @property
    def _client(self) -> RESTClient:
        if self._client_or_none is None:
            raise self._error(SourceError, "the connector is not connected")
        return self._client_or_none

    # -- datasets ------------------------------------------------------------

    def _list_datasets(self) -> list[str]:
        return list(self._settings.datasets)

    def _has_dataset(self, name: str) -> bool:
        return name in self._settings.datasets

    # -- reading -------------------------------------------------------------

    def _pieces(self, request: ReadRequest) -> Iterator[Piece]:
        spec = self._settings.datasets[request.dataset]
        try:
            tables = PageTables(spec, request)
        except RowsError as exc:
            raise self._rows_failed(exc, spec, spec.path, None) from None
        params = self._params(spec, request)
        pages = self._client.paginate(
            spec.path,
            params=params,
            paginator=make_paginator(spec.paging, self._origin),
            data_selector=spec.records or "$",
            hooks=self._hooks,
        )
        number = 0
        try:
            while True:
                started = time.perf_counter()
                try:
                    page = next(pages)
                except StopIteration:
                    break
                except Exception as exc:
                    raise self._failed(exc, spec.name, spec.path) from None
                number += 1
                response = page.response
                path = self._path_of(response.url, spec.path)
                try:
                    table = tables.page(self._rows(spec, response))
                except RowsError as exc:
                    raise self._rows_failed(exc, spec, path, number) from None
                self._log.debug(
                    "page read",
                    source=self.source_id,
                    dataset=spec.name,
                    path=path,
                    page=number,
                    status=response.status_code,
                    rows=table.num_rows if table is not None else 0,
                    attempts=self._attempts,
                    elapsed_ms=_elapsed_ms(started),
                )
                if table is not None:
                    yield Piece(table, f"page {number}")
            if not tables.produced:
                try:
                    yield Piece(tables.empty())
                except RowsError as exc:
                    raise self._rows_failed(exc, spec, spec.path, None) from None
        finally:
            pages.close()

    def _params(self, spec: HttpDataset, request: ReadRequest) -> dict[str, Any]:
        # A fresh dict per read: the paginators write their own parameters into it.
        params: dict[str, Any] = dict(spec.params)
        if spec.since is not None and request.since is not None:
            params[spec.since.param] = self._since_value(spec, request.since)
        return params

    def _since_value(self, spec: HttpDataset, since: Any) -> str | int | float:
        """`since` as the source expects it; rounded down, so no newer row is skipped."""
        assert spec.since is not None
        kind = spec.since.format
        numeric = isinstance(since, int | float) and not isinstance(since, bool)
        if numeric and kind != "iso":
            # Already a count since the epoch, in the unit the source expects.
            return since
        try:
            moment = as_utc_datetime(since)
        except ValueError as exc:
            raise self._error(
                SourceConfigError,
                f"since cannot be sent to the source as {kind}: {exc}",
                dataset=spec.name,
                option=f"datasets.{spec.name}.since.format",
            ) from None
        if kind == "epoch_s":
            return (moment - _EPOCH) // timedelta(seconds=1)
        if kind == "epoch_ms":
            return (moment - _EPOCH) // timedelta(milliseconds=1)
        return moment.isoformat().replace("+00:00", "Z")

    def _rows(self, spec: HttpDataset, response: Response) -> list[Any]:
        """The list of rows in a response, which must be exactly where `records` says.

        dlt's extraction turns a missing path into no rows and an object into a
        list of one, so the body is looked at again here.
        """
        body = response.json()
        if spec.records is None:
            if not isinstance(body, list):
                raise RowsError(
                    "the response is not a list of rows; name where the rows are with records"
                )
            return body
        found = jsonpath.find_values(spec.records, body)
        if len(found) != 1 or not isinstance(found[0], list):
            raise RowsError(f"the response holds no list of rows at {spec.records!r}")
        return found[0]

    # -- clock ---------------------------------------------------------------

    def _now(self) -> datetime:
        self._check_open()
        clock = self._settings.clock
        assert clock is not None
        started = time.perf_counter()
        try:
            response = self._client.get(clock.path, hooks=self._hooks)
            body = response.json()
        except Exception as exc:
            raise self._failed(exc, None, clock.path, action="reading the clock") from None
        path = self._path_of(response.url, clock.path)
        found = jsonpath.find_values(clock.field, body) if isinstance(body, dict | list) else []
        text = found[0] if len(found) == 1 else None
        moment = None
        if isinstance(text, str):
            try:
                moment = datetime.fromisoformat(text.strip())
            except ValueError:
                moment = None
        if moment is None or moment.utcoffset() is None:
            raise self._error(
                SourceReadError,
                f"the clock at {path} gives no ISO 8601 time with an offset at {clock.field!r}",
                path=path,
                status=response.status_code,
            )
        self._log.debug(
            "clock read",
            source=self.source_id,
            path=path,
            status=response.status_code,
            time=moment.isoformat(),
            attempts=self._attempts,
            elapsed_ms=_elapsed_ms(started),
        )
        return moment

    # -- retries -------------------------------------------------------------

    def _should_retry(self, response: Response | None, exception: BaseException | None) -> bool:
        """Called by dlt's client after every attempt; True asks it to try again.

        It must not raise: it runs inside the client's retry loop.
        """
        retry = _retryable(response, exception, self._settings.max_wait_s)
        if not retry:
            self._attempts = self._failed_attempts + 1
            self._failed_attempts = 0
            return False
        self._failed_attempts += 1
        self._attempts = self._failed_attempts
        if self._failed_attempts >= self._settings.max_attempts:
            # The client stops here; the failure is reported by the caller.
            self._failed_attempts = 0
            return True
        wait = retry_after_s(response) if response is not None else None
        self._log.warn(
            "request failed; the client tries again",
            source=self.source_id,
            path=self._path_of(url_of(response, exception), None),
            status=response.status_code if response is not None else None,
            error=None if response is not None else self._describe(exception),
            attempt=self._failed_attempts,
            max_attempts=self._settings.max_attempts,
            retry_after_s=wait,
        )
        return True

    # -- errors --------------------------------------------------------------

    def _failed(
        self,
        exc: BaseException,
        dataset: str | None,
        configured_path: str,
        *,
        action: str | None = None,
    ) -> SourceError:
        if isinstance(exc, SourceError):
            return exc
        response = getattr(exc, "response", None)
        path = self._path_of(url_of(response, exc), configured_path)
        what = action or f"reading dataset {dataset!r}"
        return self._error(
            self._classify(exc),
            f"{what} failed: GET {path}: {self._describe(exc)}",
            dataset=dataset,
            cause=exc,
            path=path,
            **self._detail_for(exc),
        )

    def _rows_failed(
        self, exc: RowsError, spec: HttpDataset, path: str, page: int | None
    ) -> SourceError:
        where = f" (GET {path}, page {page})" if page is not None else ""
        return self._error(
            SourceConfigError if exc.config else SourceReadError,
            f"reading dataset {spec.name!r} failed{where}: {exc}",
            dataset=spec.name,
            path=path,
            page=page,
            columns=exc.columns or None,
        )

    def _classify(self, exc: BaseException) -> type[SourceError]:
        if isinstance(exc, rq.HTTPError) and exc.response is not None:
            status = exc.response.status_code
            if status in (401, 403):
                return SourceAuthFailed
            if status == 429:
                return SourceRateLimited
            if status >= 500:
                return SourceUnavailable
            return SourceReadError
        if isinstance(exc, ForeignTarget):
            return SourceReadError
        if isinstance(exc, RETRIED_ERRORS):
            return SourceUnavailable
        return SourceReadError

    def _detail_for(self, exc: BaseException) -> dict[str, Any]:
        detail: dict[str, Any] = {"attempts": self._attempts}
        response = getattr(exc, "response", None)
        if isinstance(response, Response):
            detail["status"] = response.status_code
            if response.status_code == 429:
                wait = retry_after_s(response)
                if wait is not None:
                    detail["retry_after_s"] = wait
        return detail

    def _describe(self, exc: BaseException | None) -> str:
        """The cause of a failure without the address, which requests and dlt put in messages."""
        if exc is None:
            return ""
        return self._redact(_summary(exc, self._settings.timeout_s))[:_CAUSE_CHARS]

    def _path_of(self, url: str | None, fallback: str | None) -> str | None:
        """The path of a request, never its query; the location's part may be a secret."""
        if not url:
            return self._redact(fallback) if fallback is not None else None
        return self._redact(urlsplit(url).path or "/")

    # -- lifecycle -----------------------------------------------------------

    def _release(self) -> None:
        if self._session is not None:
            self._session.close()


class ClockedHttpConnector(HttpConnector):
    """An HTTP connector whose source tells the time, so it is a `TimeSource` too."""

    def now(self) -> datetime:
        """One GET of the clock path; the named field as an aware datetime."""
        return self._now()


def _retryable(
    response: Response | None, exception: BaseException | None, max_wait_s: float
) -> bool:
    """What dlt's default policy retries, except a 429 that asks for longer than `max_wait_s`.

    Such a 429 fails at once with its `Retry-After`, so that the caller comes back
    when the source allows it instead of being refused again after a shorter wait.
    """
    if exception is not None and not isinstance(exception, rq.HTTPError):
        return isinstance(exception, RETRIED_ERRORS)
    if response is None:
        return False
    status = response.status_code
    if status == 429:
        wait = retry_after_s(response)
        return wait is None or wait <= max_wait_s
    return status >= 500


def _summary(exc: BaseException, timeout_s: float) -> str:
    if isinstance(exc, rq.HTTPError) and exc.response is not None:
        reason = exc.response.reason
        if isinstance(reason, bytes):
            reason = reason.decode("iso-8859-1")
        return f"HTTP {exc.response.status_code} {reason or ''}".strip()
    if isinstance(exc, ForeignTarget):
        return str(exc)
    if isinstance(exc, rq.ConnectTimeout):
        return f"no connection within {timeout_s:g} s"
    if isinstance(exc, rq.ReadTimeout):
        return f"no answer within {timeout_s:g} s"
    if isinstance(exc, rq.JSONDecodeError):
        return f"the response is not JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"
    if isinstance(exc, rq.RequestException):
        # requests and urllib3 name the host and port in their messages; only the
        # innermost operating-system reason is kept.
        return f"{type(exc).__name__}: {_root_reason(exc)}"
    if type(exc) is ValueError:
        # dlt's paginators raise these when a response lacks what the paging
        # options name, and append the whole response body, which is left out.
        return f"the response does not fit the paging options: {str(exc).split(' got `')[0]}"
    return f"{type(exc).__name__}: {exc}"


def _root_reason(exc: BaseException) -> str:
    reason = type(exc).__name__
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        strerror = getattr(current, "strerror", None)
        if isinstance(strerror, str) and strerror:
            reason = strerror
        elif isinstance(current, TimeoutError):
            reason = "timed out"
        current = current.__cause__ or current.__context__
    return reason


def open_http(
    source_id: str,
    location: str,
    resolved: str,
    settings: HttpOptions,
    redactor: Redactor,
) -> HttpConnector:
    """Build the client for the source; nothing is requested until a dataset is read."""
    cls = ClockedHttpConnector if settings.clock is not None else HttpConnector
    connector = cls(source_id, location, redactor, settings)
    connector._connect(resolved)
    return connector
