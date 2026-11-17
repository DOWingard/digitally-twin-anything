"""The questions the twin was built to answer: what each may claim, answering one, and
measuring them against their tolerances."""

from __future__ import annotations

from typing import Annotated

import anyio.to_thread
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Body, Path, Query, Request
from starlette.responses import Response

from .. import registry
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..evaluating import MEASURE_KIND, measure_work
from ..formats import json_response
from ..listing import (
    JOB_RESPONSES,
    LIST_LIMIT_DEFAULT,
    ComputeKey,
    ListLimit,
    call_arguments,
    records_page,
    start_job,
)
from ..models import (
    AnswerOut,
    JobAccepted,
    MeasureRequest,
    QuestionList,
    QuestionOne,
    QuestionStatus,
)
from ..paging import start
from ..params import At, Branch, Cursor, FormatParam, Limit
from ..problems import ApiError
from ..tables import fingerprint, version_json
from .functions import ARGUMENTS_BODY, RUN_NOTES, run_and_answer

router = APIRouter(prefix=f"{API_PREFIX}/questions", tags=["questions"])

QuestionId = Annotated[
    str,
    Path(description="Id of the question, or its uid", max_length=190, examples=["tank_level"]),
]


@router.get(
    "",
    summary="The questions of the twin and what each may claim",
    description="Every question as of a version, in uid order: `validated` (measured within "
    "its tolerance), `provisional` (not measured yet, or not measurable: `reason` says "
    "which) or `failing` (measured outside its tolerance, or its function does not answer), "
    "with the measured error, the tolerance and what the measurement rests on (`basis`).",
    response_model=QuestionList,
    responses=versioned(400, 404),
)
def list_questions(
    services: ServicesDep,
    status: Annotated[
        QuestionStatus | None, Query(description="Only questions in this status")
    ] = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("questions", status)
    version, position = start(services, cursor, "questions", query, at, branch)
    filters = [("status", "=", status)] if status else None
    rows = registry.rows(services, registry.QUESTION_TYPE, version, filters)
    return records_page(
        rows,
        registry.question_document,
        kind="questions",
        query=query,
        version=version,
        position=position,
        limit=limit,
    )


@router.post(
    "/measure",
    status_code=202,
    summary="Measure the questions against their tolerances",
    description="Starts a job in the compute lane and answers at once with it; follow it at "
    "`/lifecycle/jobs/{id}`. At the head in force when the job starts, for each question "
    "asked for (all when none). A question answered by a model: the error of its active "
    "model's latest backtest in the question's own measure against its tolerance "
    "(`basis` `backtest`); `provisional` while the history is too short to evaluate, "
    "`failing` when no candidate is accepted. A question answered by a function alone: the "
    "function is run once and has to answer inside its latency budget (`basis` `latency`); "
    "where the profiles of every source column its property is read from hold a noise "
    "floor, that floor is the measured error (`basis` `noise_floor`) and is compared with "
    "the tolerance when both are in the property's unit. One version, cause `registration`.",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            404,
            409,
            notes={
                404: "A question named is not registered (`question.not_found`)",
                409: "The key was used for another request (`job.key_reused`)",
            },
        ),
    },
)
def measure(
    services: ServicesDep,
    body: Annotated[MeasureRequest | None, Body()] = None,
    idempotency_key: ComputeKey = None,
) -> Response:
    wanted = list(dict.fromkeys(body.questions)) if body and body.questions else None
    if wanted is not None:
        head = services.resolve("head", LIVE_BRANCH)
        known = {row["question"] for row in registry.rows(services, registry.QUESTION_TYPE, head)}
        unknown = [found for found in wanted if found not in known]
        if unknown:
            raise ApiError(
                f"no question {', '.join(unknown)} is registered", code="question.not_found"
            )
    return start_job(
        services,
        MEASURE_KIND,
        {"questions": wanted},
        measure_work(services, wanted),
        idempotency_key,
    )


@router.get(
    "/{id}",
    summary="One question and what it may claim",
    response_model=QuestionOne,
    responses=versioned(400, 404, notes={404: "No such question (`question.not_found`)"}),
)
def one_question(
    services: ServicesDep, id: QuestionId, at: At = "head", branch: Branch = LIVE_BRANCH
) -> Response:
    version = services.resolve(at, branch)
    record = registry.question_or_404(services, id, version)
    return json_response(
        {"data": registry.question_document(record), "version": version_json(version)}, version
    )


@router.post(
    "/{id}/answer",
    summary="Answer a question at a version",
    description="Runs the function the question is answered by, with the arguments of the "
    "body beside the ones its registration fixes, at one pinned version; a forecast is made "
    "by the question's active model. The answer is the one `POST /functions/{id}/run` gives, "
    "marked with what the question may claim: `provisional` with its `reasons` while the "
    "question is not validated, the note `question_failing` when it was measured outside "
    "its tolerance. The answer is given either way: what it is worth is for the reader. "
    "Answering writes nothing to the twin, also when the function gives rows to write: "
    "run the function itself for that.",
    response_model=AnswerOut,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            404,
            409,
            422,
            502,
            504,
            notes={
                **RUN_NOTES,
                404: "No such question (`question.not_found`), or it names no registered "
                "function (`function.not_found`)",
            },
        ),
    },
    openapi_extra=ARGUMENTS_BODY,
)
async def answer(
    request: Request,
    services: ServicesDep,
    id: QuestionId,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    body = await call_arguments(request, query=False)

    def work() -> Response:
        return run_and_answer(
            request, services, "", body, {},
            at=at, branch=branch, limit=limit, cursor=cursor, fmt=fmt, question=id,
        )  # fmt: skip

    return await anyio.to_thread.run_sync(work)
