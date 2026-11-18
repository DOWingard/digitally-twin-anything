"""The twin acting on the real system: its action types, proposals and what became of
them, decisions on them, and whether their effect was observed."""

from __future__ import annotations

from typing import Annotated, Any

from dta_core.log import Log
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Body, Header, Path, Query
from starlette.responses import Response

from ... import actions
from ...actions.records import RECORD_TYPE, action_uid, record_document
from .. import acting
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..formats import json_response
from ..listing import LIST_LIMIT_DEFAULT, ListLimit
from ..models import (
    ActionDecided,
    ActionProposal,
    ActionRecordList,
    ActionRecordOne,
    ActionStatus,
    ActionStepped,
    ActionTypeList,
    ActionTypeOne,
    OperatorStep,
    ReconcileOut,
    ReconciliationStatus,
)
from ..paging import next_cursor, offset_of, start
from ..params import At, Cursor
from ..tables import fingerprint, version_json

router = APIRouter(prefix=f"{API_PREFIX}/actions", tags=["actions"])
log = Log("api.actions")

ActionId = Annotated[
    str,
    Path(description="Id of the action type, or its uid", max_length=190, examples=["set_pump"]),
]
RecordUid = Annotated[
    str,
    Path(
        description="Uid of the record, or its id",
        max_length=200,
        examples=["twin_action_record:5b1f0c2e9d4a47b8a3c6e1f0d2b4a6c8"],
    ),
]
ProposalKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Names the proposal, 1 to 200 printable ASCII characters; the record's id "
        "is derived from it. Proposing again with the same key and the same proposal gives "
        "back its record and does nothing again; with another proposal the key is refused "
        "(`action.key_reused`). Kept in the twin, so it outlives the API process",
        min_length=1,
        max_length=200,
        pattern=r"^[\x20-\x7e]+$",
        examples=["agent-7:pump-on:2026-01-19T06:00"],
    ),
]
ReconcileKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Names the reconciliation, 1 to 200 printable ASCII characters. Asking "
        "again with the same key gives back what it answered and does nothing again. The "
        "last 1000 keys are remembered by the API process",
        min_length=1,
        max_length=200,
        pattern=r"^[\x20-\x7e]+$",
        examples=["reconcile:2026-01-19T06:15:00+00:00"],
    ),
]
RECORD_NOT_FOUND = "No such record (`action.record_not_found`)"
CLOCK_NOTE = (
    "The twin's clock cannot be read now (`clock.unavailable`), or the store cannot be reached"
)


@router.get(
    "",
    summary="The twin's action types",
    description="Every action type the manifest declares, as registered: what it acts on, "
    "the parameters a proposal gives, the bounds and autonomy of the control it carries out, "
    "how it is bound (`executor`: an HTTP call the twin makes; `advisory`: a recommendation "
    "only), how long its effect takes to be observable (`settle`) and how far the observed "
    "value may lie from the value set (`tolerance`). `executable` says whether the twin can "
    "execute it now; `executor_configured` whether the executors add-on is on.",
    response_model=ActionTypeList,
    responses=versioned(400, 404),
)
def list_actions(services: ServicesDep, at: At = "head") -> Response:
    version = services.resolve(at, LIVE_BRANCH)
    has_executor = services.executor is not None
    found = [
        acting.type_document(action, has_executor)
        for action in actions.types(services.twin, at=version)
    ]
    return json_response(
        {"data": found, "executor_configured": has_executor, "version": version_json(version)},
        version,
    )


