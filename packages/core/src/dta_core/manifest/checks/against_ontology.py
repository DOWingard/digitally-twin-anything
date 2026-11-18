"""Checks of a manifest against the ontology it names.

The other checks judge the manifest on its own. These need the ontology's
types and properties, so they run only once the ontology file can be loaded;
their findings are added to the others by the caller. They catch at design
time what would otherwise surface as rejected rows during the first load.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...functions import BUILTINS, Builtin, ParamKind, shape_problem
from ...log import Log
from ...schema import UID_FIELD, Kind, Ontology, Role
from ..models import Manifest
from .filters import (
    clause_problem,
    edge_clause_problem,
    precondition_clauses,
    value_problem,
)
from .findings import Finding, Severity
from .view import ManifestView, is_property_ref, split_ref

log = Log("manifest.checks")

SIGNATURE = "ontology.signature"
UNKNOWN_TYPE = "ontology.unknown_type"
UNKNOWN_PROPERTY = "ontology.unknown_property"
ABSTRACT_MAPPING = "ontology.abstract_mapping"
REQUIRED_UNMAPPED = "ontology.required_unmapped"
UNMAPPED_OBSERVATION = "ontology.unmapped_observation"
ROLE = "ontology.role"
UNIT = "ontology.unit"
FUNCTION_PARAM = "ontology.function_param"
ACTION_PRECONDITION = "ontology.action_precondition"

CODES = (
    SIGNATURE,
    UNKNOWN_TYPE,
    UNKNOWN_PROPERTY,
    ABSTRACT_MAPPING,
    REQUIRED_UNMAPPED,
    UNMAPPED_OBSERVATION,
    ROLE,
    UNIT,
    FUNCTION_PARAM,
    ACTION_PRECONDITION,
)
_NUMERIC = (Kind.integer, Kind.float)


def _concrete(ontology: Ontology, type_name: str) -> tuple[str, ...] | None:
    """Concrete types a name stands for; None when the ontology has no such class."""
    if type_name in ontology.abstract:
        return ontology.abstract[type_name]
    if ontology.has_type(type_name):
        return (type_name,)
    return None


def _precondition_problems(
    ontology: Ontology, text: str | None, type_name: str | None
) -> Iterator[tuple[str, str]]:
    """What the twin could not evaluate of an action's precondition: it is judged at every
    proposal against the object acted on, so each clause must be readable for every
    concrete type the action's object type stands for."""
    if not text:
        return
    if not type_name:
        yield ACTION_PRECONDITION, "a precondition needs the action's object_type or control"
        return
    members = _concrete(ontology, type_name)
    if members is None:
        return  # reported as an unknown type
    clauses = precondition_clauses(text)
    if not clauses:
        yield ACTION_PRECONDITION, "the precondition holds no clause"
    for clause in clauses:
        problem = clause_problem(clause)
        if problem is not None:
            yield ACTION_PRECONDITION, f"'{clause}': {problem}"
            continue
        prop = clause.split(":", 1)[0].strip()
        if prop == UID_FIELD:
            continue
        lacking = [member for member in members if not ontology.type(member).has_field(prop)]
        if lacking:
            yield UNKNOWN_PROPERTY, f"'{clause}': {', '.join(lacking)} has no property {prop}"
            continue
        for member in members:
            problem = value_problem(clause, ontology.type(member).field(prop))
            if problem is not None:
                yield ACTION_PRECONDITION, f"'{clause}': {problem}"
                break


def _references(view: ManifestView) -> Iterator[tuple[str, str]]:
    """Every `Type.property` the manifest names outside its mappings, with its path."""
    for qid, question in sorted(view.questions.items()):
        for ref in question.targets or []:
            yield f"contract.questions.{qid}.targets", ref
    for cid, control in sorted(view.controls.items()):
        if control.target:
            yield f"contract.controls.{cid}.target", control.target
    boundary = view.contract.boundary if view.contract else None
    if boundary is not None:
        for role in ("state", "controls", "drivers", "observations"):
            for ref in getattr(boundary, role) or []:
                yield f"contract.boundary.{role}", ref
    for mid, model in sorted(view.models.items()):
        if model.target:
            yield f"models.{mid}.target", model.target
        for ref in model.features or []:
            yield f"models.{mid}.features", ref


def _link_types(ontology: Ontology, type_name: str) -> tuple[str, ...] | None:
    """Link types a name stands for; None when the ontology has no such class."""
    concrete = _concrete(ontology, type_name)
    if concrete is None:
        return None
    return tuple(name for name in concrete if ontology.type(name).link is not None)


