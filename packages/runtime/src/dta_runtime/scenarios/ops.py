"""Making a scenario, filling its branch, reading its record, removing it.

A scenario's branch starts at its base version. The assumed facts that hold from
the base on are one version at the base time; a run then adds one version per
grid time, holding what the models predict for that time and the assumed facts
that change there. The live line gets nothing of this but the record.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.schema import FactKind, Kind, Ontology
from dta_core.store import (
    EPOCH,
    LIVE_BRANCH,
    BranchExists,
    ChangeSet,
    CommitMeta,
    Provenance,
    UnknownBranch,
    VersionRef,
)
from dta_core.store.model import iso_utc

from ..protocol.families import DS, MEAN, UNIQUE_ID
from ..protocol.worker import InterventionRef
from ..twin import Twin
from . import errors, prediction, records
from .checks import assumptions_of, check
from .errors import ScenarioError, refused
from .plan import Assumed, Cell, assumed_at
from .records import ACTOR, CREATED, DELETED, DONE, FAILED, RUNNING
from .request import (
    Predictor,
    ScenarioRequest,
    canonical,
    duration_of,
    duration_text,
    intervention_documents,
    interventions_of,
    scenario_id,
)
from .targets import Target, resolve

log = Log("runtime.scenarios")

# Up to this many objects are asked of the store by name; beyond it the type is read whole.
_ASKED_BY_NAME = 1000
_MICROSECOND = timedelta(microseconds=1)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _described(interventions: Sequence[InterventionRef]) -> list[dict[str, Any]]:
    """Interventions as they are logged: what is changed and when, never to what."""
    return [
        {
            "target": found.target,
            "uid": found.uid,
            "start": iso_utc(found.start),
            "end": None if found.end is None else iso_utc(found.end),
        }
        for found in interventions
    ]


# -- writing the branch ------------------------------------------------------------------


def _changes(
    ontology: Ontology,
    t: datetime,
    scenario: str,
    assumed: Sequence[Cell],
    simulated: Sequence[tuple[str, Sequence[Cell]]],
) -> ChangeSet:
    """One version's rows: the assumed cells with the scenario as their source, the
    predicted ones with their model. Each cell carries its own provenance, so one
    version holds both kinds, and both on one object when it has both."""
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    origins: dict[str, dict[str, Provenance]] = {}

    def put(cells: Sequence[Cell], origin: Provenance) -> None:
        for type_name, uid, name, value in cells:
            rows.setdefault(type_name, {}).setdefault(uid, {"uid": uid})[name] = value
            origins.setdefault(uid, {})[name] = origin

    put(assumed, Provenance(source=scenario, kind=FactKind.assumed, t_valid=t))
    for model, cells in simulated:
        put(cells, Provenance(source=model, kind=FactKind.simulated, t_valid=t))
    whole: dict[str, Provenance] = {}
    for type_name, by_uid in rows.items():
        properties = len(ontology.type(type_name).fields)
        for uid, row in by_uid.items():
            if len(row) - 1 == properties:
                # A row naming every property replaces the object, and the store then
                # wants a provenance for the row as well.
                whole[uid] = next(iter(origins[uid].values()))
    return ChangeSet(
        upserts={type_name: list(by_uid.values()) for type_name, by_uid in rows.items()},
        row_provenance=whole,
        property_provenance=origins,
    )


def _commit(
    twin: Twin,
    branch: str,
    scenario: str,
    j: int,
    t: datetime,
    assumed: Sequence[Cell],
    simulated: Sequence[tuple[str, Sequence[Cell]]] = (),
) -> VersionRef:
    changes = _changes(twin.ontology, t, scenario, assumed, simulated)
    meta = CommitMeta(
        cause="scenario",
        actor=ACTOR,
        t=t,
        extra={records.EXTRA_SCENARIO: scenario, "step": j},
    )
    return twin.store.commit(changes, meta, branch=branch)


def _make_branch(twin: Twin, branch: str, base: VersionRef, scenario: str) -> None:
    store = twin.store
    try:
        store.branch_create(branch, at=base)
    except BranchExists:
        # No live record names it, or the record is of a scenario that was removed:
        # an earlier attempt stopped between making the branch and recording it.
        log.warn(
            "a branch of the scenario's name was left behind; removing it and starting again",
            scenario=scenario,
            branch=branch,
        )
        store.branch_delete(branch)
        store.branch_create(branch, at=base)


def _drop_branch(twin: Twin, branch: str, scenario: str) -> bool:
    """Remove the branch if it is there; whether it was."""
    try:
        twin.store.branch_delete(branch)
    except UnknownBranch:
        log.debug("the scenario's branch is already gone", scenario=scenario, branch=branch)
        return False
    return True


def _start_branch(
    twin: Twin, branch: str, base: VersionRef, scenario: str, assumptions: Sequence[Assumed]
) -> None:
    """The branch at its base, with what is assumed from the base time on."""
    _make_branch(twin, branch, base, scenario)
    held = assumed_at(assumptions, 0)
    if held:
        _commit(twin, branch, scenario, 0, base.t, held)


# -- create ------------------------------------------------------------------------------


def create(
    twin: Twin, request: ScenarioRequest, *, at: VersionRef, step: timedelta
) -> dict[str, Any]:
    """Make the scenario a request describes, starting at version `at` of the live line
    on a grid of `step`: its branch, the facts assumed from the base time on, and its
    record. The record, with `id` and `created`.

    The same request at the same base is the same scenario: when it exists and was not
    deleted it is returned as it is, with `created` false, and nothing is written.
    """
    started = time.perf_counter()
    checked = check(twin, request, at, step)
    found = scenario_id(request, at, step)
    uid, branch = records.scenario_uid(found), records.branch_name(found)
    existing = records.find(twin, uid)
    if existing is not None and existing["status"] != DELETED:
        log.info(
            "scenario exists already; nothing written",
            scenario=uid,
            status=existing["status"],
            base_seq=at.seq,
        )
        return {**existing, "created": False}

    row = {
        "uid": uid,
        "name": request.name,
        "description": request.description,
        "branch": branch,
        "base_version": at.id,
        "base_t": at.t,
        "interventions": canonical(intervention_documents(request.interventions)),
        "horizon": duration_text(request.horizon),
        "models": list(request.models),
        "status": CREATED,
        "error": None,
    }
    _make_branch(twin, branch, at, uid)
    try:
        held = assumed_at(checked.assumptions, 0)
        if held:
            _commit(twin, branch, uid, 0, at.t, held)
        version = records.write(
            twin, row, status=CREATED, step=step, note=f"scenario {request.name}"
        )
    except Exception as exc:
        log.error(
            "the scenario could not be made; removing its branch",
            exc=exc,
            scenario=uid,
            branch=branch,
            base_seq=at.seq,
        )
        try:
            _drop_branch(twin, branch, uid)
            log.info("branch of the scenario that was not made is removed", branch=branch)
        except Exception as second:
            log.error(
                "the branch of a scenario that was not made could not be removed",
                exc=second,
                scenario=uid,
                branch=branch,
            )
        raise
    record = records.require(twin, uid, at=version)
    log.info(
        "scenario created",
        scenario=uid,
        branch=branch,
        base_seq=at.seq,
        base=at.id,
        steps=checked.steps,
        step_s=step.total_seconds(),
        models=list(request.models),
        interventions=_described(request.interventions),
        assumed_at_base=len(held),
        remade=existing is not None,
        elapsed_ms=_ms(started),
    )
    return {**record, "created": True}


# -- run ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    model: str
    target: Target
    table: pa.Table


_REFUSED = {
    RUNNING: (errors.RUNNING, "is running"),
    DONE: (errors.ALREADY_RUN, "has been run"),
    DELETED: (errors.DELETED, "was deleted"),
}


def _ask(
    twin: Twin,
    record: dict[str, Any],
    ontology: Ontology,
    base: VersionRef,
    step: timedelta,
    steps: int,
    interventions: Sequence[InterventionRef],
    predict: Predictor,
    budget_s: float,
    started: float,
) -> list[_Answer]:
    """Every model's prediction, checked. The budget is looked at before each call."""
    uid, models = record["uid"], list(record["models"])
    answers: list[_Answer] = []
    for index, model_uid in enumerate(models):
        left = budget_s - (time.monotonic() - started)
        if left <= 0:
            raise refused(
                errors.TIMEOUT,
                f"{uid}: out of time before {model_uid} was asked; {index} of {len(models)} "
                f"models answered within {budget_s:g} s",
                scenario=uid,
                model=model_uid,
                answered=index,
                budget_s=budget_s,
            )
        stored = twin.store.get(model_uid, at=base, branch=LIVE_BRANCH)
        if stored is None:
            raise refused(
                errors.NOT_FOUND,
                f"{uid}: its model {model_uid} is not at the base version",
                scenario=uid,
                model=model_uid,
            )
        model = {"uid": model_uid, **stored.values}
        try:
            target = resolve(ontology, model["target"])
        except ValueError as exc:
            raise errors.bad_prediction(f"its target: {exc}", model=model_uid) from exc
        asked = time.perf_counter()
        table = predict(
            model,
            base=base,
            branch=record["branch"],
            horizon=steps,
            step=step,
            interventions=interventions,
            deadline_s=left,
        )
        checked = prediction.check(table, model=model_uid, base=base.t, step=step, horizon=steps)
        log.info(
            "model answered",
            scenario=uid,
            model=model_uid,
            target=target.text,
            rows=checked.num_rows,
            elapsed_ms=_ms(asked),
        )
        answers.append(_Answer(model_uid, target, checked))
    return answers


