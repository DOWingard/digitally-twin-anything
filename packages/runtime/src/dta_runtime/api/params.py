"""Query parameters shared by many routes, with the descriptions the OpenAPI document shows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from dta_core.schema import UID_MAX_LENGTH
from dta_core.store import LIVE_BRANCH
from fastapi import Path, Query

from .formats import Format
from .problems import ApiError

JSON_LIMIT_DEFAULT = 100
JSON_LIMIT_MAX = 10_000

At = Annotated[
    str,
    Query(
        description="Version to read: `head`, a commit id, `#<seq>`, or an ISO 8601 time with "
        "an offset (the last version at or before it; write `+` as `%2B` or use `Z`)",
        examples=["head", "#2"],
        max_length=200,
    ),
]
Branch = Annotated[
    str,
    Query(description="Branch to read", examples=[LIVE_BRANCH], max_length=100),
]
FormatParam = Annotated[
    Format | None,
    Query(
        alias="format",
        description="Rendering of the table; overrides `Accept` "
        "(`application/vnd.apache.arrow.stream`, `application/vnd.apache.parquet`)",
    ),
]
Limit = Annotated[
    int | None,
    Query(
        ge=1,
        description=f"Most rows returned. JSON: default {JSON_LIMIT_DEFAULT}, at most "
        f"{JSON_LIMIT_MAX}. Arrow and Parquet: all rows unless given",
    ),
]
Cursor = Annotated[
    str | None,
    Query(
        description="`next_cursor` of the previous page. A cursor keeps reading the version, "
        "or the range of versions, of the first page, whatever `at`, `range` and `branch` say",
        max_length=2_000,
    ),
]
Columns = Annotated[
    list[str] | None,
    Query(description="Properties to return, repeated or comma-separated; all if absent"),
]
EdgeTypes = Annotated[
    list[str] | None,
    Query(
        description="Edge types to follow, repeated or comma-separated: link type names and "
        "`Type.property` for references; all if absent"
    ),
]
TypeName = Annotated[
    str, Path(description="Object or link type; an abstract class for listings", max_length=200)
]
Uid = Annotated[str, Path(description="uid of the object", max_length=UID_MAX_LENGTH)]


def split_list(values: Sequence[str] | None) -> list[str] | None:
    """Repeated and comma-separated values as one list; None when nothing was given."""
    if not values:
        return None
    found = [item.strip() for value in values for item in value.split(",") if item.strip()]
    return list(dict.fromkeys(found)) or None


def json_limit(limit: int | None) -> int:
    if limit is None:
        return JSON_LIMIT_DEFAULT
    if limit > JSON_LIMIT_MAX:
        raise ApiError(
            f"limit is at most {JSON_LIMIT_MAX} for JSON; use format=arrow or parquet for more",
            code="api.bad_request",
        )
    return limit


def page_limit(fmt: Format, limit: int | None) -> int | None:
    """Rows per page: capped for JSON, all rows for Arrow and Parquet unless `limit` is given."""
    return json_limit(limit) if fmt == "json" else limit
