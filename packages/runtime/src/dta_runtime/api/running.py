"""Running a function of the twin at one pinned version.

A built-in over the graph runs here, in the thread that serves the request, and gives
up at the function's latency budget. A forecast is handed to the question's active
model. Code the twin brought runs in the worker and nowhere else. Only a function that
gives rows to write changes the twin: the rows and the record of the run are one version.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.store import VersionRef

from .. import fidelity, functions
from ..functions.errors import BAD_ENTRY, BAD_PARAM, PARAM_MISSING, TIMEOUT, refused
from ..models import horizon_steps
from ..protocol import Answer, FunctionRequest, Interval
from . import derived, predicting, registry, withheld
from .problems import ApiError

if TYPE_CHECKING:
    from .services import Services

log = Log("api.functions")

PROVISIONAL = "provisional"
FAILING = "failing"
# What started a run, as its record keeps it.
ON_REQUEST = "on_request"
QUESTION_FAILING = "question_failing"


@dataclass
class Ran:
    """What one run gave: its answer, and what the API knows beside it."""

    function: str
    answer: Answer
    # The model that predicted, for a forecast.
    model: str | None = None
    provisional: bool = False
    reasons: list[str] = field(default_factory=list)
    # The version the run wrote, when it gave rows to write, and how many.
    wrote: VersionRef | None = None
    rows_written: int | None = None
    duration_ms: float = 0.0


def unit_of(services: Services, version: VersionRef, target: str) -> str | None:
    """The unit of a `Type.property`, as the ontology in force at a version states it."""
    type_name, _, name = target.partition(".")
    ontology = services.ontology(version)
    names = (
        ontology.abstract.get(type_name, ()) if not ontology.has_type(type_name) else (type_name,)
    )
    for found in names:
        if ontology.type(found).has_field(name):
            return ontology.type(found).field(name).unit
    return None


def question_state(record: Mapping[str, Any] | None) -> tuple[bool, list[str], tuple[str, ...]]:
    """Whether a question's answers are provisional, why, and the notes they carry."""
    if record is None:
        return False, [], ()
    if record["status"] == PROVISIONAL:
        return True, [record.get("reason") or "the question is not validated yet"], ()
    if record["status"] == FAILING:
        return False, [], (QUESTION_FAILING,)
    return False, [], ()


def _forecaster(
    services: Services, function_id: str, version: VersionRef, known: VersionRef, ran: Ran
):
    """What `forecast` is handed to: the active model of the question it names, as the
    registries stand at `known`, predicting from the history up to `version`."""

    def forecast(params: Mapping[str, Any], context: Any) -> Answer:
        question_id = params["question"]
        record = registry.one(
            services, registry.QUESTION_TYPE, registry.question_uid(question_id), known
        )
        if record is None:
            raise refused(
                BAD_PARAM,
                f"{function_id}: the twin has no question {question_id!r}",
                function=function_id,
                parameter="question",
            )
        active = registry.rows(
            services,
            registry.MODEL_TYPE,
            known,
            [("question", "=", question_id), ("status", "=", "active")],
        )
        if not active:
            log.info(
                "forecast refused: no active model", function=function_id, question=question_id
            )
            raise ApiError(
                f"no model is active for the question {question_id!r}; evaluate its "
                "specification first",
                code="model.none_active",
            )
        model = active[0]
        horizon = params.get("horizon")
        if horizon is None:
            manifest = services.twin.manifest(at=known, branch=known.branch)
            declared = ((manifest.contract.questions if manifest.contract else None) or {}).get(
                question_id
            )
            horizon = declared.horizon if declared is not None else None
        if horizon is None:
            raise refused(
                PARAM_MISSING,
                f"{function_id}: the question {question_id!r} states no horizon; give one",
                function=function_id,
                parameter="horizon",
            )
        withheld.refuse_unknown(
            services,
            version,
            fidelity.model_properties(model),
            uids=params.get("uids"),
            subject={"function": function_id, "question": question_id, "model": model["uid"]},
        )
        result, table = predicting.predict(
            services,
            model,
            version,
            horizon=horizon_steps(horizon, services.interval(known)),
            levels=list(params.get("levels") or ()),
            uids=params.get("uids"),
            step=services.interval(known),
            budget_s=context.deadline - time.monotonic(),
            known=known,
            guard=False,
        )
        ran.model = model["uid"]
        ran.provisional, ran.reasons, notes = question_state(record)
        return Answer(
            value=table,
            unit=unit_of(services, version, model["target"]),
            notes=(*result.get("notes", ()), *notes),
        )

    return forecast


def run_uid(function_uid: str, version: VersionRef, trace_id: str, args: str) -> str:
    """Names a run by the version it read and what it was asked: nothing of the wall clock."""
    digest = hashlib.sha256(f"{function_uid}:{version.id}:{trace_id}:{args}".encode()).hexdigest()
    return f"{registry.RUN_SCOPE}:{version.t:%Y%m%dT%H%M%S}Z-{digest[:8]}"


