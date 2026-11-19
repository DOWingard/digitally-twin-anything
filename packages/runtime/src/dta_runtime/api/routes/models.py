"""The models of the twin: its candidates, their evaluations, evaluating them and
asking one to predict."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.store import LIVE_BRANCH
from fastapi import APIRouter, Body, Path, Query, Request
from pydantic import ValidationError
from starlette.responses import Response

from ...models import horizon_steps
from ...models.state import read_header
from ...protocol import InterventionRef
from .. import predicting, registry
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, tabular, versioned
from ..evaluating import EVALUATE_KIND, JOB_BUDGET_S, evaluate_work
from ..formats import dumps, json_response, negotiate, without_nan
from ..listing import (
    JOB_RESPONSES,
    LIST_LIMIT_DEFAULT,
    ComputeKey,
    ListLimit,
    records_page,
    start_job,
)
from ..models import (
    EvaluateRequest,
    JobAccepted,
    ModelList,
    ModelOne,
    ModelRegistration,
    Page,
    PredictBody,
    PredictionOut,
)
from ..paging import paged_response, start
from ..params import At, Branch, Cursor, FormatParam, Limit, page_limit
from ..problems import ApiError
from ..services import Services
from ..tables import fingerprint, version_json

router = APIRouter(prefix=f"{API_PREFIX}/models", tags=["models"])
log = Log("api.models")

PREDICTION_KEY = b"dta:prediction"
ModelStatus = Literal["candidate", "active", "rejected", "retired"]
# A model's uid holds a `/` between its specification and its candidate.
ModelUid = Annotated[
    str,
    Path(
        description="uid of the model, `twin_model:<spec>/<candidate>`, or `<spec>/<candidate>`",
        max_length=190,
        examples=["twin_model:level/theta"],
    ),
]
WORKER_NOTES = {
    502: "The worker cannot be reached (`worker.unreachable`)",
    503: "The twin has no worker (`worker.unavailable`), the worker is busy (`worker.busy`, "
    "with `Retry-After`), or the store cannot be reached",
}


def _state_header(services: Services, record: dict[str, Any]) -> dict[str, Any] | None:
    """The header of a model's fitted state. Only its first line is read: what follows
    is a pickle, which this process never loads."""
    sha256 = registry.bare(record.get("fitted"))
    if sha256 is None:
        return None
    try:
        with services.artifacts.find(sha256).open("rb") as stored:
            header, _ = read_header(stored.readline())
    except (DtaError, OSError) as exc:
        log.warn(
            "the fitted state of a model cannot be read",
            model=record["uid"],
            sha256=sha256,
            error=type(exc).__name__,
        )
        return None
    return {name: value for name, value in header.items() if name != "body"}


@router.get(
    "",
    summary="The models of the twin",
    description="Every candidate of every model specification as of a version, in uid "
    "order, with its status: `candidate`, `active` (the one its question is answered "
    "with), `rejected` (evaluated and outside what was agreed) or `retired`.",
    response_model=ModelList,
    responses=versioned(400, 404),
)
def list_models(
    services: ServicesDep,
    spec: Annotated[
        str | None, Query(description="Only candidates of this specification", max_length=100)
    ] = None,
    status: Annotated[ModelStatus | None, Query(description="Only models in this status")] = None,
    question: Annotated[
        str | None, Query(description="Only models that answer this question", max_length=100)
    ] = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("models", spec, status, question)
    version, position = start(services, cursor, "models", query, at, branch)
    wanted = (("spec", spec), ("status", status), ("question", question))
    filters = [(name, "=", value) for name, value in wanted if value]
    rows = registry.rows(services, registry.MODEL_TYPE, version, filters)
    return records_page(
        rows,
        registry.model_document,
        kind="models",
        query=query,
        version=version,
        position=position,
        limit=limit,
    )


@router.post(
    "",
    status_code=201,
    summary="Register a candidate while the twin runs",
    description="A shipped family by `family`, or code the twin brought by `artifact` and "
    "`entry`. The target and the features have to be properties of the ontology. The "
    "candidate is evaluated with its specification's settings by `POST /models/evaluate`. "
    "One version, cause `registration`.",
    response_model=ModelOne,
    responses={
        201: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            404,
            409,
            notes={
                400: "The family is not one the runtime ships (`ref.model_family`), or the "
                "target or a feature is not a property of the ontology "
                "(`ontology.unknown_type`, `ontology.unknown_property`)",
                404: "No artifact is kept under the hash (`artifact.not_found`), or the "
                "question is not registered (`question.not_found`)",
                409: "A model of this specification and candidate exists (`model.exists`)",
            },
        ),
    },
)
def register_model(services: ServicesDep, body: ModelRegistration) -> Response:
    record, version = registry.add_model(
        services,
        spec=body.spec,
        candidate=body.candidate,
        family=body.family,
        artifact=body.artifact,
        entry=body.entry,
        params=body.params,
        target=body.target,
        features=body.features,
        question=body.question,
        interventional=body.interventional,
        reads=body.reads,
    )
    return json_response(
        {"data": registry.model_document(record), "version": version_json(version)},
        version,
        status=201,
    )


@router.post(
    "/evaluate",
    status_code=202,
    summary="Evaluate the candidates of a model specification",
    description="Starts a job in the compute lane and answers at once with it; follow it at "
    "`/lifecycle/jobs/{id}`. At the head in force when the job starts, for each "
    "specification asked for (all when none): every candidate is walked forward through the "
    "history by the worker, simplest family first; a candidate is accepted when its error is "
    "within the specification's threshold and below its baseline's; the accepted candidate "
    "with the lowest error is fitted on the whole history and becomes `active`, the one "
    "that was active becomes `candidate` again, candidates outside the threshold `rejected`. "
    "A candidate whose backtest fails is recorded as failed and does not stop the others. "
    "A history too short for the fewest folds leaves the candidates as they are and the "
    "question `provisional`. Everything is recorded in one version at the end: a "
    "`TwinBacktest` per candidate, the models' statuses and fitted states, and the "
    "question's status and measured error. The job stops starting new work after `budget_s` "
    "(default 3600) and marks the rest `not_evaluated` in its result.",
    response_model=JobAccepted,
    responses={
        **JOB_RESPONSES,
        200: {**JOB_RESPONSES[200], "model": JobAccepted},
        **errors(
            400,
            404,
            409,
            notes={
                404: "No model of the specification is registered (`model.unknown_spec`)",
                409: "The key was used for another request (`job.key_reused`), or the "
                "manifest sets no sync interval (`twin.no_interval`)",
                **WORKER_NOTES,
            },
        ),
    },
)
def evaluate(
    services: ServicesDep,
    body: Annotated[EvaluateRequest | None, Body()] = None,
    idempotency_key: ComputeKey = None,
) -> Response:
    spec = body.spec if body else None
    budget_s = (body.budget_s if body else None) or JOB_BUDGET_S
    services.compute.require()
    head = services.resolve("head", LIVE_BRANCH)
    services.interval(head)
    if spec is not None and not registry.rows(
        services, registry.MODEL_TYPE, head, [("spec", "=", spec)]
    ):
        raise ApiError(
            f"no model of a specification {spec!r} is registered", code="model.unknown_spec"
        )
    return start_job(
        services,
        EVALUATE_KIND,
        {"spec": spec, "budget_s": budget_s},
        evaluate_work(services, spec, budget_s),
        idempotency_key,
    )


@router.get(
    "/{uid:path}/backtests",
    summary="The evaluations of one model, newest first",
    description="Each `TwinBacktest` of the model: the range walked through, the origins, "
    "the horizon and step, how often it was fitted again, its error and the baseline's in "
    "the acceptance's measure, every score, whether it was accepted, and the artifact "
    "holding the quantiles of its residuals. A backtest that failed has no origins and its "
    "error in `scores`.",
    response_model=Page,
    responses=versioned(400, 404, notes={404: "No such model (`model.not_found`)"}),
)
def model_backtests(
    services: ServicesDep,
    uid: ModelUid,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: ListLimit = LIST_LIMIT_DEFAULT,
    cursor: Cursor = None,
) -> Response:
    query = fingerprint("backtests", uid)
    version, position = start(services, cursor, "backtests", query, at, branch)
    record = registry.model_or_404(services, uid, version)
    rows = registry.rows(services, registry.BACKTEST_TYPE, version, [("model", "=", record["uid"])])
    rows.sort(key=lambda row: (row["started_at"], row["uid"]), reverse=True)
    return records_page(
        rows,
        registry.backtest_document,
        kind="backtests",
        query=query,
        version=version,
        position=position,
        limit=limit,
    )


def _interventions(given: list[dict[str, Any]] | None) -> list[InterventionRef]:
    found = []
    for index, document in enumerate(given or []):
        try:
            found.append(InterventionRef.model_validate(document))
        except ValidationError as exc:
            first = exc.errors(include_url=False, include_input=False)[0]
            where = ".".join(str(part) for part in first["loc"])
            raise ApiError(
                f"interventions[{index}].{where}: {first['msg']}", code="api.bad_request"
            ) from exc
    return found


@router.post(
    "/{uid:path}/predict",
    summary="What a model predicts from a version on",
    description="`horizon` steps of the twin's grid after the version, per object: "
    "`unique_id`, `ds`, `mean` and, for each level, `lo_<level>` and `hi_<level>`. The "
    "prediction is made by the worker from the fitted state the model holds now and the "
    "history up to the version on its branch. `notes` says what the reader has to know: "
    "`outside_validity` (asked beyond what the fit saw), `no_intervals` (the family gives "
    "none and the model has no backtest to take them from). Nothing is written.",
    response_model=PredictionOut,
    responses={
        **tabular(400, 404, 409, 502, 504),
        **errors(
            400,
            404,
            409,
            502,
            504,
            notes={
                404: "No such model (`model.not_found`), version or branch",
                409: "The model has no fitted state (`model.not_fitted`), interventions "
                "were given for a model that does not hold under them "
                "(`model.not_interventional`), its state cannot be used "
                "(`model.bad_state`), or the manifest sets no sync interval "
                "(`twin.no_interval`)",
                **WORKER_NOTES,
            },
        ),
    },
)
def predict(
    request: Request,
    services: ServicesDep,
    uid: ModelUid,
    body: Annotated[PredictBody | None, Body()] = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    asked = body.model_dump(mode="json") if body else {}
    query = fingerprint("predict", uid, asked)
    version, position = start(services, cursor, "predict", query, at, branch)
    # The model is the one the twin holds now, whatever version it predicts from.
    known = services.store.head()
    record = registry.model_or_404(services, uid, known)
    step = services.interval(known)
    horizon = body.horizon if body else None
    if horizon is None:
        manifest = services.twin.manifest(at=known)
        questions = (manifest.contract.questions if manifest.contract else None) or {}
        declared = questions.get(record.get("question") or "")
        horizon = declared.horizon if declared is not None else None
        if horizon is None:
            raise ApiError(
                "give a horizon: the model's question states none", code="api.bad_request"
            )
    if isinstance(horizon, timedelta):
        if horizon <= timedelta(0):
            raise ApiError("the horizon is longer than nothing", code="api.bad_request")
        steps = horizon_steps(horizon, step)
    else:
        steps = horizon
    if steps < 1 or steps > 100_000:
        raise ApiError("the horizon is 1 to 100000 steps", code="api.bad_request")
    result, table = predicting.predict(
        services,
        record,
        version,
        horizon=steps,
        levels=(body.levels if body and body.levels is not None else ()),
        interventions=_interventions(body.interventions if body else None),
        step=step,
        known=known,
    )
    extra = {"notes": list(result.get("notes") or []), "model": record["uid"]}
    if chosen == "json":
        table = without_nan(table)
    else:
        metadata = dict(table.schema.metadata or {})
        metadata[PREDICTION_KEY] = dumps(extra)
        table = table.replace_schema_metadata(metadata)
    return paged_response(table, chosen, version, per_page, position, "predict", query, extra=extra)


@router.get(
    "/{uid:path}",
    summary="One model, with the header of its fitted state",
    description="`state` is what the fitted state says of itself: the family and settings it "
    "was fitted with, the range and number of series it saw, the range of every input "
    "(`validity`), and the versions of the libraries that made it. The state itself is "
    "never loaded by the API.",
    response_model=ModelOne,
    responses=versioned(400, 404, notes={404: "No such model (`model.not_found`)"}),
)
def one_model(
    services: ServicesDep, uid: ModelUid, at: At = "head", branch: Branch = LIVE_BRANCH
) -> Response:
    version = services.resolve(at, branch)
    record = registry.model_or_404(services, uid, version)
    document = registry.model_document(record)
    document["state"] = _state_header(services, record)
    return json_response({"data": document, "version": version_json(version)}, version)


@router.delete(
    "/{uid:path}",
    summary="Retire a model registered while the twin ran",
    description="The model stays in the graph, `retired`: it is no longer evaluated. A "
    "candidate the manifest declares is changed through the manifest.",
    response_model=ModelOne,
    responses=versioned(
        400,
        404,
        409,
        notes={
            404: "No such model (`model.not_found`)",
            409: "The manifest declares it (`model.from_manifest`)",
        },
    ),
)
def delete_model(services: ServicesDep, uid: ModelUid) -> Response:
    record, version = registry.retire_model(services, uid)
    return json_response(
        {"data": registry.model_document(record), "version": version_json(version)}, version
    )
