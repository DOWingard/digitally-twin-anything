"""The functions shipped with the runtime: their entries and the parameters each takes.

One table, read by the manifest's checks (an entry that does not exist, or a
fixed parameter that cannot be right, is refused before anything is built),
by the runtime that runs the functions and by the operator skill that
describes them.

A parameter is given either in the manifest (`functions.<id>.params`: fixed
for that function) or with the call. Every call also takes the version and
the branch it reads at; those are not parameters of the table.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter, ValidationError

PROPERTY_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Entry of a function supplied as code: a module path and a function in it.
REGISTERED_ENTRY = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)

_DURATION = TypeAdapter(timedelta)
# ISO 8601 only: a bare number or `1 day` would leave the unit to guesswork.
_ISO_DURATION = re.compile(r"^P(?=\d|T\d)(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$")


class ParamKind(StrEnum):
    # `Type.property` of the ontology.
    property = "property"
    # The name of a property, of a type the other parameters decide.
    name = "name"
    number = "number"
    numbers = "numbers"
    integer = "integer"
    boolean = "boolean"
    # A UCUM unit code.
    unit = "unit"
    # ISO 8601, such as PT15M.
    duration = "duration"
    # Any version selector: `head`, a version id, `#<n>` or a time.
    version = "version"
    uid = "uid"
    uids = "uids"
    # Names of types of the ontology.
    types = "types"
    # `<property>:<op>:<value>` clauses over the properties of one type.
    filters = "filters"
    # `<LinkType>.<property>:<op>:<value>` clauses over the links followed.
    edge_filters = "edge_filters"
    choice = "choice"
    # The id of a question of the contract.
    question = "question"


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: ParamKind
    description: str
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    # For a property (or the name of one): it has to hold numbers.
    numeric: bool = False


@dataclass(frozen=True)
class Builtin:
    entry: str
    description: str
    parameters: tuple[Parameter, ...]

    def parameter(self, name: str) -> Parameter | None:
        return next((found for found in self.parameters if found.name == name), None)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(found.name for found in self.parameters if found.required)


def _p(name: str, kind: ParamKind, description: str, **more: Any) -> Parameter:
    return Parameter(name=name, kind=kind, description=description, **more)


_K = ParamKind
_UIDS = _p("uids", _K.uids, "Only these objects; every object of the type when absent")
_WHERE = _p("where", _K.filters, "Only objects of the property's type that pass these filters")


def _threshold(entry: str, word: str) -> Builtin:
    return Builtin(
        entry,
        f"Objects whose value in force is {word} a threshold: at one version, or over a range "
        "of versions with the times it was",
        (
            _p("property", _K.property, "The property compared", required=True, numeric=True),
            _p("threshold", _K.number, "The bound", required=True),
            _p("unit", _K.unit, "Unit of the threshold; the property's unit when absent"),
            _p("from", _K.version, "First version of a range; with `to`"),
            _p("to", _K.version, "Last version of a range; with `from`"),
            _UIDS,
            _WHERE,
        ),
    )


_BUILTINS = (
    Builtin(
        "latest",
        "The value in force of a property, per object, at a version",
        (
            _p("property", _K.property, "The property read", required=True),
            _UIDS,
            _WHERE,
            _p("against", _K.version, "An earlier version: adds the value then and the change"),
        ),
    ),
    _threshold("below", "below"),
    _threshold("above", "above"),
    Builtin(
        "integrate",
        "The integral over time of a property, each value held from its version until the "
        "next (left step), per object and period",
        (
            _p("property", _K.property, "The property integrated", required=True, numeric=True),
            _p("from", _K.version, "First version of the range", required=True),
            _p("to", _K.version, "Last version of the range", required=True),
            _p(
                "period",
                _K.duration,
                "One value per object and period, periods counted from the Unix epoch in UTC; "
                "one value per object when absent",
            ),
            _p("unit", _K.unit, "Unit of the result; the property's unit times seconds if absent"),
            _UIDS,
        ),
    ),
    Builtin(
        "paths",
        "Paths between two objects with the fewest links, or the least total weight",
        (
            _p("source", _K.uid, "Where the paths start", required=True),
            _p("target", _K.uid, "Where the paths end", required=True),
            _p("edge_types", _K.types, "Link types followed; every link type when absent"),
            _p("edge_where", _K.edge_filters, "Only links that pass these filters are followed"),
            _p("weight", _K.name, "Property of the links summed along a path", numeric=True),
            _p("default_weight", _K.number, "Weight of a link that holds none"),
            _p(
                "direction",
                _K.choice,
                "Way links are followed",
                default="both",
                choices=("both", "out", "in"),
            ),
            _p("k", _K.integer, "Most paths returned", default=1),
            _p("max_depth", _K.integer, "Most links in a path"),
        ),
    ),
    Builtin(
        "aggregate",
        "One number over the objects of a type, or one per value of another of its properties",
        (
            _p("property", _K.property, "The property aggregated", required=True),
            _p(
                "op",
                _K.choice,
                "How its values are combined",
                required=True,
                choices=("sum", "mean", "min", "max", "count"),
            ),
            _p(
                "group_by",
                _K.name,
                "A property of the same type: one result per value of it, which for a "
                "reference is one per object referred to",
            ),
            _UIDS,
            _WHERE,
        ),
    ),
    Builtin(
        "series",
        "The history of a property over a range of versions, per object",
        (
            _p("property", _K.property, "The property read", required=True),
            _p("from", _K.version, "First version of the range", required=True),
            _p("to", _K.version, "Last version of the range", required=True),
            _UIDS,
            _p(
                "step",
                _K.duration,
                "The value in force on a regular grid of this step; on the versions of the "
                "range when absent",
            ),
        ),
    ),
    Builtin(
        "forecast",
        "What the active model of a question predicts from a version on",
        (
            _p("question", _K.question, "The question whose model predicts", required=True),
            _p("horizon", _K.duration, "How far ahead; the question's horizon when absent"),
            _UIDS,
            _p("levels", _K.numbers, "Coverage of the intervals in percent", default=(80, 95)),
        ),
    ),
)

BUILTINS: Mapping[str, Builtin] = {found.entry: found for found in _BUILTINS}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_texts(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and all(isinstance(item, str) and item for item in value)
    )


def shape_problem(parameter: Parameter, value: Any) -> str | None:
    """Why `value` cannot be a value of the parameter; None when it can.

    Only the shape is judged: whether a property, a type or a unit exists is
    for whoever holds the ontology and the units.
    """
    kind = parameter.kind
    if kind == _K.property:
        ok = isinstance(value, str) and bool(PROPERTY_REF.match(value))
        return None if ok else "must be a property written Type.property"
    if kind == _K.name:
        ok = isinstance(value, str) and bool(NAME.match(value))
        return None if ok else "must be the name of a property"
    if kind == _K.number:
        return None if _is_number(value) else "must be a finite number"
    if kind == _K.numbers:
        ok = (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) > 0
            and all(_is_number(item) for item in value)
        )
        return None if ok else "must be a list of finite numbers"
    if kind == _K.integer:
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 1
        return None if ok else "must be a whole number of at least 1"
    if kind == _K.boolean:
        return None if isinstance(value, bool) else "must be true or false"
    if kind == _K.duration:
        if isinstance(value, timedelta):
            return None if value > timedelta(0) else "must be longer than nothing"
        if not isinstance(value, str) or not _ISO_DURATION.match(value):
            return "must be an ISO 8601 duration such as PT15M"
        try:
            parsed = _DURATION.validate_strings(value)
        except ValidationError:
            return "must be an ISO 8601 duration such as PT15M"
        return None if parsed > timedelta(0) else "must be longer than nothing"
    if kind == _K.choice:
        ok = isinstance(value, str) and value in parameter.choices
        return None if ok else f"must be one of {', '.join(parameter.choices)}"
    if kind in (_K.unit, _K.version, _K.uid, _K.question):
        ok = isinstance(value, str) and bool(value.strip())
        return None if ok else "must be a text that is not empty"
    if kind in (_K.uids, _K.types, _K.filters, _K.edge_filters):
        return None if _is_texts(value) and len(value) > 0 else "must be a list of texts"
    raise AssertionError(f"no shape rule for parameter kind {kind}")


__all__ = [
    "BUILTINS",
    "PROPERTY_REF",
    "REGISTERED_ENTRY",
    "Builtin",
    "ParamKind",
    "Parameter",
    "shape_problem",
]
