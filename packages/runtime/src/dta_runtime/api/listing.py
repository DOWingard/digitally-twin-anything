"""What the routes over the twin's registries share: pages of records, answers of
functions, and jobs of the compute lane started by a request."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any

import pyarrow as pa
from dta_core.store import VersionRef
from fastapi import Header, Query
from starlette.requests import Request
from starlette.responses import Response

from ..protocol import answer_document
from .formats import Format, json_response, negotiate, without_nan
from .formats import rows as table_rows
from .jobs import Work
from .paging import next_cursor, offset_of, paged_response
from .params import JSON_LIMIT_DEFAULT, JSON_LIMIT_MAX, page_limit
from .problems import ApiError
from .running import Ran
from .services import Services
from .tables import version_json

JOBS_PATH = "/api/v1/lifecycle/jobs"
ANSWER_KEY = b"dta:answer"
# Names a run of a function may not use for its own arguments in the query string.
RESERVED_QUERY = frozenset({"at", "branch", "format", "limit", "cursor"})

ListLimit = Annotated[
    int,
    Query(ge=1, le=JSON_LIMIT_MAX, description=f"Most records returned, at most {JSON_LIMIT_MAX}"),
]
LIST_LIMIT_DEFAULT = JSON_LIMIT_DEFAULT
ComputeKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Names the work asked for, 1 to 200 printable ASCII characters. Asking "
        "again with the same key gives back the job made for it; with another request the "
        "key is refused (`job.key_reused`). The last 1000 keys are remembered",
        min_length=1,
        max_length=200,
        pattern=r"^[\x20-\x7e]+$",
        examples=["evaluate:2026-01-19"],
    ),
]
JOB_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The job already made for this key, or for the same work while it "
        "is queued or running",
        "headers": {"Location": {"description": "Address of the job"}},
    },
    202: {
        "description": "A job was made",
        "headers": {"Location": {"description": "Address of the job"}},
    },
}


def records_page(
    rows: Sequence[Mapping[str, Any]],
    document: Callable[[Mapping[str, Any]], dict[str, Any]],
    *,
    kind: str,
    query: str,
    version: VersionRef,
    position: Any,
    limit: int,
) -> Response:
    """One page of records that were read whole, in the order given."""
    offset = offset_of(position)
    page = rows[offset : offset + limit]
    following = offset + limit if offset + limit < len(rows) else None
    return json_response(
        {
            "data": [document(row) for row in page],
            "next_cursor": next_cursor(kind, query, version, following),
            "version": version_json(version),
        },
        version,
    )


def start_job(
    services: Services, kind: str, params: Mapping[str, Any], work: Work, key: str | None
) -> Response:
    """Queue work in the compute lane and answer at once with its job."""
    document, created = services.compute_jobs.start(kind, params, work, key)
    return json_response(
        {"job": document},
        headers={"Location": f"{JOBS_PATH}/{document['id']}"},
        status=202 if created else 200,
    )


async def call_arguments(request: Request, *, query: bool) -> dict[str, Any]:
    """The arguments of one call of a function: the JSON object of the body and, where a
    call may be written without a body, the query string. A name is given in one place."""
    raw = await request.body()
    given: dict[str, Any] = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ApiError("the body is not JSON", code="api.bad_request") from exc
        if not isinstance(parsed, dict):
            raise ApiError(
                "the body is a JSON object of the function's arguments", code="api.bad_request"
            )
        given = parsed
    if not query:
        return given
    for name in dict.fromkeys(request.query_params.keys()):
        if name in RESERVED_QUERY:
            continue
        if name in given:
            raise ApiError(
                f"{name} is given in the body and in the query string", code="api.bad_request"
            )
        values = request.query_params.getlist(name)
        given[name] = values[0] if len(values) == 1 else values
    return given


def answer_response(
    request: Request,
    ran: Ran,
    version: VersionRef,
    *,
    fmt: Format | None,
    limit: int | None,
    position: Any,
    kind: str,
    query: str,
) -> Response:
    """The answer of a run: its document, or its table as asked for with the document
    beside the rows (JSON) or in the schema metadata (Arrow, Parquet)."""
    answer = ran.answer
    value = answer.value
    plain = (
        answer
        if not isinstance(value, pa.Table)
        else type(answer)(
            value=None, unit=answer.unit, interval=answer.interval, notes=answer.notes
        )
    )
    document = answer_document(
        plain,
        version=version_json(version),
        function=ran.function,
        model=ran.model,
        provisional=ran.provisional,
        reasons=ran.reasons,
    )
    if ran.wrote is not None:
        document["wrote"] = version_json(ran.wrote)
        document["rows_written"] = ran.rows_written
    if not isinstance(value, pa.Table):
        return json_response(document, version)
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    if chosen != "json":
        metadata = dict(value.schema.metadata or {})
        metadata[ANSWER_KEY] = json.dumps(
            {name: found for name, found in document.items() if name != "value"}, default=str
        ).encode("utf-8")
        return paged_response(
            value.replace_schema_metadata(metadata),
            chosen,
            version,
            per_page,
            position,
            kind,
            query,
        )
    offset = offset_of(position)
    assert per_page is not None
    page = without_nan(value.slice(offset, per_page))
    following = offset + per_page if offset + per_page < value.num_rows else None
    document["value"] = table_rows(page)
    document["next_cursor"] = next_cursor(kind, query, version, following)
    return json_response(document, version)
