"""What the worker answers when a run does not give a result: problem documents.

Every error leaves the worker as `application/problem+json` with a stable `code`.
The codes the listening process decides on its own (a token, a body, a full pool)
have a status of their own. A run's code comes from the process that ran it, which
may be running code the twin brought: the status of such a code is looked up here,
never taken from the process, and a code the worker does not know is a handler's
`DtaError` passed through as 422.
"""

from __future__ import annotations

import json
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import current_trace_id
from starlette.responses import Response

PROBLEM_MEDIA = "application/problem+json"
JSON_MEDIA = "application/json"
ARROW_MEDIA = "application/vnd.apache.arrow.stream"

UNAUTHORIZED = "worker.unauthorized"
BAD_REQUEST = "worker.bad_request"
BUSY = "worker.busy"
NOT_FOUND = "worker.not_found"
METHOD_NOT_ALLOWED = "worker.method_not_allowed"
INTERNAL = "worker.internal"
HANDLER_MISSING = "worker.handler_missing"
API_UNREACHABLE = "worker.api_unreachable"
TIMEOUT = "function.timeout"
FAILED = "function.failed"
MEMORY = "function.memory"
BAD_STATE = "model.bad_state"

# Codes only the listening process gives.
LISTENER_STATUS: dict[str, int] = {
    UNAUTHORIZED: 401,
    BAD_REQUEST: 400,
    NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
    INTERNAL: 500,
    BUSY: 503,
}
# Codes a run ends with; any other code a run gives is a handler's own, answered 422.
RUN_STATUS: dict[str, int] = {
    TIMEOUT: 504,
    FAILED: 500,
    MEMORY: 500,
    HANDLER_MISSING: 501,
    BAD_STATE: 422,
    # The run could not reach the API, or the API answered without a problem document.
    API_UNREACHABLE: 502,
}
HANDLER_ERROR_STATUS = 422


class WorkerError(DtaError):
    """A request the listening process refuses, or a run that ended without a result."""

    code = INTERNAL

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.headers = headers or {}
        # Members added to the problem document beside the standard ones.
        self.extra = extra or {}


def run_status(code: str) -> int:
    return RUN_STATUS.get(code, HANDLER_ERROR_STATUS)


def title_of(code: str) -> str:
    """`function.timeout` -> `Timeout`."""
    words = code.rsplit(".", 1)[-1].replace("_", " ")
    return words[:1].upper() + words[1:]


def problem(
    status: int,
    code: str,
    detail: str,
    *,
    trace_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Response:
    document: dict[str, Any] = {
        "type": f"urn:dta:error:{code}",
        "title": title_of(code),
        "status": status,
        "code": code,
        "detail": detail,
        "trace_id": trace_id or current_trace_id(),
    }
    for key, value in (extra or {}).items():
        document.setdefault(key, value)
    return Response(
        content=json.dumps(document, default=str, separators=(",", ":")),
        status_code=status,
        media_type=PROBLEM_MEDIA,
        headers=headers,
    )
