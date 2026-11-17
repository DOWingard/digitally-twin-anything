"""Row blocks of a cursor-paged API listing, as an infinite-scrolling grid asks for them.

The grid asks for rows by offset (`startRow`, `endRow`); the API pages with
opaque keyset cursors. The cursors met while reading are remembered per query,
so the next block costs one call; a jump ahead reads forward from the nearest
known cursor. Only the asked block leaves the server for the browser.

A listing that is not pinned to a version (the sync runs, for one) is read
under a generation the caller names: cursors of one generation are never
used to read another, so a grid asked to read its rows again after the
listing changed starts from the first page instead of resuming a cursor
issued for what the listing was before.

A table that a POST answers (the answer to a question) is paged the same way:
the body is part of the query, and each page is asked for again with the
cursor. A caller that has read the first page itself hands its cursor on
(`seed`), so that page is not asked for twice.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from dta_core.log import Log

from .client import ApiClient, ApiProblem, Params, query_pairs

log = Log("ui.paging")

# The API's largest JSON page.
API_PAGE_MAX = 10_000
BAD_CURSOR = "api.bad_cursor"
QUERIES_KEPT = 256
CURSORS_PER_QUERY = 5_000


@dataclass(frozen=True)
class Block:
    rows: list[dict[str, Any]]
    # Number of rows of the whole listing, once its end has been read.
    last_row: int | None
    # The other members of the last page read, such as a `total`.
    extra: dict[str, Any] = field(default_factory=dict)


Key = tuple[str, str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...], str]


@dataclass(frozen=True)
class Posted:
    """A table answered to a POST: the body sent, the member of the answer holding the
    rows, and how long the API may take."""

    body: Mapping[str, Any]
    rows_member: str = "data"
    timeout: httpx.Timeout | None = None


class PagedReader:
    def __init__(self, client: ApiClient, *, queries: int = QUERIES_KEPT) -> None:
        self._client = client
        self._queries = queries
        self._cursors: OrderedDict[Key, dict[int, str]] = OrderedDict()
        self._lock = threading.Lock()

    def block(
        self,
        route: str,
        *,
        path: Mapping[str, str] | None,
        params: Params,
        start: int,
        end: int,
        page_max: int = API_PAGE_MAX,
        generation: str = "",
        posted: Posted | None = None,
    ) -> Block:
        """Rows `start` to `end` (excluded) of the listing `route` with `params`.

        `page_max` is the most rows the route gives in one page; `generation`
        names what the listing was when the grid was last asked to read it;
        `posted`, for a table answered to a POST, what is sent and where the
        rows are in the answer.
        """
        if start < 0 or end <= start:
            raise ApiProblem(
                code="ui.bad_block",
                title="Rows asked for are out of range",
                detail=f"rows {start} to {end} cannot be read",
            )
        key = self._key(route, path, params, generation, posted)
        try:
            return self._read(key, route, path, params, start, end, page_max, posted)
        except ApiProblem as problem:
            if problem.code != BAD_CURSOR or not self._forget(key):
                raise
            # A cursor of a listing that has changed since (a file the API reads that was
            # written again) is refused; the block is read again from the first page.
            log.warn(
                "a remembered cursor was refused; the block is read from the start",
                route=route,
                start=start,
                code=problem.code,
            )
            return self._read(key, route, path, params, start, end, page_max, posted)

    def seed(
        self,
        route: str,
        *,
        path: Mapping[str, str] | None,
        params: Params,
        position: int,
        cursor: str,
        generation: str = "",
        posted: Posted | None = None,
    ) -> None:
        """Remember the cursor of a page the caller read itself: the rows from `position`
        on are read from it."""
        self._remember(self._key(route, path, params, generation, posted), position, cursor)

    @staticmethod
    def _key(
        route: str,
        path: Mapping[str, str] | None,
        params: Params,
        generation: str,
        posted: Posted | None,
    ) -> Key:
        body = json.dumps(posted.body, sort_keys=True, default=str) if posted else ""
        return (
            route,
            generation,
            tuple(sorted((path or {}).items())),
            tuple(query_pairs(params)),
            body,
        )

    def _page(
        self,
        route: str,
        path: Mapping[str, str] | None,
        params: Params,
        posted: Posted | None,
    ) -> tuple[Any, dict[str, Any]]:
        """One page: its rows and the whole answer."""
        if posted is None:
            document = self._client.get(route, path=path, params=params)
            return document.get("data"), document
        document = self._client.post(
            route, path=path, params=params, body=posted.body, timeout=posted.timeout
        )
        return document.get(posted.rows_member), document

    def _read(
        self,
        key: Key,
        route: str,
        path: Mapping[str, str] | None,
        params: Params,
        start: int,
        end: int,
        page_max: int,
        posted: Posted | None = None,
    ) -> Block:
        position, cursor = self._nearest(key, start)
        if position < start:
            log.debug("reading forward to a block", route=route, skip=start - position)
        rows: list[dict[str, Any]] = []
        while True:
            hop = min(end - position, page_max)
            data, document = self._page(
                route, path, {**params, "limit": hop, "cursor": cursor}, posted
            )
            following = document.get("next_cursor")
            if not isinstance(data, list):
                raise ApiProblem(
                    code="ui.bad_response",
                    title="Unexpected answer from the API",
                    detail="a listing came back without rows",
                )
            if position + len(data) > start:
                rows.extend(data[max(0, start - position) :])
            position += len(data)
            member = posted.rows_member if posted else "data"
            extra = {name: value for name, value in document.items() if name != member}
            if not following or not data:
                if following and not data:
                    log.warn("the API returned an empty page with a cursor", route=route)
                return Block(rows[: end - start], last_row=position, extra=extra)
            self._remember(key, position, following)
            cursor = following
            if position >= end:
                return Block(rows[: end - start], last_row=None, extra=extra)

    def _nearest(self, key: Key, start: int) -> tuple[int, str | None]:
        with self._lock:
            known = self._cursors.get(key)
            if known is None:
                return 0, None
            self._cursors.move_to_end(key)
            before = [position for position in known if position <= start]
            if not before:
                return 0, None
            position = max(before)
            return position, known[position]

    def _forget(self, key: Key) -> bool:
        """Drop the cursors remembered for a query; whether there were any."""
        with self._lock:
            return self._cursors.pop(key, None) is not None

    def _remember(self, key: Key, position: int, cursor: str) -> None:
        with self._lock:
            known = self._cursors.setdefault(key, {})
            self._cursors.move_to_end(key)
            if len(known) < CURSORS_PER_QUERY:
                known[position] = cursor
            while len(self._cursors) > self._queries:
                self._cursors.popitem(last=False)


def read_all(
    client: ApiClient, route: str, params: Params, *, cap: int = 1_000, page: int = 200
) -> list[dict[str, Any]]:
    """Every row of a short listing (the questions, the scenarios), page after page, up to
    `cap` rows."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(rows) < cap:
        document = client.get(route, params={**params, "limit": page, "cursor": cursor})
        rows.extend(document.get("data") or [])
        cursor = document.get("next_cursor")
        if not cursor:
            return rows
    log.warn("a listing was cut short at its cap", route=route, rows=len(rows), cap=cap)
    return rows
