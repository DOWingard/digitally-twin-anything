"""Everything a request for a scenario is checked for, before anything is written.

Every problem is found and reported together, each naming the member of the
request it is about, so that a caller mends the request once. Only then is the
rule on controls applied: a scenario that changes a control is predicted only
by models that say their predictions hold under such a change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import LIVE_BRANCH, StoreError, VersionRef
from dta_core.store.model import iso_utc

from ..protocol.worker import InterventionRef
from ..twin import Twin
from .errors import BAD_REQUEST, NOT_INTERVENTIONAL, ScenarioError, bad_request, problem, refused
from .plan import Assumed, place
from .records import MODEL_TYPE
from .request import MAX_STEPS, ScenarioRequest, canonical
from .targets import Target, resolve

log = Log("runtime.scenarios")

ACTIVE = "active"
_MILLISECOND = timedelta(milliseconds=1)
_NONE = timedelta(0)


@dataclass(frozen=True)
class Checked:
    """What the checks worked out on the way, for the writes that follow."""

    # Steps of the grid in the horizon.
    steps: int
    assumptions: list[Assumed]


def _aware(moment: datetime) -> bool:
    return moment.tzinfo is not None and moment.utcoffset() is not None


def _logged(problems: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """The problems as they are logged: what is said of a value may quote it, so of a
    value only the member is kept."""
    return [
        {"member": found["member"]} if found["member"].endswith(".value") else dict(found)
        for found in problems
    ]


def _refuse(problems: Sequence[dict[str, str]], request: ScenarioRequest, at: VersionRef) -> None:
    log.warn(
        "scenario refused: the request cannot be met",
        name=request.name,
        base=at.id,
        base_seq=at.seq,
        problems=_logged(problems),
    )
    raise bad_request(problems)


def assumptions_of(
    twin: Twin,
    ontology: Ontology,
    base: VersionRef,
    step: timedelta,
    interventions: Sequence[InterventionRef],
) -> list[Assumed]:
    """The interventions of a scenario that passed the checks, placed on its grid."""
    placed = []
    for found in interventions:
        try:
            target = resolve(ontology, found.target)
        except ValueError as exc:
            raise ScenarioError(
                f"an intervention of the scenario names {found.target!r}: {exc}",
                code=BAD_REQUEST,
                detail={"problems": [problem("interventions", str(exc))]},
            ) from exc
        stored = twin.store.get(found.uid, at=base, branch=LIVE_BRANCH)
        if stored is None or stored.type not in target.members:
            message = f"{found.uid!r} is not a {target.type} at the base version"
            raise ScenarioError(
                message, code=BAD_REQUEST, detail={"problems": [problem("interventions", message)]}
            )
        placed.append(
            place(
                stored.type,
                found.uid,
                target.property,
                found.value,
                found.start,
                found.end,
                stored.values.get(target.property),
                base=base.t,
                step=step,
            )
        )
    return placed


def _value_problems(twin: Twin, type_name: str, uid: str, name: str, value: Any) -> list[str]:
    """Why a value cannot be assumed for a property: what the store would refuse it for,
    and what the record could not keep."""
    found: list[str] = []
    try:
        canonical(value)
    except (TypeError, ValueError) as exc:
        found.append(f"cannot be kept as JSON: {exc}")
    try:
        violations = twin.store.validator.validate(
            type_name, [{"uid": uid, name: value}], partial=True
        )
    except Exception as exc:
        # A value of a shape the row checks were not written for.
        log.warn(
            "the row checks raised on an intervention's value",
            error=type(exc).__name__,
            type=type_name,
            property=name,
            uid=uid,
        )
        return [*found, f"cannot be checked against {type_name}.{name} ({type(exc).__name__})"]
    return [*found, *(violation.message for violation in violations)]


def _controls(twin: Twin, ontology: Ontology, at: VersionRef) -> set[tuple[str, str]]:
    """(concrete type, property) of every property the manifest in force at the base
    says can be set."""
    manifest = twin.manifest(at=at)
    boundary = manifest.contract.boundary if manifest.contract is not None else None
    cells: set[tuple[str, str]] = set()
    for text in boundary.controls if boundary is not None else ():
        try:
            cells |= resolve(ontology, text).cells()
        except ValueError as exc:
            log.warn(
                "a control of the manifest is not a property of the ontology; ignored",
                control=text,
                reason=str(exc),
            )
    return cells


def check(twin: Twin, request: ScenarioRequest, at: VersionRef, step: timedelta) -> Checked:
    """Raise `scenario.bad_request` with every problem of the request, or
    `scenario.not_interventional`; otherwise give what the writes need."""
    store = twin.store
    problems: list[dict[str, str]] = []

    # Nothing else can be judged without the base version and the grid.
    if at.branch != LIVE_BRANCH:
        problems.append(
            problem("at", f"a scenario starts at a version of {LIVE_BRANCH}, not of {at.branch}")
        )
    else:
        try:
            ontology = store.ontology_at(at)
        except StoreError as exc:
            problems.append(problem("at", exc.message))
        else:
            if ontology.hash != store.ontology.hash:
                # The store writes a branch only under the ontology it was opened with.
                problems.append(
                    problem(
                        "at",
                        f"the ontology has changed since version #{at.seq}; a scenario starts "
                        "at a version that holds the ontology in force",
                    )
                )
    if step <= _NONE or step % _MILLISECOND:
        problems.append(problem("step", "the step is a positive whole number of milliseconds"))
    if problems:
        _refuse(problems, request, at)

    base = at.t
    steps = request.horizon // step
    until = base + request.horizon
    if request.horizon % step:
        problems.append(
            problem(
                "horizon",
                f"is not a whole multiple of the step of {step.total_seconds():g} s",
            )
        )
    elif steps > MAX_STEPS:
        problems.append(
            problem("horizon", f"is {steps} steps of the grid; at most {MAX_STEPS} are written")
        )

    # -- models ----------------------------------------------------------------------
    models: list[tuple[int, dict[str, Any], Target]] = []
    for index, uid in enumerate(request.models):
        member = f"models[{index}]"
        stored = store.get(uid, at=at, branch=LIVE_BRANCH)
        if stored is None or stored.type != MODEL_TYPE:
            problems.append(problem(member, f"{uid!r} is not a model at the base version"))
            continue
        record = {"uid": uid, **stored.values}
        if record["status"] != ACTIVE:
            problems.append(problem(member, f"{uid} is {record['status']}, not {ACTIVE}"))
        if not record.get("fitted"):
            problems.append(problem(member, f"{uid} has no fitted state"))
        try:
            target = resolve(ontology, record["target"])
        except ValueError as exc:
            problems.append(problem(member, f"the target of {uid}: {exc}"))
            continue
        if not target.numeric:
            problems.append(
                problem(
                    member,
                    f"{uid} predicts {target.text}, which holds "
                    f"{'lists of ' if target.field.multivalued else ''}"
                    f"{target.field.kind.value} values; a prediction is a number",
                )
            )
            continue
        for earlier_index, earlier, earlier_target in models:
            if earlier_target.cells() & target.cells():
                problems.append(
                    problem(
                        member,
                        f"{uid} predicts {target.text}, which models[{earlier_index}] "
                        f"({earlier['uid']}) predicts as {earlier_target.text}; one model "
                        "predicts a property",
                    )
                )
        models.append((index, record, target))
    predicted = {
        cell: record["uid"] for _, record, target in models for cell in sorted(target.cells())
    }

    # -- interventions ---------------------------------------------------------------
    placed: list[tuple[int, InterventionRef, Assumed]] = []
    for index, found in enumerate(request.interventions):
        member = f"interventions[{index}]"
        timed = True
        if not _aware(found.start):
            problems.append(problem(f"{member}.start", "has no time zone"))
            timed = False
        elif found.start < base:
            problems.append(problem(f"{member}.start", f"is before the base time {iso_utc(base)}"))
            timed = False
        elif found.start >= until:
            problems.append(
                problem(
                    f"{member}.start",
                    f"is not before the end of the horizon, {iso_utc(until)}",
                )
            )
            timed = False
        if found.end is not None:
            if not _aware(found.end):
                problems.append(problem(f"{member}.end", "has no time zone"))
                timed = False
            elif _aware(found.start) and found.end <= found.start:
                problems.append(problem(f"{member}.end", "is not after start"))
                timed = False
            elif found.end > until:
                problems.append(
                    problem(f"{member}.end", f"is after the end of the horizon, {iso_utc(until)}")
                )
                timed = False
        try:
            target = resolve(ontology, found.target)
        except ValueError as exc:
            problems.append(problem(f"{member}.target", str(exc)))
            continue
        stored = store.get(found.uid, at=at, branch=LIVE_BRANCH)
        if stored is None or stored.type not in target.members:
            problems.append(
                problem(
                    f"{member}.uid", f"{found.uid!r} is not a {target.type} at the base version"
                )
            )
            continue
        for message in _value_problems(twin, stored.type, found.uid, target.property, found.value):
            problems.append(problem(f"{member}.value", message))
        by_model = predicted.get((stored.type, target.property))
        if by_model is not None:
            problems.append(
                problem(
                    f"{member}.target",
                    f"{stored.type}.{target.property} of {found.uid} is predicted by {by_model}; "
                    "a property is assumed or predicted, not both",
                )
            )
        if timed:
            for earlier_index, earlier, earlier_placed in placed:
                if (earlier_placed.uid, earlier_placed.property) != (found.uid, target.property):
                    continue
                # Each holds from its start up to its end, or without end.
                if (earlier.end is None or found.start < earlier.end) and (
                    found.end is None or earlier.start < found.end
                ):
                    problems.append(
                        problem(
                            member,
                            f"overlaps interventions[{earlier_index}] on "
                            f"{stored.type}.{target.property} of {found.uid}",
                        )
                    )
            placed.append(
                (
                    index,
                    found,
                    place(
                        stored.type,
                        found.uid,
                        target.property,
                        found.value,
                        found.start,
                        found.end,
                        stored.values.get(target.property),
                        base=base,
                        step=step,
                    ),
                )
            )
    if problems:
        _refuse(problems, request, at)

    # -- controls --------------------------------------------------------------------
    if placed:
        controls = _controls(twin, ontology, at)
        on_controls = [
            f"interventions[{index}]"
            for index, _, assumed in placed
            if (assumed.type, assumed.property) in controls
        ]
        unfit = [record["uid"] for _, record, _ in models if not record["interventional"]]
        if on_controls and unfit:
            log.warn(
                "scenario refused: a control is changed and a model does not hold under it",
                name=request.name,
                base=at.id,
                models=unfit,
                interventions=on_controls,
            )
            raise refused(
                NOT_INTERVENTIONAL,
                f"{', '.join(on_controls)} change a control, and {', '.join(unfit)} "
                f"{'does' if len(unfit) == 1 else 'do'} not say predictions hold when a "
                "control is changed",
                models=unfit,
                interventions=on_controls,
            )
    return Checked(steps=steps, assumptions=[assumed for _, _, assumed in placed])
