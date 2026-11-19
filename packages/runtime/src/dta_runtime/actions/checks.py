"""Whether a proposal can be carried out as asked: its object, its parameters and the
bounds of the control it changes.

Every problem found is given, each with the member of the proposal it is about, so a
caller sees all it has to change at once. Nothing here writes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dta_core.schema import Field, Kind, Ontology
from dta_core.store import StoredObject

from .errors import reason

VALUE = "value"
# The type a parameter `value` has when an action that carries out a control does not
# declare it: that of the property the control sets.
_KIND_TYPES = {
    Kind.float: "number",
    Kind.integer: "integer",
    Kind.boolean: "boolean",
    Kind.string: "string",
    Kind.enum: "string",
}


@dataclass
class Judged:
    """What a proposal came to before the gate: the problems found, the parameters as
    they are recorded (whole numbers given as such), and the value of the control's
    target in force on the object, when there is one."""

    reasons: list[dict[str, str]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    in_force: Any = None


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def same(left: Any, right: Any) -> bool:
    """Equality that does not take `True` for `1`."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if is_number(left) and is_number(right):
        return float(left) == float(right)
    return type(left) is type(right) and left == right


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if is_number(value):
        return f"{value:g}"
    return repr(value)


def target_field(ontology: Ontology, target: str | None, type_name: str | None) -> Field | None:
    """The property a control sets, as the object's own type has it."""
    if not target or "." not in target:
        return None
    declared, _, name = target.partition(".")
    candidates = [type_name] if type_name else []
    try:
        candidates.extend(ontology.concrete(declared))
    except KeyError:
        pass
    for candidate in candidates:
        if candidate and ontology.has_type(candidate) and ontology.type(candidate).has_field(name):
            return ontology.type(candidate).field(name)
    return None


def _typed(name: str, spec: Mapping[str, Any], value: Any, found: Judged) -> Any:
    """The value as it is recorded, or None after noting why it cannot be."""
    member = f"params.{name}"
    wanted = spec.get("type")
    if wanted == "number" and not is_number(value):
        found.reasons.append(reason(member, f"must be a number, not {_shown(value)}"))
        return None
    if wanted == "integer":
        if is_number(value) and float(value).is_integer():
            value = int(value)
        else:
            found.reasons.append(reason(member, f"must be a whole number, not {_shown(value)}"))
            return None
    if wanted == "string" and not isinstance(value, str):
        found.reasons.append(reason(member, f"must be text, not {_shown(value)}"))
        return None
    if wanted == "boolean" and not isinstance(value, bool):
        found.reasons.append(reason(member, f"must be true or false, not {_shown(value)}"))
        return None
    if wanted is None and not (value is None or isinstance(value, str | bool) or is_number(value)):
        found.reasons.append(reason(member, "must be a number, text, true or false"))
        return None
    return value


def _limited(member: str, value: Any, limits: Mapping[str, Any], whose: str) -> list[dict]:
    """The problems of a value against a minimum, a maximum and a list of allowed values."""
    problems = []
    low, high, allowed = limits.get("minimum"), limits.get("maximum"), limits.get("allowed")
    if (low is not None or high is not None) and not is_number(value):
        problems.append(reason(member, f"{whose} limits it to numbers, not {_shown(value)}"))
    else:
        if low is not None and value < low:
            problems.append(reason(member, f"{_shown(value)} is below {whose} minimum {low:g}"))
        if high is not None and value > high:
            problems.append(reason(member, f"{_shown(value)} is above {whose} maximum {high:g}"))
    if allowed is not None and not any(same(value, item) for item in allowed):
        shown = ", ".join(_shown(item) for item in allowed)
        problems.append(reason(member, f"{_shown(value)} is not one of {whose} values: {shown}"))
    return problems


