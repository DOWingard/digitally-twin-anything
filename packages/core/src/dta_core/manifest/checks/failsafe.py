"""Checks of what each control sends when the twin fails (`contract.controls.<c>.on_failure`).

A fail-safe is an action of the manifest sent with constants only, whatever the twin's
fidelity: so the action has to exist, call the real system, act on what the control
sets, and be given every parameter it takes. Whether the action's object type covers
the control's target type through subtypes needs the ontology and is judged with the
other checks against it.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

from ..models import MANIFEST_VERSION, ActionType, Autonomy, Control, FailureMode, Parameter
from ..status import CONDITIONS
from .findings import Issue, check
from .view import ManifestView, split_ref

MISSING = "control.on_failure_missing"
INVALID = "control.on_failure_invalid"
# The parameter an action that carries out a control takes even when it does not declare it.
VALUE = "value"


def target_type_of(view: ManifestView, action: ActionType) -> str | None:
    """The type an action acts on: its own object type, else that of its control's target."""
    if action.object_type:
        return action.object_type
    control = view.controls.get(action.control) if action.control else None
    if control is not None and control.target:
        return split_ref(control.target)[0]
    return None


def failsafe_actions(view: ManifestView) -> Iterator[tuple[str, Control, str, ActionType]]:
    """Each control that sends something when the twin fails, with the action it sends, when
    that action is declared."""
    for cid, control in sorted(view.controls.items()):
        found = control.on_failure
        if found is None or found.mode in (None, FailureMode.hold):
            continue
        action_id = found.action_of(control.action)
        action = view.actions.get(action_id) if action_id else None
        if action is not None:
            yield cid, control, action_id or "", action


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"{value:g}" if _number(value) else repr(value)


def _type_problem(spec: Parameter | None, value: Any) -> str | None:
    wanted = spec.type if spec is not None else None
    if wanted == "number" and not _number(value):
        return f"must be a number, not {_shown(value)}"
    if wanted == "integer" and not (_number(value) and float(value).is_integer()):
        return f"must be a whole number, not {_shown(value)}"
    if wanted == "string" and not isinstance(value, str):
        return f"must be text, not {_shown(value)}"
    if wanted == "boolean" and not isinstance(value, bool):
        return f"must be true or false, not {_shown(value)}"
    if wanted is None and not (isinstance(value, str | bool) or _number(value)):
        return "must be a number, text, true or false"
    return None


def _limit_problems(value: Any, limits: Any, whose: str) -> Iterator[str]:
    low, high = getattr(limits, "minimum", None), getattr(limits, "maximum", None)
    allowed = getattr(limits, "allowed", None)
    if (low is not None or high is not None) and not _number(value):
        yield f"{whose} limits it to numbers, not {_shown(value)}"
    else:
        if low is not None and value < low:
            yield f"{_shown(value)} is below {whose} minimum {low:g}"
        if high is not None and value > high:
            yield f"{_shown(value)} is above {whose} maximum {high:g}"
    if allowed is not None and not any(
        type(value) is type(item)
        and value == item
        or (_number(value) and _number(item) and float(value) == float(item))
        for item in allowed
    ):
        shown = ", ".join(_shown(item) for item in allowed)
        yield f"{_shown(value)} is not one of {whose} values: {shown}"


def _parameter_issues(
    view: ManifestView, base: str, action_id: str, action: ActionType, constants: Mapping
) -> Iterator[Issue]:
    declared: dict[str, Parameter | None] = dict(action.parameters)
    carried = view.controls.get(action.control) if action.control else None
    if action.control and VALUE not in declared:
        declared[VALUE] = None
    path = f"{base}.parameters"
    for name in sorted(set(declared) - set(constants)):
        yield Issue(
            path,
            f"{action_id} takes {name}, which is left open: a fail-safe is sent with constants "
            "only, so give every parameter",
        )
    for name in sorted(set(constants) - set(declared)):
        yield Issue(f"{path}.{name}", f"{name} is not a parameter of {action_id}")
    for name in sorted(set(constants) & set(declared)):
        value, spec = constants[name], declared[name]
        problem = _type_problem(spec, value)
        if problem is not None:
            yield Issue(f"{path}.{name}", f"{name} {problem}")
            continue
        for found in _limit_problems(value, spec, "the parameter's") if spec else ():
            yield Issue(f"{path}.{name}", f"{name}: {found}")
        if name == VALUE and carried is not None and carried.bounds is not None:
            for found in _limit_problems(value, carried.bounds, "the control's"):
                yield Issue(f"{path}.{name}", f"{name}: {found}")


def _hold_issues(base: str, control: Control) -> Iterator[Issue]:
    found = control.on_failure
    assert found is not None
    for name, present in (
        ("action", found.action is not None),
        ("parameters", bool(found.parameters)),
        ("after_stale", found.after_stale is not None),
    ):
        if present:
            yield Issue(f"{base}.{name}", f"hold sends nothing, so {name} has no use: remove it")


def _sending_issues(view: ManifestView, cid: str, control: Control) -> Iterator[Issue]:
    found = control.on_failure
    assert found is not None and found.mode is not None
    base = f"contract.controls.{cid}.on_failure"
    if control.autonomy == Autonomy.advisory:
        yield Issue(
            f"{base}.mode",
            f"control '{cid}' is advisory: the twin never commands it, so it has nothing to "
            f"{'release' if found.mode == FailureMode.release else 'set'}; use hold",
        )
        return
    if found.mode == FailureMode.release:
        if found.action is None:
            yield Issue(f"{base}.action", "release names the action that hands the object back")
            return
        if found.action == control.action:
            yield Issue(
                f"{base}.action",
                f"release names the control's own action '{found.action}'; sending a value "
                "through it is safe_value",
            )
            return
    action_id = found.action_of(control.action)
    if action_id is None:
        return  # the control's own action is not set yet: a gap
    action = view.actions.get(action_id)
    where = f"{base}.action" if found.action is not None else f"{base}.mode"
    if action is None:
        yield Issue(where, f"action '{action_id}' is not declared in actions")
        return
    if action.binding != "executor":
        yield Issue(
            where,
            f"action '{action_id}' is bound {action.binding or 'to nothing'}; a fail-safe calls "
            "the real system, so it needs binding: executor",
        )
    yield from _parameter_issues(view, base, action_id, action, found.parameters)


@check(INVALID)
def on_failure_invalid(view: ManifestView) -> Iterator[Issue]:
    for cid, control in sorted(view.controls.items()):
        found = control.on_failure
        if found is None or found.mode is None:
            continue
        base = f"contract.controls.{cid}.on_failure"
        if found.mode == FailureMode.hold:
            yield from _hold_issues(base, control)
        else:
            yield from _sending_issues(view, cid, control)


@check(MISSING)
def on_failure_missing(view: ManifestView) -> Iterator[Issue]:
    required = CONDITIONS["autonomous_executor"]
    for cid, control in sorted(view.controls.items()):
        if control.on_failure is not None or not required(view.manifest, control):
            continue
        version = view.manifest.version
        needs = (
            f"; on_failure needs manifest_version {MANIFEST_VERSION}"
            if version < MANIFEST_VERSION
            else ""
        )
        yield Issue(
            f"contract.controls.{cid}.on_failure",
            f"control '{cid}' is autonomous and calls the real system: the user chooses what "
            f"it sends when the twin fails (on_failure.mode: hold, release or safe_value){needs}",
        )