def _function_findings(
    base: str, builtin: Builtin, params: dict[str, object], ontology: Ontology
) -> Iterator[tuple[str, str, str]]:
    """(code, path, message) for each fixed parameter of a built-in that names something the
    ontology does not have, or has in another form than the function needs."""
    given = {
        name: value
        for name, value in params.items()
        if (parameter := builtin.parameter(name)) is not None
        and shape_problem(parameter, value) is None  # a wrong shape is reported elsewhere
    }
    # The types the property parameter stands for, once it is known to exist.
    holders: tuple[str, ...] = ()
    for name, value in sorted(given.items()):
        parameter = builtin.parameter(name)
        if parameter is None or parameter.kind != ParamKind.property:
            continue
        path = f"{base}.params.{name}"
        type_name, prop = split_ref(str(value))
        concrete = _concrete(ontology, type_name)
        if concrete is None:
            yield UNKNOWN_TYPE, path, f"{value}: the ontology has no type {type_name}"
            continue
        if not all(ontology.type(member).has_field(prop) for member in concrete):
            yield UNKNOWN_PROPERTY, path, f"{value}: {type_name} has no property {prop}"
            continue
        holders = concrete
        if parameter.numeric and any(
            ontology.type(member).field(prop).kind not in _NUMERIC for member in concrete
        ):
            yield (
                FUNCTION_PARAM,
                path,
                f"{value} does not hold numbers; {builtin.entry} needs a numeric property",
            )

    followed: list[str] | None = None
    edge_types = given.get("edge_types")
    if builtin.parameter("edge_types") is not None and edge_types is not None:
        followed = []
        for type_name in edge_types:  # type: ignore[union-attr]
            links = _link_types(ontology, type_name)
            path = f"{base}.params.edge_types"
            if links is None:
                yield UNKNOWN_TYPE, path, f"the ontology has no type {type_name}"
            elif not links:
                yield FUNCTION_PARAM, path, f"{type_name} is not a link type"
            else:
                followed.extend(links)

    for clause in given.get("edge_where") or ():  # type: ignore[union-attr]
        path = f"{base}.params.edge_where"
        if edge_clause_problem(str(clause)) is not None:
            continue  # a clause the twin cannot read is reported by ref.function_entry
        head = str(clause).split(":", 1)[0]
        type_name, _, prop = head.partition(".")
        prop = prop.strip()
        links = _link_types(ontology, type_name.strip())
        if links is None:
            yield UNKNOWN_TYPE, path, f"'{clause}': the ontology has no type {type_name}"
        elif not links:
            yield FUNCTION_PARAM, path, f"'{clause}': {type_name} is not a link type"
        elif prop != UID_FIELD and not all(
            ontology.type(member).has_field(prop) for member in links
        ):
            yield UNKNOWN_PROPERTY, path, f"'{clause}': {type_name} has no property {prop}"
        elif prop != UID_FIELD:
            rest = str(clause)[str(clause).index(".") + 1 :]
            for member in links:
                problem = value_problem(rest, ontology.type(member).field(prop))
                if problem is not None:
                    yield FUNCTION_PARAM, path, f"'{clause}': {problem}"
                    break

    weight = given.get("weight")
    if weight is not None and followed:
        for member in followed:
            link = ontology.type(member)
            path = f"{base}.params.weight"
            if not link.has_field(str(weight)):
                yield UNKNOWN_PROPERTY, path, f"{member} has no property {weight}"
            elif link.field(str(weight)).kind not in _NUMERIC:
                yield FUNCTION_PARAM, path, f"{member}.{weight} does not hold numbers"

    if holders:
        group_by = given.get("group_by")
        if group_by is not None and not all(
            ontology.type(member).has_field(str(group_by)) for member in holders
        ):
            yield (
                UNKNOWN_PROPERTY,
                f"{base}.params.group_by",
                f"{', '.join(holders)} has no property {group_by}",
            )
        for clause in given.get("where") or ():  # type: ignore[union-attr]
            if clause_problem(str(clause)) is not None:
                continue  # a clause the twin cannot read is reported by ref.function_entry
            prop = str(clause).split(":", 1)[0].strip()
            if prop == UID_FIELD:
                continue
            if not all(ontology.type(member).has_field(prop) for member in holders):
                yield (
                    UNKNOWN_PROPERTY,
                    f"{base}.params.where",
                    f"'{clause}': {', '.join(holders)} has no property {prop}",
                )
                continue
            for member in holders:
                problem = value_problem(str(clause), ontology.type(member).field(prop))
                if problem is not None:
                    yield FUNCTION_PARAM, f"{base}.params.where", f"'{clause}': {problem}"
                    break


