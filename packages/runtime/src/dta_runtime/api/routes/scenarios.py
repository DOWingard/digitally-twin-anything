"""Scenarios: what the twin would be under assumed changes, on branches of their own."""

from __future__ import annotations

from typing import Annotated, Any

import anyio.to_thread
from dta_core.log import Log
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Body, Path, Query, Request
from pydantic import ValidationError
from starlette.responses import Response

from ... import scenarios
from ...scenarios import ScenarioRequest, records
from .. import registry
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, tabular, versioned
from ..formats import json_response, negotiate
from ..listing import (
    JOB_RESPONSES,
    LIST_LIMIT_DEFAULT,
    ComputeKey,
    ListLimit,
    records_page,
    start_job,
)
from ..models import JobAccepted, Page, ScenarioList, ScenarioOne, ScenarioRunRequest
from ..paging import paged_response, start
from ..params import At, Branch, Cursor, FormatParam, Limit, page_limit, split_list
from ..problems import ApiError
from ..scenario_runs import RUN_BUDGET_S, SCENARIO_KIND, document, run_work, scenario_uid
from ..tables import fingerprint, version_json

router = APIRouter(prefix=f"{API_PREFIX}/scenarios", tags=["scenarios"])
log = Log("api.scenarios")

ScenarioId = Annotated[
    str,
    Path(
        description="Id of the scenario (twelve hexadecimal characters), or its uid",
        max_length=190,
        examples=["0a1b2c3d4e5f"],
    ),
]
SCENARIO_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": ScenarioRequest.model_json_schema(),
                "examples": {
                    "valve": {
                        "value": {
                            "name": "valve closed",
                            "interventions": [
                                {
                                    "target": "Valve.opening",
                                    "uid": "valve:1",
                                    "value": 0,
                                    "start": "2026-01-05T06:00:00Z",
                                }
                            ],
                            "horizon": "PT2H",
                            "models": ["twin_model:level/balance"],
                        }
                    }
                },
            }
        },
    }
}
REFUSALS = {
    400: "The request cannot be a scenario (`scenario.bad_request`; `errors` lists every "
    "problem: an unknown object, property or model, a time off the grid or outside the "
    "horizon, a property both assumed and predicted)",
    404: "No such scenario (`scenario.not_found`), version or branch",
    409: "A change to a control is assumed and a model does not hold under interventions "
    "(`scenario.not_interventional`); the scenario is running (`scenario.running`) or was "
    "run already (`scenario.already_run`); or the manifest sets no sync interval "
    "(`twin.no_interval`)",
    410: "The scenario was deleted (`scenario.deleted`)",
}


def _one(services: Any, record: dict[str, Any], status: int = 200) -> Response:
    head = services.resolve("head", LIVE_BRANCH)
    return json_response(
        {"data": registry.finite(document(record)), "version": version_json(head)},
        head,
        status=status,
    )


@router.get(
    "",
    summary="The scenarios of the twin",
    description="Every scenario's record as of a version of the live line, in uid order: its "
    "name, branch, base version and time, interventions, horizon, step, models and status "
    "(`created`, `running`, `done`, `failed`, `deleted`). Deleted ones are left out unless "
    "`include_deleted` is true.",
    response_model=ScenarioList,
    responses=versioned(400, 404),
)
def list_scenarios(
    services: ServicesDep,
    include_deleted: Annotated[
        bool, Query(description="Also the scenarios that were deleted")
    ] = False,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("scenarios", include_deleted)
    version, position = start(services, cursor, "scenarios", query, at, branch)
    rows = [
        {**row, "id": records.id_of(row["uid"])}
        for row in registry.rows(services, registry.SCENARIO_TYPE, version)
        if include_deleted or row["status"] != records.DELETED
    ]
    return records_page(
        rows,
        lambda row: registry.finite(document(row)),
        kind="scenarios",
        query=query,
        version=version,
        position=position,
        limit=limit,
    )


@router.post(
    "",
    status_code=201,
    summary="Make a scenario from a version of the live line",
    description="A branch that starts at the version, holding the assumed changes as facts "
    "from the base time on, and the scenario's record. `interventions` are "
    "`{target, uid, value, start, end?}` with times on the twin's grid (the sync interval) "
    "inside the horizon; `models` are the uids of the models that predict the rest. A "
    "property is assumed or predicted, never both. The same request at the same base is the "
    "same scenario: it is given back as it is, with 200 and `created` false, and nothing is "
    "written. Nothing is predicted before the scenario is run.",
    response_model=ScenarioOne,
    responses={
        200: {
            "model": ScenarioOne,
            "description": "The scenario existed already",
            "headers": dict(VERSION_HEADERS),
        },
        201: {"headers": dict(VERSION_HEADERS)},
        **errors(400, 404, 409, notes=REFUSALS),
    },
    openapi_extra=SCENARIO_BODY,
)
async def create_scenario(request: Request, services: ServicesDep, at: At = "head") -> Response:
    raw = await request.body()
    try:
        asked = ScenarioRequest.model_validate_json(raw)
    except ValidationError as exc:
        problems = [
            {"loc": [str(part) for part in error.get("loc", ())], "msg": error.get("msg", "")}
            for error in exc.errors(include_url=False, include_input=False)
        ]
        first = problems[0] if problems else {"loc": [], "msg": "invalid request"}
        where = ".".join(first["loc"])
        log.info("scenario refused: invalid request", count=len(problems))
        raise ApiError(
            f"invalid request: {where}: {first['msg']}" if where else first["msg"],
            code="api.bad_request",
            detail={"problems": problems},
        ) from exc

    def work() -> Response:
        version = services.resolve(at, LIVE_BRANCH)
        record = scenarios.create(services.twin, asked, at=version, step=services.interval(version))
        return _one(services, record, 201 if record.get("created") else 200)

    return await anyio.to_thread.run_sync(work)


@router.post(
    "/{id}/run",
    status_code=202,
    summary="Run a scenario",
    description="Starts a job in the compute lane and answers at once with it; follow it at "
    "`/lifecycle/jobs/{id}`. Each model predicts the horizon from the scenario's branch "
    "through the worker; when every model has answered, the branch gains one version per "
    "grid time holding what was assumed and what was predicted. Nothing is written before "
    "every answer is in. A scenario that failed is started again from a fresh branch. The "
    "job's result is the scenario's record; a refusal is the job's error "
    "(`scenario.timeout`, `scenario.bad_prediction`, a model's or the worker's own code).",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            404,
            409,
            410,
            notes={
                **REFUSALS,
                409: "The scenario is running (`scenario.running`) or was run already "
                "(`scenario.already_run`), or the key was used for another request "
                "(`job.key_reused`)",
                503: "The twin has no worker (`worker.unavailable`), or the store cannot be "
                "reached",
            },
        ),
    },
)
def run_scenario(
    services: ServicesDep,
    id: ScenarioId,
    body: Annotated[ScenarioRunRequest | None, Body()] = None,
    idempotency_key: ComputeKey = None,
) -> Response:
    uid = scenario_uid(id)
    budget_s = (body.budget_s if body else None) or RUN_BUDGET_S
    services.compute.require()
    record = records.require(services.twin, uid)
    status = record["status"]
    params = {"scenario": uid, "budget_s": budget_s}
    # A request that is answered with a job that exists is not judged again: its
    # scenario may well be running or done by now, through that very job.
    if not services.compute_jobs.known(SCENARIO_KIND, params, idempotency_key):
        refusal = {
            records.DELETED: ("scenario.deleted", "was deleted"),
            records.DONE: ("scenario.already_run", "was run already"),
        }.get(status)
        if status == records.RUNNING and uid in services.running_scenarios:
            refusal = ("scenario.running", "is running")
        if refusal is not None:
            log.info("scenario run refused", scenario=uid, status=status, code=refusal[0])
            raise ApiError(f"{uid} {refusal[1]}", code=refusal[0])
    return start_job(
        services, SCENARIO_KIND, params, run_work(services, uid, budget_s), idempotency_key
    )