def _types_on_branch(twin: Twin, answer: _Answer, head: VersionRef) -> dict[str, str]:
    """The concrete type of every object the model predicted for, as the branch holds
    them. A series for an object that is not there is refused."""
    series = answer.table.column(UNIQUE_ID).unique().to_pylist()
    by_name = [("uid", "in", series)] if len(series) <= _ASKED_BY_NAME else None
    types: dict[str, str] = {}
    for member in answer.target.members:
        found = twin.store.table(
            member, columns=[], filter=by_name, at=head, ontology=twin.ontology
        )
        types.update(dict.fromkeys(found.column("uid").to_pylist(), member))
    absent = sorted(uid for uid in series if uid not in types)
    if absent:
        raise errors.bad_prediction(
            f"{len(absent)} series are of objects that are not {answer.target.type} on the "
            f"scenario's branch: {', '.join(absent[: prediction.NAMED])}",
            model=answer.model,
            series=absent[: prediction.NAMED],
        )
    return types


def _by_step(
    answer: _Answer, types: dict[str, str], base: datetime, step: timedelta, steps: int
) -> list[list[Cell]]:
    """The predicted cells of each grid time, the first at index 0."""
    whole = answer.target.field.kind == Kind.integer
    name = answer.target.property
    table = answer.table
    # Times as whole microseconds: the place of each on the grid is then exact.
    moments = table.column(DS).cast(pa.int64()).to_pylist()
    origin = (base - EPOCH) // _MICROSECOND
    width = step // _MICROSECOND
    cells: list[list[Cell]] = [[] for _ in range(steps)]
    for uid, moment, mean in zip(
        table.column(UNIQUE_ID).to_pylist(), moments, table.column(MEAN).to_pylist(), strict=True
    ):
        value = round(mean) if whole else mean
        cells[(moment - origin) // width - 1].append((types[uid], uid, name, value))
    return cells


def _error_text(exc: BaseException) -> str:
    """What the record keeps of a failure: its code and message, never a stack."""
    if isinstance(exc, DtaError):
        return f"{exc.code}: {exc.message}"
    # What code outside the twin's own errors said may hold anything; it stays in the log.
    return f"{ScenarioError.code}: {type(exc).__name__} was raised; the cause is in the log"


def run(twin: Twin, uid: str, *, predict: Predictor, budget_s: float) -> dict[str, Any]:
    """Fill the scenario's branch: ask each model, then write one version per grid time.
    The record, with `versions` (how many the branch gained) and `duration_ms`.

    Nothing is written to the branch before every model has answered and every answer
    has been checked. A scenario that failed is started again from a fresh branch.
    """
    started = time.monotonic()
    clock = time.perf_counter()
    store = twin.store
    record = records.require(twin, uid)
    status = record["status"]
    if status in _REFUSED:
        code, said = _REFUSED[status]
        log.warn("scenario run refused", scenario=uid, status=status, code=code)
        raise refused(code, f"{uid} {said}", scenario=uid, status=status)

    branch = record["branch"]
    base = store.resolve(record["base_version"], LIVE_BRANCH)[0]
    step = records.step_of(twin, uid)
    steps = duration_of(record["horizon"]) // step
    interventions = interventions_of(record["interventions"])
    ontology = store.ontology_at(base)
    assumptions = assumptions_of(twin, ontology, base, step, interventions)
    if status == FAILED:
        dropped = _drop_branch(twin, branch, uid)
        _start_branch(twin, branch, base, uid, assumptions)
        log.info(
            "branch of a failed scenario made again from its base",
            scenario=uid,
            branch=branch,
            was_there=dropped,
        )

    log.info(
        "scenario run started",
        scenario=uid,
        branch=branch,
        base_seq=base.seq,
        steps=steps,
        step_s=step.total_seconds(),
        models=list(record["models"]),
        interventions=_described(interventions),
        budget_s=budget_s,
    )
    records.set_status(twin, uid, RUNNING)
    written = rows = 0
    try:
        answers = _ask(
            twin, record, ontology, base, step, steps, interventions, predict, budget_s, started
        )
        head = store.head(branch)
        predicted = [
            (
                answer.model,
                _by_step(answer, _types_on_branch(twin, answer, head), base.t, step, steps),
            )
            for answer in answers
        ]
        for j in range(1, steps + 1):
            assumed = assumed_at(assumptions, j)
            simulated = [(model, cells[j - 1]) for model, cells in predicted]
            _commit(twin, branch, uid, j, base.t + j * step, assumed, simulated)
            written += 1
            rows += len(assumed) + sum(len(cells) for _, cells in simulated)
        records.set_status(twin, uid, DONE)
    except Exception as exc:
        text = _error_text(exc)
        log.error(
            "scenario run failed",
            exc=exc,
            scenario=uid,
            branch=branch,
            code=getattr(exc, "code", None),
            versions_written=written,
            steps=steps,
            elapsed_ms=_ms(clock),
        )
        try:
            records.set_status(twin, uid, FAILED, error=text)
        except Exception as second:
            log.error(
                "the failure of a scenario run could not be recorded; its status is still "
                f"{RUNNING}",
                exc=second,
                scenario=uid,
            )
        raise
    duration_ms = _ms(clock)
    log.info(
        "scenario run finished",
        scenario=uid,
        branch=branch,
        versions=written,
        rows=rows,
        duration_ms=duration_ms,
    )
    return {**records.require(twin, uid), "versions": written, "duration_ms": duration_ms}


# -- get, list, delete -------------------------------------------------------------------


def _with_branch(twin: Twin, record: dict[str, Any]) -> dict[str, Any]:
    """The record with whether its branch exists and, when it does, where its head is."""
    try:
        head = twin.store.head(record["branch"])
    except UnknownBranch:
        return {**record, "exists": False}
    return {**record, "exists": True, "head": {"seq": head.seq, "t": head.t}}


def get(twin: Twin, uid: str, at: Any = "head") -> dict[str, Any]:
    """The record as it was at a version of the live line, with `id`, `exists` (whether
    its branch exists now) and, when it does, the branch's `head` (`seq` and `t`)."""
    return _with_branch(twin, records.require(twin, uid, at))


def list_all(twin: Twin, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Every scenario's record with its `id`, by base time and then name."""
    rows = twin.store.table(records.TYPE, ontology=twin.ontology).to_pylist()
    found = [
        # The base time as a read of the one record gives it.
        {**row, "base_t": row["base_t"].astimezone(UTC), "id": records.id_of(row["uid"])}
        for row in rows
        if include_deleted or row["status"] != DELETED
    ]
    return sorted(found, key=lambda row: (row["base_t"], row["name"], row["uid"]))


def delete(twin: Twin, uid: str) -> dict[str, Any]:
    """Remove the scenario's branch and mark its record deleted; the record as `get`
    gives it. A scenario that is already deleted is returned unchanged."""
    record = records.require(twin, uid)
    status = record["status"]
    if status == RUNNING:
        log.warn("scenario not deleted: it is running", scenario=uid)
        raise refused(errors.RUNNING, f"{uid} is running", scenario=uid, status=status)
    if status == DELETED:
        log.debug("scenario is already deleted", scenario=uid)
        return _with_branch(twin, record)
    removed = _drop_branch(twin, record["branch"], uid)
    records.write(twin, {"uid": uid, "status": DELETED}, status=DELETED)
    log.info(
        "scenario deleted",
        scenario=uid,
        branch=record["branch"],
        branch_removed=removed,
        was=status,
    )
    return _with_branch(twin, records.require(twin, uid))
