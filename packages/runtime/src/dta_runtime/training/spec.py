"""What a frame reference asks for, checked against the ontology before anything is read.

A reference names properties, link types and lags as text. Here they become
the types to read and the columns to compute, or a refusal that names the
member at fault. Nothing here touches the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from dta_core.errors import DtaError
from dta_core.schema import Field, Kind, ObjectType, Ontology
from dta_core.store import UnknownProperty, UnknownType

from ..protocol.families import DS, UNIQUE_ID, Y
from ..protocol.worker import FeatureRef, FrameRef

BAD_SPEC = "training.bad_spec"
TOO_LARGE = "training.too_large"
LABEL = "label"
LABEL_DS = "label_ds"
_NUMERIC = (Kind.integer, Kind.float, Kind.boolean)


class TrainingError(DtaError):
    """A frame that cannot be built as asked: `training.bad_spec` (the reference is wrong,
    `detail.member` says where) or `training.too_large`. Both are the caller's to fix."""

    code = BAD_SPEC


def bad_spec(member: str, message: str) -> TrainingError:
    return TrainingError(f"{member}: {message}", detail={"member": member})


@dataclass(frozen=True)
class Hop:
    """One way a link type joins the row's object to a neighbour: the link's field that
    holds the row's object, and the one that holds the neighbour."""

    link_type: str
    row_field: str
    neighbour_field: str


@dataclass(frozen=True)
class Neighbours:
    """The objects a feature aggregates over: which links lead to them and which types
    they are. Features that share one are computed from the same reads."""

    hops: tuple[Hop, ...]
    types: tuple[str, ...]
    property: str


@dataclass(frozen=True)
class Column:
    name: str
    property: str
    # Whole steps of the grid the cell is read back; negative for a label, read ahead.
    lag: int
    # None: a property of the row's own object.
    neighbours: Neighbours | None = None
    agg: str | None = None
    # What the column is, in the words of the reference, for the metadata.
    described: str = ""


@dataclass(frozen=True)
class Spec:
    type: ObjectType
    target: str
    step: timedelta
    features: tuple[Column, ...]
    # Steps ahead at which the label is read; None without one.
    label: int | None

    @property
    def names(self) -> list[str]:
        names = [UNIQUE_ID, DS, Y, *(column.name for column in self.features)]
        return names + ([LABEL, LABEL_DS] if self.label is not None else [])

    def own_properties(self) -> list[str]:
        """Properties of the target's type the frame reads, the target first."""
        found = [self.target]
        for column in self.features:
            if column.neighbours is None and column.property not in found:
                found.append(column.property)
        return found


def _split(text: str, member: str) -> tuple[str, str]:
    type_name, dot, name = text.partition(".")
    if not dot or not type_name or not name or "." in name:
        raise bad_spec(member, f"{text!r} is not written Type.property")
    return type_name, name


def _known(ontology: Ontology, name: str) -> bool:
    return ontology.has_type(name) or name in ontology.abstract


def _numeric(object_type: ObjectType, name: str, member: str) -> Field:
    if not object_type.has_field(name):
        raise UnknownProperty(
            f"{object_type.name} has no property {name!r}",
            detail={"type": object_type.name, "property": name, "member": member},
        )
    field = object_type.field(name)
    if field.kind not in _NUMERIC or field.multivalued:
        kind = f"a list of {field.kind.value}" if field.multivalued else field.kind.value
        raise bad_spec(
            member,
            f"{object_type.name}.{name} is {kind}; a frame holds numbers "
            "(integer, float, or boolean as 0 and 1)",
        )
    return field


def _link_types(ontology: Ontology, via: str, member: str) -> list[ObjectType]:
    """The link types `via` names: itself, or every type under the class it names."""
    if not _known(ontology, via):
        raise UnknownType(f"unknown type {via!r}", detail={"type": via, "member": member})
    found = [ontology.type(name) for name in ontology.concrete(via)]
    if not found or any(candidate.link is None for candidate in found):
        raise bad_spec(member, f"via {via!r} is not a link type")
    return found


