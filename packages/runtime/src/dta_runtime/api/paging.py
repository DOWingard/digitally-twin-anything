"""Where a page starts and how the next one is found.

A cursor carries the version of the first page, so following pages read the
same version even after new versions arrive, and a fingerprint of the query,
so it is not used with another one.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from dta_core.store import VersionRef
from starlette.responses import Response

from .formats import Format, table_response
from .problems import ApiError
from .services import Services
from .tables import decode_cursor, encode_cursor, page


def start(
    services: Services, cursor: str | None, kind: str, query: str, at: str, branch: str
) -> tuple[VersionRef, Any]:
    """The version to read and where the page starts: the cursor's, else the selector's."""
    if cursor:
        payload = decode_cursor(cursor, kind, query)
        return services.resolve_cursor(payload), payload.get("p")
    return services.resolve(at, branch), None


def next_cursor(
    kind: str,
    query: str,
    version: VersionRef,
    position: Any | None,
    more: dict[str, Any] | None = None,
) -> str | None:
    if position is None:
        return None
    return encode_cursor(
        {"k": kind, "q": query, "v": version.id, "b": version.branch, "p": position, **(more or {})}
    )


def start_range(
    services: Services, cursor: str | None, kind: str, query: str, selector: str, branch: str
) -> tuple[list[VersionRef], str, Any]:
    """The versions of a range, written with commit ids, and where the page starts."""
    if cursor:
        payload = decode_cursor(cursor, kind, query)
        pinned, pinned_branch = payload.get("r"), payload.get("b")
        if not isinstance(pinned, str) or not isinstance(pinned_branch, str):
            raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
        versions, pinned = services.resolve_range(pinned, pinned_branch)
        return versions, pinned, payload.get("p")
    versions, pinned = services.resolve_range(selector, branch)
    return versions, pinned, None


def offset_of(position: Any) -> int:
    offset = 0 if position is None else position
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
    return offset


def paged_response(
    table: pa.Table,
    chosen: Format,
    version: VersionRef,
    per_page: int | None,
    position: Any,
    kind: str,
    query: str,
    extra: dict[str, Any] | None = None,
    more: dict[str, Any] | None = None,
) -> Response:
    """A table computed whole, served a page at a time by offset; `extra` goes into the
    JSON document, `more` into the next cursor."""
    offset = offset_of(position)
    following = None
    if per_page is not None:
        table, following = page(table, offset, per_page)
    elif offset:
        table = table.slice(offset)
    return table_response(
        table, chosen, version, next_cursor(kind, query, version, following, more), extra
    )