def _registered(
    services: Services,
    record: Mapping[str, Any],
    version: VersionRef,
    args: Mapping[str, Any],
    ran: Ran,
    write: bool,
    trigger: str,
) -> Answer:
    function_id = ran.function
    artifact = registry.bare(record.get("artifact"))
    if artifact is None:
        log.error("a registered function has no code artifact", function=function_id)
        raise refused(
            BAD_ENTRY,
            f"{function_id}: the function is supplied as code and its record names none",
            function=function_id,
        )
    request = FunctionRequest(
        budget_s=registry.latency_s(record),
        trace_id=predicting.trace_id(),
        artifact=artifact,
        entry=record["entry"],
        args=dict(args),
        at=version.id,
        branch=version.branch,
        env=list(record.get("env") or []),
    )
    result, table = services.compute.run(request)
    if result.get("derived"):
        path = services.artifacts.find(result["derived"])
        rows = pa.ipc.open_file(pa.memory_map(str(path))).read_all().to_pylist()
        ran.rows_written = len(rows)
        if not write:
            log.info("derived rows of a run that only measures are not written", rows=len(rows))
            return Answer(value=None)
        text = registry.as_json(dict(args))
        run_row = {
            "uid": run_uid(record["uid"], version, request.trace_id, text),
            "function": record["uid"],
            "trigger": trigger,
            "status": "succeeded",
            "started_at": version.t,
            "at_version": version.id,
            "params": text,
            "rows_written": len(rows),
            "duration_ms": round(float(result.get("duration_ms") or 0.0)),
            "trace_id": request.trace_id,
        }
        ran.wrote = derived.write(
            services,
            branch=version.branch,
            type_name=result["derived_type"],
            rows=rows,
            kind=result["derived_kind"],
            source=record["uid"],
            inputs_version=version.id,
            confidence=result.get("derived_confidence"),
            records={registry.RUN_TYPE: [run_row]},
            note=f"rows derived by {function_id}",
        )
        return Answer(value=None)
    interval = result.get("interval")
    return Answer(
        value=table if table is not None else result.get("value"),
        unit=result.get("unit"),
        interval=Interval(**interval) if interval else None,
        notes=tuple(result.get("notes") or ()),
    )


def run(
    services: Services,
    record: Mapping[str, Any],
    version: VersionRef,
    call: Mapping[str, Any],
    *,
    known: VersionRef | None = None,
    write: bool = True,
    trigger: str = ON_REQUEST,
) -> Ran:
    """One run of the function a record describes, with the arguments of one call, at
    the pinned version. Refuses a disabled function; raises what the function package,
    the worker or the store refuse the run for.

    What the twin runs is what its registries say now, whatever version of its data a
    run reads: `known` is the version the registries are read at, the pinned one when
    absent. `trigger` is what started the run, as the record of a run that wrote keeps it."""
    started = time.perf_counter()
    known = known or version
    function_id = str(registry.bare(record["uid"]))
    if record["status"] == "disabled":
        log.info("run refused: the function is disabled", function=function_id)
        raise ApiError(f"the function {function_id!r} is disabled", code="function.disabled")
    spec = registry.spec_of(record)
    params = functions.resolve(function_id, spec, call)
    ran = Ran(function=function_id, answer=Answer(value=None))
    if spec.kind == "builtin":
        # A forecast's inputs are its model's: judged when the model is known.
        properties, uids = withheld.function_inputs(str(spec.entry), params)
        withheld.refuse_unknown(
            services, version, properties, uids=uids, subject={"function": function_id}
        )
        deadline = time.monotonic() + registry.latency_s(record)
        context = functions.Context(
            store=services.store,
            ontology=services.ontology(version),
            version=version,
            graphs=services.graphs,
            deadline=deadline,
            forecast=_forecaster(services, function_id, version, known, ran),
        )
        ran.answer = functions.run(function_id, str(spec.entry), params, context)
        if time.monotonic() > deadline:
            # The last read began in time and ended late: the answer is as late as none.
            log.warn("function finished after its deadline", function=function_id)
            raise refused(
                TIMEOUT,
                f"{function_id}: did not answer within its latency budget of "
                f"{registry.latency_s(record):g} s",
                function=function_id,
            )
    else:
        ran.answer = _registered(services, record, version, params, ran, write, trigger)
    ran.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
    value = ran.answer.value
    log.info(
        "function run",
        function=function_id,
        kind=spec.kind,
        version=version.id,
        seq=version.seq,
        branch=version.branch,
        rows=value.num_rows if isinstance(value, pa.Table) else ran.rows_written,
        wrote=None if ran.wrote is None else ran.wrote.id,
        model=ran.model,
        duration_ms=ran.duration_ms,
    )
    return ran
