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

    `extra` holds members added to the problem document beside the standard ones,
    `headers` what the response carries beside it (a `Retry-After`). `status` is given
    only where the code alone does not decide it: a problem of the worker is passed on
    with the status the worker gave it.
    """

    code = "api.error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.extra = extra or {}
        self.status = status
        self.headers = headers or {}


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
    "artifact.not_found": 404,
    "artifact.empty": 400,
    "artifact.too_large": 413,
    "twin.no_interval": 409,
    "job.key_reused": 409,
    "worker.unavailable": 503,
    "worker.unreachable": 502,
    "worker.busy": 503,
    "register.code_missing": 409,
    # What the manifest's checks call a function, a family or a property that cannot be.
    "ref.function_entry": 400,
    "ref.model_family": 400,
    "ontology.unknown_type": 400,
    "ontology.unknown_property": 400,
    "ontology.function_param": 400,
    "function.not_found": 404,
    "function.exists": 409,
    "function.from_manifest": 409,
    "function.disabled": 409,
    "function.param_unknown": 400,
    "function.param_fixed": 400,
    "function.param_missing": 400,
    "function.bad_param": 400,
    "function.bad_unit": 400,
    "function.timeout": 504,
    "function.unavailable": 503,
    "function.unknown_entry": 500,
    "function.bad_entry": 422,
    "function.bad_result": 422,
    "function.failed": 500,
    "function.memory": 500,
    "model.not_found": 404,
    "model.unknown_spec": 404,
    "model.exists": 409,
    "model.from_manifest": 409,
    "model.none_active": 409,
    "model.not_fitted": 409,
    "model.not_interventional": 409,
    "model.unknown_family": 400,
    "model.bad_family": 400,
    "model.bad_params": 400,
    "model.bad_frame": 400,
    "model.bad_state": 409,
    "model.failed": 500,
    "question.not_found": 404,
    "scenario.bad_request": 400,
    "scenario.not_interventional": 409,
    "scenario.not_found": 404,
    "scenario.running": 409,
    "scenario.already_run": 409,
    "scenario.deleted": 410,
    "scenario.timeout": 504,
    "scenario.bad_prediction": 502,
    "derived.observed_refused": 403,
    "derived.live_refused": 409,
    "derived.unknown_source": 404,
    "derived.bad_inputs_version": 400,
    "derived.mapped_property": 409,
    "derived.too_large": 413,
    "training.bad_spec": 400,
    "training.too_large": 413,
    "clock.unavailable": 503,
    "fidelity.not_live": 409,
    "fidelity.unsupported": 409,
    "fidelity.out_of_order": 409,
    "fidelity.not_found": 404,
    "fidelity.bad_request": 400,
    "fidelity.conflict": 409,
    "action.not_found": 404,
    "action.rejected": 422,
    "action.key_reused": 409,
    "action.record_not_found": 404,
    "action.not_pending": 409,
    "action.not_executable": 409,
    "action.loop_not_found": 404,
    "action.loop_orphaned": 409,
    "action.loop_disabled": 409,
    "action.policy_failed": 502,
    "action.bad_policy_answer": 502,
    "action.bad_state": 500,
    "action.executor_invalid": 500,
}

# Server errors whose message is the caller's to read: it says what to do (wait, give the
# function more time, start the worker), and holds nothing of the server's inside.
PUBLIC_SERVER_CODES = frozenset(
    {
        "worker.unavailable",
        "worker.unreachable",
        "worker.busy",
        "function.timeout",
        "function.unavailable",
        "function.failed",
        "function.memory",
        "model.failed",
        "scenario.timeout",
        "scenario.bad_prediction",
        "clock.unavailable",
        # What the policy of a control loop did; its own text stays in the log.
        "action.policy_failed",
        "action.bad_policy_answer",
    }
)
# Members of an action's refusal that name the record it is about.
_ACTION_MEMBERS = ("record",)

# What a server error tells the client; the cause stays in the logs.
_SERVER_DETAIL = {
    503: "the twin's store cannot be reached at the moment; retry later",
    500: "the request failed on the server; quote the trace id when reporting it",
}


def status_of(error: DtaError) -> int:
    if isinstance(error, ApiError) and error.status is not None:
        return error.status
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
    extra = error.extra if isinstance(error, ApiError) else None
    if error.code.startswith("action."):
        extra = {name: error.detail[name] for name in _ACTION_MEMBERS if name in error.detail}
    headers = (error.headers or None) if isinstance(error, ApiError) else None
    if status >= 500 and error.code in PUBLIC_SERVER_CODES:
        log.warn("request failed", code=error.code, status=status, reason=error.message)
        return problem(status, error.code, error.message, headers=headers, extra=extra)
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
    elif isinstance(error.detail.get("problems"), list):
        # Every problem of a request that was refused whole, each naming its member.
        errors = [dict(found) for found in error.detail["problems"] if isinstance(found, dict)]
    elif isinstance(error.detail.get("reasons"), list):
        # Every reason a proposed action was rejected for, each naming its member.
        errors = [dict(found) for found in error.detail["reasons"] if isinstance(found, dict)]
    return problem(status, error.code, error.message, errors=errors, headers=headers, extra=extra)


def internal_error_response() -> Response:
    return problem(500, INTERNAL, _SERVER_DETAIL[500])
