"""Errors as RFC 9457 problem documents.

Every error leaves the API as `application/problem+json` with a stable
machine-readable `code`; the HTTP status follows from the code alone. A
client error says what was wrong with the request. A server error says only
that it happened and which trace to look up: its cause is in the logs, never
in the response.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log, current_trace_id
from dta_core.store import InvalidInput, ViolationError
from starlette.responses import Response

log = Log("api.error")

PROBLEM_MEDIA = "application/problem+json"
INTERNAL = "api.internal"


class ApiError(DtaError):
    """A request the API refuses for a reason of its own.

    `extra` holds members added to the problem document beside the standard ones.
    """

    code = "api.error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.extra = extra or {}


STATUS_BY_CODE: dict[str, int] = {
    "api.bad_request": 400,
    "api.bad_cursor": 400,
    "api.sort_too_large": 400,
    "api.slice_too_large": 400,
    "api.unauthorized": 401,
    "api.not_found": 404,
    "api.unknown_object": 404,
    "api.method_not_allowed": 405,
    "store.bad_selector": 400,
    "store.bad_filter": 400,
    "store.bad_time": 400,
    "store.naive_datetime": 400,
    "store.bad_branch_name": 400,
    "store.unknown_version": 404,
    "store.unknown_branch": 404,
    "store.unknown_type": 404,
    "store.unknown_property": 404,
    "store.not_installed": 503,
    "store.violation": 422,
    "store.unavailable": 503,
    "store.closed": 503,
    "store.lock_timeout": 503,
    "graph.too_broad": 400,
    "graph.bad_weight": 400,
    "graph.bad_edge_filter": 400,
    "twin.not_registered": 404,
    "twin.not_live": 409,
    "job.not_found": 404,
    "job.closed": 503,
    "sync.unknown_source": 400,
    "sync.replay_unknown": 400,
    "sync.key_reused": 409,
    "sync.busy": 409,
    "sync.run_not_found": 404,
    "sync.rejects_missing": 404,
}

# What a server error tells the client; the cause stays in the logs.
_SERVER_DETAIL = {
    503: "the twin's store cannot be reached at the moment; retry later",
    500: "the request failed on the server; quote the trace id when reporting it",
}


def status_of(error: DtaError) -> int:
    found = STATUS_BY_CODE.get(error.code)
    if found is not None:
        return found
    if isinstance(error, InvalidInput):
        return 400
    return 500


def title_of(code: str) -> str:
    """`store.unknown_type` -> `Unknown type`."""
    words = code.rsplit(".", 1)[-1].replace("_", " ")
    return words[:1].upper() + words[1:]


def problem(
    status: int,
    code: str,
    detail: str,
    *,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Response:
    document: dict[str, Any] = {
        "type": f"urn:dta:error:{code}",
        "title": title_of(code),
        "status": status,
        "detail": detail,
        "code": code,
        "trace_id": current_trace_id(),
    }
    if errors is not None:
        document["errors"] = errors
    # Members of the problem's own kind, such as the job a refusal is about.
    for key, value in (extra or {}).items():
        document.setdefault(key, value)
    return Response(
        content=json.dumps(document, default=str, separators=(",", ":")),
        status_code=status,
        media_type=PROBLEM_MEDIA,
        headers=headers,
    )


def error_response(error: DtaError) -> Response:
    """The problem document for an error raised while serving a request."""
    status = status_of(error)
    if status >= 500:
        log.error(
            "request failed",
            exc=error,
            code=error.code,
            status=status,
            detail=error.detail,
        )
        return problem(status, error.code, _SERVER_DETAIL.get(status, _SERVER_DETAIL[500]))
    log.info("request refused", code=error.code, status=status)
    errors = None
    if isinstance(error, ViolationError):
        errors = [asdict(violation) for violation in error.violations]
    extra = error.extra if isinstance(error, ApiError) else None
    return problem(status, error.code, error.message, errors=errors, extra=extra)


def internal_error_response() -> Response:
    return problem(500, INTERNAL, _SERVER_DETAIL[500])