@router.get(
    "/{id}/compare",
    summary="A scenario beside the live line or another scenario",
    description="The trajectories of `targets` on the scenario's grid, from its base time "
    "to the end of its horizon: `target`, `uid`, `t`, `scenario` (the value on the "
    "scenario's branch), `against` (the value on the other side) and `difference` "
    "(scenario less against), in target, object and time order. `against` is `main` or "
    "another scenario of the same base, by id, uid or branch.",
    response_model=Page,
    responses={
        **tabular(400, 404, 409, 410),
        **errors(400, 404, 409, 410, notes=REFUSALS),
    },
)
def compare_scenario(
    request: Request,
    services: ServicesDep,
    id: ScenarioId,
    targets: Annotated[
        list[str],
        Query(
            description="Properties to compare, `Type.property`, repeated or comma-separated",
            examples=["Tank.level"],
        ),
    ],
    against: Annotated[
        str, Query(description="`main`, or another scenario of the same base", max_length=190)
    ] = LIVE_BRANCH,
    uids: Annotated[
        list[str] | None,
        Query(description="Objects to keep, repeated or comma-separated; all if absent"),
    ] = None,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    uid = scenario_uid(id)
    wanted = split_list(targets) or []
    objects = split_list(uids)
    query = fingerprint("compare", uid, wanted, against, objects)
    version, position = start(services, cursor, "compare", query, "head", LIVE_BRANCH)
    other = against
    if against != LIVE_BRANCH and not against.startswith(records.BRANCH_PREFIX):
        other = scenario_uid(against)
    table = scenarios.compare(services.twin, uid, targets=wanted, against=other, uids=objects)
    return paged_response(table, chosen, version, per_page, position, "compare", query)


@router.get(
    "/{id}",
    summary="One scenario",
    description="The record as it was at a version of the live line, with whether its "
    "branch exists now (`exists`) and where the branch's head is (`head`).",
    response_model=ScenarioOne,
    responses=versioned(400, 404, notes={404: "No such scenario (`scenario.not_found`)"}),
)
def one_scenario(
    services: ServicesDep, id: ScenarioId, at: At = "head", branch: Branch = LIVE_BRANCH
) -> Response:
    version = services.resolve(at, branch)
    record = scenarios.get(services.twin, scenario_uid(id), at=version)
    return json_response(
        {"data": registry.finite(document(record)), "version": version_json(version)}, version
    )


@router.delete(
    "/{id}",
    summary="Delete a scenario",
    description="Removes the scenario's branch and marks its record `deleted`; the record "
    "stays, so the history says what was tried. Deleting a deleted scenario changes nothing.",
    response_model=ScenarioOne,
    responses=versioned(
        400,
        404,
        409,
        notes={
            404: "No such scenario (`scenario.not_found`)",
            409: "The scenario is running (`scenario.running`)",
        },
    ),
)
def delete_scenario(services: ServicesDep, id: ScenarioId) -> Response:
    return _one(services, scenarios.delete(services.twin, scenario_uid(id)))