@router.get(
    "/records",
    summary="What became of the proposals, newest first",
    description="Every proposal is recorded, whatever became of it: `proposed` (waiting for "
    "a decision, or advice only), `rejected` (outside its bounds, or turned down), "
    "`approved` (being sent), `blocked` (by the interlock), `executed` or `failed` (the "
    "external call). An executed action that set a control is reconciled: `pending`, then "
    "`ok` or `mismatch`. Newest proposal first; every filter given must hold.",
    response_model=ActionRecordList,
    responses=versioned(400, 404),
)
def list_records(
    services: ServicesDep,
    status: Annotated[ActionStatus | None, Query(description="Only records in this status")] = None,
    action: Annotated[
        str | None, Query(description="Only records of this action type", max_length=190)
    ] = None,
    object: Annotated[
        str | None, Query(description="Only records on this object", max_length=400)
    ] = None,
    source: Annotated[
        str | None,
        Query(
            description="Only records proposed by this source, a name or a loop's uid",
            max_length=200,
        ),
    ] = None,
    reconciliation: Annotated[
        ReconciliationStatus | None, Query(description="Only records reconciled so")
    ] = None,
    at: At = "head",
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("action-records", status, action, object, source, reconciliation)
    version, position = start(services, cursor, "action-records", query, at, LIVE_BRANCH)
    offset = offset_of(position)
    ontology = services.ontology(version)
    rows: list[dict[str, Any]] = []
    if ontology.has_type(RECORD_TYPE):
        conditions = [
            (name, "=", value)
            for name, value in (
                ("action", action_uid(action) if action else None),
                ("object", object),
                ("status", status),
                ("source", source),
                ("reconciliation", reconciliation),
            )
            if value is not None
        ]
        # One row past the page tells whether another page follows.
        rows = services.store.table(
            RECORD_TYPE,
            filter=conditions or None,
            at=version,
            ontology=ontology,
            order_by=[("proposed_at", "desc")],
            limit=limit + 1,
            offset=offset or None,
        ).to_pylist()
    following = offset + limit if len(rows) > limit else None
    return json_response(
        {
            "data": [record_document(row["uid"], row) for row in rows[:limit]],
            "next_cursor": next_cursor("action-records", query, version, following),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/records/{uid}",
    summary="One record of a proposal",
    description="What was proposed, judged and decided, the external call and its answer, "
    "and whether its effect was observed.",
    response_model=ActionRecordOne,
    responses=versioned(400, 404, notes={404: RECORD_NOT_FOUND}),
)
def one_record(services: ServicesDep, uid: RecordUid, at: At = "head") -> Response:
    version = services.resolve(at, LIVE_BRANCH)
    found = actions.record(services.twin, uid, at=version)
    return json_response({"data": found, "version": version_json(version)}, version)


DECISION_ERRORS = errors(
    400,
    404,
    409,
    notes={
        404: RECORD_NOT_FOUND,
        409: "The record is not waiting for a decision (`action.not_pending`), or the twin "
        "cannot execute it: advisory, its type disabled, or no executor "
        "(`action.not_executable`)",
        503: CLOCK_NOTE,
    },
)


@router.post(
    "/records/{uid}/approve",
    summary="Approve a proposed action",
    description="The approval is recorded, then the action is executed: one HTTP call to the "
    "real system, made inside this request, which may take as long as its timeout (30 s) "
    "and two retries of a connection that cannot be made. While the twin's fidelity is "
    "`failed` an approval gives `blocked` and nothing is sent (`twin.action.blocked` is "
    "notified). An approved record that never got an outcome (the API stopped in between) "
    "is sent again with the same idempotency key.",
    response_model=ActionStepped,
    responses={200: {"headers": dict(VERSION_HEADERS)}, **DECISION_ERRORS},
)
def approve_record(
    services: ServicesDep, uid: RecordUid, body: Annotated[OperatorStep, Body()]
) -> Response:
    made, version = acting.approve(services, uid, by=body.by, note=body.note)
    log.info("action approval answered", record=made["uid"], by=body.by, status=made["status"])
    return json_response({"data": made, "version": version_json(version)}, version)


@router.post(
    "/records/{uid}/reject",
    summary="Turn down a proposed action",
    description="The record becomes `rejected`, with who turned it down and why.",
    response_model=ActionStepped,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            404,
            409,
            notes={
                404: RECORD_NOT_FOUND,
                409: "The record is not waiting for a decision (`action.not_pending`)",
                503: CLOCK_NOTE,
            },
        ),
    },
)
def reject_record(
    services: ServicesDep, uid: RecordUid, body: Annotated[OperatorStep, Body()]
) -> Response:
    made, version = acting.reject(services, uid, by=body.by, note=body.note)
    log.info("action turned down", record=made["uid"], by=body.by)
    return json_response({"data": made, "version": version_json(version)}, version)


