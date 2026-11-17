"""The functions of the twin: what it can compute, registering one, and running one."""

from __future__ import annotations

from typing import Annotated, Any

import anyio.to_thread
from dta_core.log import Log
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Path, Request
from starlette.responses import Response

from .. import registry, running
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..formats import json_response
from ..listing import (
    LIST_LIMIT_DEFAULT,
    ListLimit,
    answer_response,
    call_arguments,
    records_page,
)
from ..models import AnswerOut, FunctionList, FunctionOne, FunctionRegistration
from ..paging import start
from ..params import At, Branch, Cursor, FormatParam, Limit
from ..problems import ApiError
from ..services import Services
from ..tables import fingerprint, version_json

router = APIRouter(prefix=f"{API_PREFIX}/functions", tags=["functions"])
log = Log("api.functions")

FunctionId = Annotated[
    str,
    Path(description="Id of the function, or its uid", max_length=190, examples=["f_latest"]),
]
ARGUMENTS_BODY: dict[str, Any] = {
    "requestBody": {
        "required": False,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "description": "The arguments of the call, by name",
                    "additionalProperties": True,
                },
                "examples": {"none": {"value": {}}},
            }
        },
    }
}
RUN_NOTES = {
    400: "An argument the entry does not take, one that is fixed, one that is missing or of "
    "the wrong shape (`function.param_unknown`, `function.param_fixed`, "
    "`function.param_missing`, `function.bad_param`, `function.bad_unit`)",
    404: "No such function (`function.not_found`), version, branch, type or property",
    409: "The function is disabled (`function.disabled`), or a forecast has no active model "
    "(`model.none_active`)",
    422: "Code of the twin's own that cannot be called or gave back something that is not "
    "an answer (`function.bad_entry`, `function.bad_result`), or rows the ontology refuses",
    500: "The function raised (`function.failed`, with the `frames` of its own code), ran "
    "out of memory (`function.memory`), or names an entry the runtime does not have",
    502: "The worker cannot be reached (`worker.unreachable`)",
    503: "The twin has no worker (`worker.unavailable`), the worker is busy (`worker.busy`, "
    "with `Retry-After`), nothing can forecast (`function.unavailable`), or the store "
    "cannot be reached",
    504: "The function did not answer within its latency budget (`function.timeout`)",
}


@router.get(
    "",
    summary="The functions of the twin",
    description="Every registered function as of a version, in uid order: shipped built-ins "
    "with the arguments their registration fixes, and code the twin brought.",
    response_model=FunctionList,
    responses=versioned(400, 404),
)
def list_functions(
    services: ServicesDep,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("functions")
    version, position = start(services, cursor, "functions", query, at, branch)
    rows = registry.rows(services, registry.FUNCTION_TYPE, version)
    return records_page(
        rows,
        registry.function_document,
        kind="functions",
        query=query,
        version=version,
        position=position,
        limit=limit,
    )


@router.post(
    "",
    status_code=201,
    summary="Register a function while the twin runs",
    description="A built-in with the arguments it is fixed to, or code the twin brought "
    "(`module:function` with the hash of its code). Checked as the manifest's functions "
    "are: the entry has to exist, its fixed arguments have to be ones it takes, of the "
    "right shape, naming properties the ontology has. One version, cause `registration`.",
    response_model=FunctionOne,
    responses={
        201: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            404,
            409,
            notes={
                400: "The entry or a fixed argument cannot be (`ref.function_entry`, "
                "`ontology.unknown_type`, `ontology.unknown_property`, "
                "`ontology.function_param`); `errors` lists every problem",
                404: "No artifact is kept under the hash given (`artifact.not_found`)",
                409: "A function of this id exists (`function.exists`)",
            },
        ),
    },
)
def register_function(services: ServicesDep, body: FunctionRegistration) -> Response:
    record, version = registry.add_function(
        services,
        function_id=body.id,
        entry=body.entry,
        params=body.params,
        description=body.description,
        latency_budget=body.latency_budget,
        env=body.env,
        artifact=body.artifact,
    )
    return json_response(
        {"data": registry.function_document(record, True), "version": version_json(version)},
        version,
        status=201,
    )