def ontology_findings(manifest: Manifest, ontology: Ontology, signature: str) -> list[Finding]:
    """What in the manifest does not fit the ontology.

    `signature` is the type signature of `ontology`, computed by the caller.
    """
    view = ManifestView(manifest)
    found: list[Finding] = []

    def report(code: str, path: str, message: str, severity: Severity = Severity.error) -> None:
        found.append(Finding(code=code, severity=severity, path=path, message=message))

    confirmed = manifest.ontology.type_signature if manifest.ontology else None
    if confirmed is not None and confirmed != signature:
        report(
            SIGNATURE,
            "ontology.type_signature",
            "the ontology's types changed since they were confirmed; confirm them again",
        )

    for path, ref in _references(view):
        if not is_property_ref(ref):
            continue  # malformed references are reported by the manifest's own checks
        type_name, prop = split_ref(ref)
        concrete = _concrete(ontology, type_name)
        if concrete is None:
            report(UNKNOWN_TYPE, path, f"{ref}: the ontology has no type {type_name}")
        elif not all(ontology.type(name).has_field(prop) for name in concrete):
            report(UNKNOWN_PROPERTY, path, f"{ref}: {type_name} has no property {prop}")

    for fid, function in sorted(view.functions.items()):
        builtin = BUILTINS.get(function.entry or "") if function.kind == "builtin" else None
        if builtin is None:
            continue  # an entry that is no built-in is reported by the manifest's own checks
        for code, path, message in _function_findings(
            f"functions.{fid}", builtin, function.params, ontology
        ):
            report(code, path, message)

    for aid, action in sorted(view.actions.items()):
        if action.object_type and _concrete(ontology, action.object_type) is None:
            report(
                UNKNOWN_TYPE,
                f"actions.{aid}.object_type",
                f"the ontology has no type {action.object_type}",
            )
        acted_on = action.object_type
        controls = manifest.contract.controls if manifest.contract else {}
        if not acted_on and action.control in controls:
            # A controlled action acts on the type of its control's target.
            acted_on = (controls[action.control].target or "").partition(".")[0] or None
        for code, message in _precondition_problems(ontology, action.precondition, acted_on):
            report(code, f"actions.{aid}.precondition", message)

    mapped: dict[str, set[str]] = {}
    first_mapping: dict[str, str] = {}
    for sid, source in sorted(view.sources.items()):
        for mid, mapping in sorted((source.mappings or {}).items()):
            base = f"sources.{sid}.mappings.{mid}"
            type_name = mapping.object_type
            if not type_name:
                continue
            if type_name in ontology.abstract:
                report(
                    ABSTRACT_MAPPING,
                    f"{base}.object_type",
                    f"{type_name} is abstract; rows become objects of one of "
                    f"{', '.join(ontology.abstract[type_name])}",
                )
                continue
            if not ontology.has_type(type_name):
                report(UNKNOWN_TYPE, f"{base}.object_type", f"the ontology has no type {type_name}")
                continue
            object_type = ontology.type(type_name)
            first_mapping.setdefault(type_name, base)
            names = mapped.setdefault(type_name, set())
            for prop, how in sorted((mapping.properties or {}).items()):
                path = f"{base}.properties.{prop}"
                if not object_type.has_field(prop):
                    report(UNKNOWN_PROPERTY, path, f"{type_name} has no property {prop}")
                    continue
                names.add(prop)
                field = object_type.field(prop)
                timed = mapping.valid_time is not None
                if timed and field.role == Role.static and not object_type.event:
                    report(
                        ROLE,
                        path,
                        f"{type_name}.{prop} is declared fixed but is mapped as a value that "
                        "changes with time",
                        Severity.warning,
                    )
                elif not timed and field.role == Role.state:
                    report(
                        ROLE,
                        path,
                        f"{type_name}.{prop} changes with time but its mapping names no "
                        "valid_time column",
                        Severity.warning,
                    )
                if field.unit and not how.unit:
                    report(
                        UNIT,
                        path,
                        f"no source unit is stated; values are taken to be in {field.unit}",
                        Severity.warning,
                    )
                elif how.unit and not field.unit:
                    report(
                        UNIT,
                        path,
                        f"a source unit ({how.unit}) is stated but {type_name}.{prop} has no unit",
                        Severity.warning,
                    )

    for type_name, names in sorted(mapped.items()):
        missing = [field.name for field in ontology.type(type_name).fields if field.required]
        missing = [name for name in missing if name not in names]
        if missing:
            report(
                REQUIRED_UNMAPPED,
                f"{first_mapping[type_name]}.properties",
                f"no mapping fills the required {', '.join(missing)} of {type_name}; every "
                "row would be refused",
            )

    boundary = view.contract.boundary if view.contract else None
    for ref in (boundary.observations or []) if boundary is not None else []:
        if not is_property_ref(ref):
            continue
        type_name, prop = split_ref(ref)
        concrete = _concrete(ontology, type_name)
        if concrete is None or not all(ontology.type(name).has_field(prop) for name in concrete):
            continue  # already reported above
        if not any(prop in mapped.get(name, ()) for name in concrete):
            report(
                UNMAPPED_OBSERVATION,
                "contract.boundary.observations",
                f"{ref} is listed as observed but no mapping fills it",
            )

    found.sort(key=lambda finding: (finding.path, finding.code, finding.message))
    log.debug(
        "manifest checked against its ontology",
        errors=sum(1 for finding in found if finding.severity == Severity.error),
        warnings=sum(1 for finding in found if finding.severity == Severity.warning),
    )
    return found
