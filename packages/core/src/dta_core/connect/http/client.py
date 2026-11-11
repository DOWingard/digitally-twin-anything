"""The HTTP side of the connector, on dlt's REST client and its paginators.

Retries, back-off, waiting out `Retry-After` and paging are dlt's. What is
added here keeps the configured headers, which usually carry a credential,
with the source: a redirect or a next-page link to another scheme, host or
port is refused before anything is sent there, and dlt's own request log
line, which holds the whole address and at debug level the headers, is not
written.
"""

from __future__ import annotations

from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

from dlt.sources.helpers.requests import Client
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import (
    BasePaginator,
    HeaderLinkPaginator,
    JSONLinkPaginator,
    JSONResponseCursorPaginator,
    OffsetPaginator,
    PageNumberPaginator,
    SinglePagePaginator,
)
from requests import PreparedRequest, Request, Response, Session
from requests import exceptions as rq

from .settings import HttpOptions, Paging

# Multiplier of dlt's exponential back-off (1, 2, 4... seconds), capped by `max_wait_s`.
BACKOFF_FACTOR_S = 1.0
USER_AGENT = "dta-connect"
# Errors after which dlt's default policy tries again; kept the same here.
RETRIED_ERRORS = (rq.ConnectionError, rq.Timeout, rq.ChunkedEncodingError)

RetryPredicate = Callable[[Response | None, BaseException | None], bool]


class Origin(NamedTuple):
    scheme: str
    host: str
    port: int | None


def origin_of(url: str) -> Origin:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return Origin(scheme, (parts.hostname or "").lower(), port)


class ForeignTarget(rq.RequestException):
    """A redirect or next-page link points away from the source's scheme, host and port."""


class QuietRESTClient(RESTClient):
    def _log_request(self, request: Request, prepared_url: str) -> None:
        # dlt logs the full address here, and at debug level every header; the
        # connector logs its requests itself, by path only.
        return None


def make_session(settings: HttpOptions, should_retry: RetryPredicate) -> Session:
    """A requests session whose `send` dlt wraps with retries and back-off.

    dlt's default retry conditions are switched off so that `should_retry`
    sees the outcome of every attempt; it decides whether to try again and
    dlt does the waiting, honouring `Retry-After`.
    """
    client = Client(
        request_timeout=settings.timeout_s,
        raise_for_status=False,
        status_codes=(),
        exceptions=(),
        request_max_attempts=settings.max_attempts,
        retry_condition=should_retry,
        request_backoff_factor=BACKOFF_FACTOR_S,
        request_max_retry_delay=settings.max_wait_s,
        respect_retry_after_header=True,
    )
    session = client.session
    session.headers["User-Agent"] = USER_AGENT
    return session


def response_guard(origin: Origin) -> Callable[..., None]:
    """A response hook that refuses a redirect elsewhere and raises on an error status.

    requests runs response hooks on every attempt and on every redirect before
    following it, so a refused redirect is never requested.
    """

    def check(response: Response, *args: Any, **kwargs: Any) -> None:
        if response.is_redirect:
            target = urljoin(response.url, response.headers.get("location", ""))
            if origin_of(target) != origin:
                raise ForeignTarget(
                    "the source redirected to another host, which is not followed so that "
                    "the configured headers stay with the source",
                    response=response,
                )
            return
        if response.status_code >= 400:
            raise rq.HTTPError(f"HTTP {response.status_code}", response=response)

    return check


class _SameOrigin:
    """Follows a next-page link only to the source's own scheme, host and port."""

    origin: Origin

    def update_request(self, request: Request) -> None:
        super().update_request(request)  # type: ignore[misc]
        if request.url and origin_of(request.url) != self.origin:
            raise ForeignTarget(
                "a next-page link points to another host, which is not followed so that "
                "the configured headers stay with the source"
            )


class _SameOriginHeaderLinks(_SameOrigin, HeaderLinkPaginator):
    def __init__(self, origin: Origin) -> None:
        super().__init__()
        self.origin = origin


class _SameOriginJSONLinks(_SameOrigin, JSONLinkPaginator):
    def __init__(self, origin: Origin, next_url_path: str) -> None:
        super().__init__(next_url_path=next_url_path)
        self.origin = origin


def make_paginator(paging: Paging, origin: Origin) -> BasePaginator:
    """A fresh dlt paginator for one read."""
    if paging.type == "cursor":
        return JSONResponseCursorPaginator(
            cursor_path=paging.cursor_path or "", cursor_param=paging.cursor_param
        )
    if paging.type == "page":
        return PageNumberPaginator(
            base_page=paging.first_page,
            page=paging.first_page,
            page_param=paging.page_param,
            total_path=paging.total_path,
            stop_after_empty_page=True,
        )
    if paging.type == "offset":
        return OffsetPaginator(
            limit=paging.limit or 1,
            offset=0,
            offset_param=paging.offset_param,
            limit_param=paging.limit_param,
            total_path=paging.total_path,
            stop_after_empty_page=True,
        )
    if paging.type == "link":
        if paging.next_url_path is not None:
            return _SameOriginJSONLinks(origin, paging.next_url_path)
        return _SameOriginHeaderLinks(origin)
    return SinglePagePaginator()


def retry_after_s(response: Response) -> float | None:
    """The wait a response's `Retry-After` asks for, in seconds; None when absent or unreadable.

    A date is measured against the response's own `Date` header, not the local
    clock, so that a skewed clock on either side does not change the wait.
    """
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(int(value))
    sent = response.headers.get("Date")
    if not sent:
        return None
    try:
        wait = (parsedate_to_datetime(value) - parsedate_to_datetime(sent)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0.0, wait)


def request_of(
    response: Response | None, exception: BaseException | None
) -> PreparedRequest | None:
    if response is not None:
        return response.request
    return getattr(exception, "request", None)


def url_of(response: Response | None, exception: BaseException | None) -> str | None:
    if response is not None and response.url:
        return response.url
    request = request_of(response, exception)
    return request.url if request is not None else None