@router.get(
    "/{id}",
    summary="One function, with the arguments its entry takes",
    description="`parameters` lists what the entry takes and which of them the "
    "registration fixed; a call gives the others.",
    response_model=FunctionOne,
    responses=versioned(400, 404, notes={404: "No such function (`function.not_found`)"}),
)
def one_function(
    services: ServicesDep, id: FunctionId, at: At = "head", branch: Branch = LIVE_BRANCH
) -> Response:
    version = services.resolve(at, branch)
    record = registry.function_or_404(services, id, version)
    return json_response(
        {"data": registry.function_document(record, True), "version": version_json(version)},
        version,
    )


@router.delete(
    "/{id}",
    summary="Disable a function registered while the twin ran",
    description="The function stays in the graph, `disabled`: it is no longer run. A "
    "function the manifest declares is changed through the manifest.",
    response_model=FunctionOne,
    responses=versioned(
        400,
        404,
        409,
        notes={
            404: "No such function (`function.not_found`)",
            409: "The manifest declares it (`function.from_manifest`)",
        },
    ),
)
def delete_function(services: ServicesDep, id: FunctionId) -> Response:
    record, version = registry.disable_function(services, id)
    return json_response(
        {"data": registry.function_document(record), "version": version_json(version)}, version
    )


def run_and_answer(
    request: Request,
    services: Services,
    function_id: str,
    body: dict[str, Any],
    given: dict[str, Any],
    *,
    at: str,
    branch: str,
    limit: int | None,
    cursor: str | None,
    fmt: Any,
    question: str | None = None,
) -> Response:
    """Run a function at one pinned version and render its answer. `body` and `given`
    are the arguments of the call from the body and from the query string; with
    `question`, the answer is that question's."""
    kind = "answer" if question is not None else "run"
    query = fingerprint(kind, question or function_id, body, given)
    version, position = start(services, cursor, kind, query, at, branch)
    # What the twin runs is what its registries say now; `at` chooses the data it reads.
    known = services.store.head()
    asked = None
    if question is not None:
        asked = registry.question_or_404(services, question, known)
        if not asked.get("function"):
            raise ApiError(
                f"the question {asked['question']!r} names no function", code="function.not_found"
            )
        function_id = asked["function"]
    record = registry.function_or_404(services, function_id, known)
    if given and record["kind"] != "builtin":
        raise ApiError(
            "the arguments of code of the twin's own are given in the body, as JSON",
            code="api.bad_request",
        )
    # Answering a question is a read: rows its function would have written are not.
    ran = running.run(
        services, record, version, {**body, **given}, known=known, write=question is None
    )
    if asked is not None:
        provisional, reasons, notes = running.question_state(asked)
        ran.provisional = ran.provisional or provisional
        ran.reasons = list(dict.fromkeys([*ran.reasons, *reasons]))
        answer = ran.answer
        ran.answer = type(answer)(
            value=answer.value,
            unit=answer.unit,
            interval=answer.interval,
            notes=tuple(dict.fromkeys([*answer.notes, *notes])),
        )
    return answer_response(
        request, ran, version, fmt=fmt, limit=limit, position=position, kind=kind, query=query
    )


@router.post(
    "/{id}/run",
    summary="Run a function at a version",
    description="The arguments of the call are the JSON object of the body; for a built-in "
    "they may be written in the query string instead, so that a call needs no body (a list "
    "is repeated or comma-separated; `at`, `branch`, `format`, `limit` and `cursor` keep "
    "their meaning). The function is the one registered now; `at` and `branch` choose the "
    "data it reads: the version is resolved once and every read is made at it. A built-in "
    "runs in the API and gives up at the function's latency budget; a forecast is made by "
    "the active model of its question; code of the twin's own runs in the worker. The "
    "answer is `value` with its `unit`, `interval`, `notes`, the `version` read, the "
    "`function`, the `model` and whether it is `provisional`. A table is the rows of "
    "`value`, a page at a time, or Arrow or Parquet with `format` or `Accept`. A function "
    "that gives rows to write has them written as one version, with a record of the run, "
    "and names that version in `wrote`; any other run writes nothing.",
    response_model=AnswerOut,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(400, 404, 409, 422, 502, 504, notes=RUN_NOTES),
    },
    openapi_extra=ARGUMENTS_BODY,
)
async def run_function(
    request: Request,
    services: ServicesDep,
    id: FunctionId,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    body = await call_arguments(request, query=False)
    both = await call_arguments(request, query=True)
    given = {name: value for name, value in both.items() if name not in body}

    def work() -> Response:
        return run_and_answer(
            request, services, id, body, given,
            at=at, branch=branch, limit=limit, cursor=cursor, fmt=fmt,
        )  # fmt: skip

    return await anyio.to_thread.run_sync(work)
