"""The parameters of one call of a function: the manifest's fixed ones and the call's.

A parameter is given in one of the two places, never both. Each value is judged
against the kind its parameter has in the table of built-ins and read into the
Python value the built-in works with. What only the ontology can decide (whether
a property exists, whether a unit converts) is left to the run.

A call that arrives in a query string carries text only, so a call's value given
as text is read as its kind first: a number, a whole number, true or false, a
list separated by commas. A filter clause may hold commas of its own (`in` takes
a list), so one text is one clause and several clauses are given repeated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from dta_core.functions import BUILTINS, Builtin, Parameter, ParamKind, shape_problem
from dta_core.log import Log
from dta_core.manifest.models import FunctionSpec
from dta_core.store import InvalidInput, Selector
from pydantic import TypeAdapter

from ..filters import repair_plus
from .errors import (
    BAD_PARAM,
    PARAM_FIXED,
    PARAM_MISSING,
    PARAM_UNKNOWN,
    UNKNOWN_ENTRY,
    FunctionError,
    refused,
)

log = Log("functions.params")

_K = ParamKind
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_INTEGER = re.compile(r"^[+-]?\d+$")
_TRUE = ("true", "1")
_FALSE = ("false", "0")
_DURATION = TypeAdapter(timedelta)
_TEXT_LISTS = (_K.uids, _K.types)
_CLAUSES = (_K.filters, _K.edge_filters)


def _number(value: Any) -> Any:
    """A number written as text; anything else as it is, for the shape rule to refuse."""
    if isinstance(value, str) and _NUMBER.match(value.strip()):
        return float(value)
    return value


def _split(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def from_text(parameter: Parameter, value: Any) -> Any:
    """A call's value as the kind of its parameter when it was given as text."""
    kind = parameter.kind
    if kind == _K.number:
        return _number(value)
    if kind == _K.numbers:
        if isinstance(value, str):
            return [_number(item) for item in _split(value)]
        if isinstance(value, Sequence) and not isinstance(value, bytes):
            return [_number(item) for item in value]
        return value
    if not isinstance(value, str):
        return value
    if kind == _K.integer:
        return int(value) if _INTEGER.match(value.strip()) else value
    if kind == _K.boolean:
        lowered = value.strip().lower()
        return True if lowered in _TRUE else False if lowered in _FALSE else value
    if kind in _TEXT_LISTS:
        return _split(value)
    if kind in _CLAUSES:
        return [value]
    return value


def selector_of(function_id: str, name: str, value: Any) -> Selector:
    """The one version a parameter names, not yet looked up on a branch."""
    if isinstance(value, Selector):
        selector = value
    else:
        try:
            # A `+` left unencoded in a query string arrives as a space.
            selector = Selector.parse(repair_plus(value) if isinstance(value, str) else value)
        except InvalidInput as exc:
            raise _refuse(
                BAD_PARAM, f"{function_id}: {name}: {exc.message}", function_id, name
            ) from exc
    if selector.is_range:
        raise _refuse(
            BAD_PARAM,
            f"{function_id}: {name} names one version, not a range: {selector}",
            function_id,
            name,
        )
    return selector


def _python(function_id: str, parameter: Parameter, value: Any) -> Any:
    """A value of the right shape as the Python value a built-in takes."""
    kind = parameter.kind
    if kind == _K.number:
        return float(value)
    if kind == _K.numbers:
        return tuple(float(item) for item in value)
    if kind == _K.duration:
        return value if isinstance(value, timedelta) else _DURATION.validate_strings(value)
    if kind == _K.version:
        return selector_of(function_id, parameter.name, value)
    if kind in _TEXT_LISTS:
        return list(dict.fromkeys(value))
    if kind in _CLAUSES:
        return list(value)
    if kind in (_K.unit, _K.uid, _K.question):
        return value.strip()
    return value


def _refuse(code: str, message: str, function_id: str, name: str, **detail: Any) -> FunctionError:
    log.warn("parameters refused", code=code, function=function_id, parameter=name, **detail)
    return refused(code, message, function=function_id, parameter=name, **detail)


def builtin_of(function_id: str, entry: str | None) -> Builtin:
    found = BUILTINS.get(entry or "")
    if found is None:
        log.warn("no such built-in", function=function_id, entry=entry)
        raise refused(
            UNKNOWN_ENTRY,
            f"{function_id}: there is no built-in function {entry!r}; "
            f"the built-ins are {', '.join(sorted(BUILTINS))}",
            function=function_id,
            entry=entry,
        )
    return found


def resolve(function_id: str, spec: FunctionSpec, call: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of one call: the manifest's fixed ones and the call's, each read
    into the value of its kind.

    Refused with `FunctionError`: a parameter the entry does not have
    (`function.param_unknown`), one that is fixed and also given with the call, even
    with the same value (`function.param_fixed`), a required one given in neither
    place (`function.param_missing`), a value of the wrong shape (`function.bad_param`).

    A function supplied as code has no table to be judged against: its fixed
    arguments and the call's are put together, and only one given twice is refused.
    """
    fixed = dict(spec.params or {})
    given = dict(call)
    twice = sorted(set(fixed) & set(given))
    if spec.kind != "builtin":
        if twice:
            raise _refuse(
                PARAM_FIXED,
                f"{function_id}: {twice[0]} is fixed by the manifest and cannot be given "
                "with the call",
                function_id,
                twice[0],
            )
        return {**fixed, **given}
    builtin = builtin_of(function_id, spec.entry)
    for name in (*fixed, *given):
        if builtin.parameter(name) is None:
            known = ", ".join(parameter.name for parameter in builtin.parameters)
            raise _refuse(
                PARAM_UNKNOWN,
                f"{function_id}: {builtin.entry} has no parameter {name!r}; it takes {known}",
                function_id,
                name,
            )
    if twice:
        raise _refuse(
            PARAM_FIXED,
            f"{function_id}: {twice[0]} is fixed by the manifest and cannot be given with the call",
            function_id,
            twice[0],
        )
    for name in builtin.required:
        if name not in fixed and name not in given:
            raise _refuse(
                PARAM_MISSING,
                f"{function_id}: {builtin.entry} needs {name}: "
                f"{builtin.parameter(name).description}",  # type: ignore[union-attr]
                function_id,
                name,
            )
    values: dict[str, Any] = {}
    for parameter in builtin.parameters:
        name = parameter.name
        if name in fixed:
            value = fixed[name]
        elif name in given:
            value = from_text(parameter, given[name])
        else:
            continue
        problem = shape_problem(parameter, value)
        if problem is not None:
            raise _refuse(
                BAD_PARAM, f"{function_id}: {name} {problem}", function_id, name, problem=problem
            )
        values[name] = _python(function_id, parameter, value)
    log.debug(
        "parameters resolved",
        function=function_id,
        entry=builtin.entry,
        fixed=sorted(fixed),
        given=sorted(given),
    )
    return values
