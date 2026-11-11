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
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

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


Key = tuple[str, str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]


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
    ) -> Block:
        """Rows `start` to `end` (excluded) of the listing `route` with `params`.

        `page_max` is the most rows the route gives in one page; `generation`
        names what the listing was when the grid was last asked to read it.
        """
        if start < 0 or end <= start:
            raise ApiProblem(
                code="ui.bad_block",
                title="Rows asked for are out of range",
                detail=f"rows {start} to {end} cannot be read",
            )
        key: Key = (
            route,
            generation,
            tuple(sorted((path or {}).items())),
            tuple(query_pairs(params)),
        )
        try:
            return self._read(key, route, path, params, start, end, page_max)
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
            return self._read(key, route, path, params, start, end, page_max)

    def _read(
        self,
        key: Key,
        route: str,
        path: Mapping[str, str] | None,
        params: Params,
        start: int,
        end: int,
        page_max: int,
    ) -> Block:
        position, cursor = self._nearest(key, start)
        if position < start:
            log.debug("reading forward to a block", route=route, skip=start - position)
        rows: list[dict[str, Any]] = []
        while True:
            hop = min(end - position, page_max)
            document = self._client.get(
                route, path=path, params={**params, "limit": hop, "cursor": cursor}
            )
            data = document.get("data")
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
            extra = {name: value for name, value in document.items() if name != "data"}
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