@router.post(
    "/reconcile",
    summary="Compare executed actions with what is observed",
    description="Every executed action that set a control and whose effective time plus its "
    "`settle` has passed on the twin's clock is compared with the observed value in force "
    "of its target on its object: equal, or within its `tolerance`, is `ok`; otherwise "
    "`mismatch`. A value the twin did not observe says nothing, and the record waits. The "
    "outcomes are written as one version. A mismatch opens an incident of kind `reconcile` "
    "on its record (the twin's fidelity is then `correcting`) and is notified "
    "(`twin.action.mismatch`); an `ok` closes the open reconcile incident of an earlier "
    "record on the same control and object. The scheduler sends the key "
    "`reconcile:<boundary>`.",
    response_model=ReconcileOut,
    responses={
        200: {"headers": dict(VERSION_HEADERS)},
        **errors(400, notes={503: CLOCK_NOTE}),
    },
)
def reconcile(services: ServicesDep, idempotency_key: ReconcileKey = None) -> Response:
    with services.reconciled.working:
        known = services.reconciled.get(idempotency_key)
        if known is not None:
            log.info("reconciliation asked for again; its answer is given back")
            document, version = known
            return json_response(document, version)
        document, version = acting.reconcile(services)
        services.reconciled.keep(idempotency_key, (document, version))
    return json_response(document, version)


@router.get(
    "/{id}",
    summary="One action type",
    description="As in the listing: parameters, bounds, autonomy, binding, settle, "
    "tolerance, and whether the twin can execute it.",
    response_model=ActionTypeOne,
    responses=versioned(400, 404, notes={404: "No such action type (`action.not_found`)"}),
)
def one_action(services: ServicesDep, id: ActionId, at: At = "head") -> Response:
    version = services.resolve(at, LIVE_BRANCH)
    has_executor = services.executor is not None
    found = acting.type_document(acting.action_or_404(services, id, version), has_executor)
    return json_response(
        {"data": found, "executor_configured": has_executor, "version": version_json(version)},
        version,
    )


@router.post(
    "/{id}/propose",
    status_code=201,
    summary="Propose an action",
    description="The proposal is judged at the head: the object exists and is of the type's "
    "`object_type`, the parameters are the type's, and `value` lies within the bounds of "
    "the control (`minimum`, `maximum`, `allowed`, `max_step` against the value in force). "
    "Then the interlock, with the twin's fidelity state now: an advisory action is recorded "
    "as `proposed` and never executed; a supervised one is `proposed` and waits for an "
    "approval (`twin.action.approval_pending` is notified); an autonomous one is executed "
    "while the twin is `healthy` and `blocked` otherwise (`twin.action.blocked`). An "
    "execution is one HTTP call to the real system made inside this request, which may take "
    "as long as its timeout (30 s) and two retries of a connection that cannot be made. "
    "Whatever the judgment, a record is kept; a refused proposal is recorded as `rejected` "
    "and answered 422 with every reason in `errors` and the record's uid in `record`.",
    response_model=ActionDecided,
    responses={
        201: {"description": "The record made", "headers": dict(VERSION_HEADERS)},
        200: {
            "description": "The record the Idempotency-Key made before",
            "model": ActionDecided,
            "headers": dict(VERSION_HEADERS),
        },
        **errors(
            400,
            409,
            422,
            notes={
                409: "The key was used before for another proposal (`action.key_reused`)",
                422: "The proposal cannot be carried out as asked (`action.rejected`): no such "
                "action type, object or parameter, a value outside the bounds; `errors` "
                "lists every reason (`member`, `message`) and `record` names the record "
                "that keeps the refusal (null when the action type does not exist)",
                503: CLOCK_NOTE,
            },
        ),
    },
)
def propose(
    services: ServicesDep,
    id: ActionId,
    body: Annotated[ActionProposal, Body()],
    idempotency_key: ProposalKey = None,
) -> Response:
    made, version = acting.propose(
        services,
        id,
        object_uid=body.object,
        params=body.params,
        reason=body.reason,
        source=body.source,
        key=idempotency_key,
    )
    created = bool(made.pop("created", False))
    return json_response(
        {"data": made, "created": created, "version": version_json(version)},
        version,
        status=201 if created else 200,
    )