def _target_kind(field_: Field, value: Any, ontology: Ontology) -> str | None:
    """Why a value cannot be held by the property the control sets; None when it can."""
    kind = field_.kind
    if kind == Kind.float and not is_number(value):
        return f"{field_.name} holds numbers, not {_shown(value)}"
    if kind == Kind.integer and not (is_number(value) and float(value).is_integer()):
        return f"{field_.name} holds whole numbers, not {_shown(value)}"
    if kind == Kind.boolean and not isinstance(value, bool):
        return f"{field_.name} holds true or false, not {_shown(value)}"
    if kind == Kind.string and not isinstance(value, str):
        return f"{field_.name} holds text, not {_shown(value)}"
    if kind == Kind.enum:
        choices = ontology.enums.get(field_.target or "", ())
        if not isinstance(value, str) or (choices and value not in choices):
            return f"{field_.name} holds one of {', '.join(choices)}, not {_shown(value)}"
    return None


def judge(
    ontology: Ontology,
    action: Mapping[str, Any],
    *,
    object_uid: str,
    stored: StoredObject | None,
    params: Any,
    step_limit: bool = True,
) -> Judged:
    """Every problem of a proposal of `action` (its document) on the object `stored`
    (as the head holds it, None when there is none) with `params`.

    `step_limit` false leaves the control's max_step out: it bounds what a model may
    change in one action, measured from the value in force, and a fail-safe sends a
    constant the user signed, at a moment when the value in force may be unknown or far
    away. Every other bound of the control still applies."""
    found = Judged()
    object_type = action.get("object_type")
    if stored is None:
        found.reasons.append(reason("object", f"there is no object {object_uid}"))
    elif object_type:
        try:
            allowed = ontology.concrete(object_type)
        except KeyError:
            allowed = ()
        if stored.type not in allowed:
            found.reasons.append(
                reason(
                    "object",
                    f"{object_uid} is a {stored.type}; {action['id']} acts on {object_type}",
                )
            )

    declared: dict[str, Any] = dict(action.get("parameters") or {})
    target = action.get("target")
    controlled = bool(action.get("control"))
    target_type = stored.type if stored is not None else None
    property_ = target_field(ontology, target, target_type) if controlled else None
    if controlled and VALUE not in declared:
        kind = _KIND_TYPES.get(property_.kind) if property_ is not None else None
        declared[VALUE] = {"type": kind} if kind else {}

    if not isinstance(params, Mapping) or not all(isinstance(name, str) for name in params):
        found.reasons.append(reason("params", "must be an object of parameter names to values"))
        return found
    for name in sorted(set(declared) - set(params)):
        found.reasons.append(reason(f"params.{name}", "is missing"))
    for name in sorted(set(params) - set(declared)):
        found.reasons.append(reason(f"params.{name}", f"is not a parameter of {action['id']}"))
    valid: dict[str, Any] = {}
    for name in sorted(set(params) & set(declared)):
        spec = declared[name] or {}
        before = len(found.reasons)
        value = _typed(name, spec, params[name], found)
        if len(found.reasons) > before:
            continue
        problems = _limited(f"params.{name}", value, spec, "the parameter's")
        found.reasons.extend(problems)
        if not problems:
            valid[name] = value
    found.params = {name: valid.get(name, params[name]) for name in params}

    if not controlled or VALUE not in valid:
        return found
    value = valid[VALUE]
    if property_ is not None:
        problem = _target_kind(property_, value, ontology)
        if problem is not None:
            found.reasons.append(reason("params.value", problem))
            return found
    bounds = action.get("bounds") or {}
    found.reasons.extend(_limited("params.value", value, bounds, "the control's"))
    step = bounds.get("max_step")
    if step is None or stored is None or not step_limit:
        return found
    name = target.partition(".")[2] if target else ""
    found.in_force = stored.values.get(name)
    if not is_number(value):
        found.reasons.append(reason("params.value", "the control's max_step applies to numbers"))
    elif not is_number(found.in_force):
        found.reasons.append(
            reason(
                "params.value",
                f"the control's max_step cannot be checked: {object_uid} has no {target} in force",
            )
        )
    elif abs(value - found.in_force) > step:
        found.reasons.append(
            reason(
                "params.value",
                f"a change of {abs(value - found.in_force):g} from {found.in_force:g} is larger "
                f"than the control's max_step {step:g}",
            )
        )
    return found
