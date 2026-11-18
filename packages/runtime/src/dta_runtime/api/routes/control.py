"""The twin's control loops: a policy asked, on an interval, what to do about the controls
a loop drives, and every action it names proposed as any other."""

from __future__ import annotations

from typing import Annotated

from dta_core.log import Log
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Body, Path
from starlette.responses import Response

from ... import actions
from .. import acting
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..formats import json_response
from ..listing import JOB_RESPONSES, ComputeKey, start_job
from ..models import ControlLoopList, ControlLoopOne, JobAccepted, LoopSwitch
from ..params import At
from ..tables import version_json

router = APIRouter(prefix=f"{API_PREFIX}/control", tags=["control"])
log = Log("api.control")

LoopId = Annotated[
    str,
    Path(description="Id of the control loop, or its uid", max_length=190, examples=["pumping"]),
]
LOOP_NOT_FOUND = "No such control loop (`action.loop_not_found`)"
CLOCK_NOTE = (
    "The twin's clock cannot be read now (`clock.unavailable`), or the store cannot be reached"
)


@router.get(
    "/loops",
    summary="The twin's control loops",
    description="Every control loop the manifest declares, as registered: the controls it "
    "drives, its policy function, its interval, whether it is enabled (a loop is disabled "
    "until it is enabled by hand) and what its last run did.",
    response_model=ControlLoopList,
    responses=versioned(400, 404),
)
def list_loops(services: ServicesDep, at: At = "head") -> Response:
    version = services.resolve(at, LIVE_BRANCH)
    found = actions.loops(services.twin, at=version)
    return json_response({"data": found, "version": version_json(version)}, version)


@router.get(
    "/loops/{id}",
    summary="One control loop, with its last run",
    description="`last_run` is what the loop's last run did: its key, its time, its status, "
    "how many actions it proposed and what became of them, the records and the policy's "
    "notes, and the error of a run that failed.",
    response_model=ControlLoopOne,
    responses=versioned(400, 404, notes={404: LOOP_NOT_FOUND}),
)
def one_loop(services: ServicesDep, id: LoopId, at: At = "head") -> Response:
    version = services.resolve(at, LIVE_BRANCH)
    found = actions.control_loop(services.twin, id, at=version)
    return json_response({"data": found, "version": version_json(version)}, version)


def _switched(services: ServicesDep, id: str, body: LoopSwitch, enabled: bool) -> Response:
    head = services.store.head()
    now = acting.clock_now(services, head)
    switch = actions.enable if enabled else actions.disable
    found = switch(services.twin, id, by=body.by, now=now)
    version = services.store.head()
    return json_response({"data": found, "version": version_json(version)}, version)


@router.post(
    "/loops/{id}/enable",
    summary="Let a control loop run",
    description="The scheduler asks for its run once per boundary of its interval from now "
    "on. One version, which names who enabled it; nothing is written for a loop enabled "
    "already.",
    response_model=ControlLoopOne,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            404,
            409,
            notes={
                404: LOOP_NOT_FOUND,
                409: "The manifest no longer declares the loop (`action.loop_orphaned`)",
                503: CLOCK_NOTE,
            },
        ),
    },
)
def enable_loop(services: ServicesDep, id: LoopId, body: Annotated[LoopSwitch, Body()]) -> Response:
    return _switched(services, id, body, True)


@router.post(
    "/loops/{id}/disable",
    summary="Stop a control loop",
    description="It is no longer run. One version, which names who disabled it; nothing is "
    "written for a loop disabled already.",
    response_model=ControlLoopOne,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(400, 404, notes={404: LOOP_NOT_FOUND, 503: CLOCK_NOTE}),
    },
)
def disable_loop(
    services: ServicesDep, id: LoopId, body: Annotated[LoopSwitch, Body()]
) -> Response:
    return _switched(services, id, body, False)


@router.post(
    "/loops/{id}/run",
    status_code=202,
    summary="Run a control loop now",
    description="Starts a run as a job of the compute lane (kind `loop`) and answers at once; "
    "follow it at `/lifecycle/jobs/{id}`. The run asks the loop's policy function, in the "
    "worker and as a function run does, reading the head and writing nothing, for "
    '`{"actions": [{"action", "object", "params", "reason"}], "notes": [...]}`. Each action '
    "is proposed with the loop as its source, the twin's fidelity state and the incidents "
    "it has not resolved at the run's start, and the twin's clock at the request: bounds, "
    "the interlock (as `POST /actions/{id}/propose` states it), execution and records are "
    "the twin's, never the policy's; an action whose control is not the loop's is refused. "
    "The job's result, kept on the loop's record as `last_run`, is what the run did. A "
    "policy that raises (`action.policy_failed`) or answers something that cannot be used "
    "whole (`action.bad_policy_answer`) fails the run before anything is proposed. A key "
    "that names the loop's last run gives that run back. The scheduler sends the key "
    "`loop:<id>:<boundary>`.",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            404,
            409,
            notes={
                404: LOOP_NOT_FOUND,
                409: "The loop is not enabled (`action.loop_disabled`), or the key was used "
                "for another request (`job.key_reused`)",
                503: CLOCK_NOTE,
            },
        ),
    },
)
def run_loop(services: ServicesDep, id: LoopId, idempotency_key: ComputeKey = None) -> Response:
    found = acting.loop_runnable(services, id)
    now = acting.clock_now(services, services.store.head())
    key = acting.run_key(found["id"], idempotency_key)
    log.info("loop run asked for", loop=found["uid"], keyed=idempotency_key is not None)
    return start_job(
        services,
        acting.LOOP_KIND,
        {"loop": found["id"], "key": key},
        acting.loop_work(services, found["id"], key, now),
        idempotency_key,
    )