def _hops(
    ontology: Ontology,
    links: list[ObjectType],
    row_type: str,
    neighbour_types: list[str],
    direction: str,
) -> list[Hop]:
    hops = []
    for link in links:
        assert link.link is not None
        source, target = link.link
        ends = {
            name: set(ontology.concrete(link.field(name).target or "")) for name in (source, target)
        }
        # `out`: the row's object is the link's source; `in`: its target.
        ways = {"out": [(source, target)], "in": [(target, source)]}
        for row_field, neighbour_field in ways.get(direction, ways["out"] + ways["in"]):
            if row_type in ends[row_field] and ends[neighbour_field] & set(neighbour_types):
                hops.append(Hop(link.name, row_field, neighbour_field))
    return hops


def _lag(feature: FeatureRef, step: timedelta, member: str) -> int:
    if feature.lag < timedelta(0):
        raise bad_spec(member, f"the lag {feature.lag} is negative")
    if feature.lag % step:
        raise bad_spec(member, f"the lag {feature.lag} is not a whole multiple of the step {step}")
    return feature.lag // step


def _feature(
    ontology: Ontology, target_type: ObjectType, feature: FeatureRef, step: timedelta, index: int
) -> Column:
    member = f"features[{index}]"
    type_name, name = _split(feature.property, member)
    if (feature.via is None) != (feature.agg is None):
        given, missing = ("via", "agg") if feature.agg is None else ("agg", "via")
        raise bad_spec(member, f"{given} is given without {missing}: a feature has both or neither")
    own = type_name == target_type.name or type_name in target_type.ancestors
    if not _known(ontology, type_name) and not (own and feature.via is None):
        raise UnknownType(
            f"unknown type {type_name!r}", detail={"type": type_name, "member": member}
        )
    lag = _lag(feature, step, member)
    column = feature.column(step)
    described = feature.property + (f" {lag} steps back" if lag else "")
    if feature.via is None:
        if not own:
            raise bad_spec(
                member,
                f"{feature.property} is a property of another type than the target's "
                f"({target_type.name}); name the link type that joins them as via",
            )
        _numeric(target_type, name, member)
        return Column(column, name, lag, described=described)
    links = _link_types(ontology, feature.via, member)
    neighbour_types = list(ontology.concrete(type_name))
    for neighbour in neighbour_types:
        _numeric(ontology.type(neighbour), name, member)
    hops = _hops(ontology, links, target_type.name, neighbour_types, feature.direction)
    if not hops:
        raise bad_spec(
            member,
            f"no link of {feature.via} joins a {target_type.name} to a {type_name} "
            f"(direction {feature.direction})",
        )
    joined = {
        hop.link_type: set(
            ontology.concrete(ontology.type(hop.link_type).field(hop.neighbour_field).target or "")
        )
        for hop in hops
    }
    reachable = set().union(*joined.values())
    neighbours = Neighbours(
        hops=tuple(hops),
        types=tuple(found for found in neighbour_types if found in reachable),
        property=name,
    )
    described = f"{feature.agg} of {described} over {feature.via} ({feature.direction})"
    return Column(column, name, lag, neighbours=neighbours, agg=feature.agg, described=described)


def resolve(ontology: Ontology, ref: FrameRef, horizon: timedelta | None = None) -> Spec:
    """The reference as types and columns, or the refusal that names what is wrong."""
    if ref.step <= timedelta(0):
        raise bad_spec("step", f"the step {ref.step} is not positive")
    type_name, name = _split(ref.target, "target")
    if type_name in ontology.abstract and not ontology.has_type(type_name):
        raise bad_spec(
            "target",
            f"{type_name} is a class, not a type of objects; name one of "
            f"{', '.join(ontology.abstract[type_name])}",
        )
    if not ontology.has_type(type_name):
        raise UnknownType(
            f"unknown type {type_name!r}", detail={"type": type_name, "member": "target"}
        )
    target_type = ontology.type(type_name)
    _numeric(target_type, name, "target")
    features = tuple(
        _feature(ontology, target_type, feature, ref.step, index)
        for index, feature in enumerate(ref.features)
    )
    label = None
    if horizon is not None:
        if horizon <= timedelta(0) or horizon % ref.step:
            raise bad_spec(
                "horizon", f"the horizon {horizon} is not a whole multiple of the step {ref.step}"
            )
        label = horizon // ref.step
    spec = Spec(target_type, name, ref.step, features, label)
    seen: set[str] = set()
    for column in spec.names:
        if column in seen:
            raise bad_spec("features", f"two columns are named {column!r}")
        seen.add(column)
    return spec
