"""Reading a domain ontology written in LinkML into engine-neutral descriptors.

The domain file is merged with the shared meta-model and every convention is
checked in one pass; all problems found are raised together so an author can
fix a file in one round instead of one error at a time. Nothing outside the
conventions is dropped silently: it is either mapped or reported.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from linkml_runtime.linkml_model.meta import SchemaDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView

from ..log import Log
from ..schema import (
    CLASS_NAME,
    FIELD_NAME,
    META_CLASS_PREFIX,
    UID_FIELD,
    Field,
    Kind,
    ObjectType,
    Ontology,
    Role,
)
from .errors import INVALID, UNREADABLE, OntologyError, Problem
from .expressions import screen_expression

log = Log("ontology.loader")

META_MODEL_PATH = Path(__file__).resolve().parent.parent / "metamodel" / "dta_meta.yaml"
ALLOWED_IMPORT = "linkml:types"
DEFAULT_RANGE = "string"
CHECK_PREFIX = "dta_check_"

# Built-in LinkML types a property may use, and the kind each one becomes.
_BUILTIN_KINDS = {
    "string": Kind.string,
    "uri": Kind.string,
    "uriorcurie": Kind.string,
    "curie": Kind.string,
    "ncname": Kind.string,
    "integer": Kind.integer,
    "float": Kind.float,
    "double": Kind.float,
    "decimal": Kind.float,
    "boolean": Kind.boolean,
    "datetime": Kind.datetime,
    "date": Kind.date,
}
_NUMERIC = (Kind.integer, Kind.float)
_ANY_URIS = ("linkml:Any", "https://w3id.org/linkml/Any")
_SLOT_ANNOTATIONS = frozenset({"dta_role", "dta_long"})
_CLASS_ANNOTATIONS = frozenset({"dta_event", "link_source", "link_target"})
# LinkML constraints the descriptors cannot carry; accepting them would drop them silently.
_UNSUPPORTED_SLOT_CONSTRAINTS = (
    "equals_string",
    "equals_string_in",
    "equals_number",
    "equals_expression",
    "structured_pattern",
    "minimum_cardinality",
    "maximum_cardinality",
    "exact_cardinality",
    "value_presence",
    "ifabsent",
    "designates_type",
    "has_member",
    "all_members",
)
_RANGE_EXPRESSIONS = ("any_of", "exactly_one_of", "all_of", "none_of")
_UNSUPPORTED_CLASS_CONSTRAINTS = (
    "rules",
    "unique_keys",
    "classification_rules",
    "slot_conditions",
    "any_of",
    "exactly_one_of",
    "all_of",
    "none_of",
)


@dataclass(frozen=True)
class ParsedOntology:
    """The descriptors together with the merged LinkML document they came from."""

    ontology: Ontology
    schema: dict[str, Any]  # domain classes and enums plus the meta-model's


def load_ontology(path: str | Path) -> Ontology:
    """Read a domain LinkML file, merge in the meta-model and return its descriptors.

    Raises `OntologyError` with code `ontology.unreadable` when the file cannot
    be read as YAML/LinkML, and `ontology.invalid` listing every convention it
    breaks otherwise.
    """
    path = Path(path)
    started = time.perf_counter()
    log.debug("loading ontology", path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("ontology file cannot be read", exc=exc, path=str(path))
        raise OntologyError(
            f"cannot read {path}", code=UNREADABLE, detail={"path": str(path), "reason": str(exc)}
        ) from exc
    parsed = parse_ontology(text, origin=str(path))
    ontology = parsed.ontology
    log.info(
        "ontology loaded",
        path=str(path),
        hash=ontology.hash,
        types=len(ontology.types),
        domain_types=len(ontology.domain_types()),
        link_types=len(ontology.link_types()),
        abstract=len(ontology.abstract),
        enums=len(ontology.enums),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return ontology


def parse_ontology(text: str, origin: str = "<memory>") -> ParsedOntology:
    """Descriptors for the domain ontology given as LinkML text; see `load_ontology`."""
    document = _read_yaml(text, origin)
    meta = _meta_model()
    problems: list[Problem] = []
    merged = _merge(document, meta, origin, problems)
    view, default_range = _schema_view(merged, origin)
    analysis = _Analysis(view, frozenset(meta["classes"]), default_range)
    ontology = analysis.run(source=text)
    problems.extend(analysis.problems())
    if problems:
        unique = sorted(set(problems), key=lambda p: (p.cls or "", p.slot or "", p.rule, p.message))
        log.error(
            "ontology breaks the conventions",
            origin=origin,
            count=len(unique),
            problems=[p.as_dict() for p in unique],
        )
        raise OntologyError(
            f"{len(unique)} convention problem(s) in {origin}",
            code=INVALID,
            detail={"origin": origin},
            problems=tuple(unique),
        )
    return ParsedOntology(ontology=ontology, schema=merged)


# -- reading and merging ------------------------------------------------------


def _unreadable(origin: str, reason: str, exc: BaseException | None = None) -> OntologyError:
    log.error("ontology is not valid YAML/LinkML", exc=exc, origin=origin, reason=reason)
    return OntologyError(
        f"{origin} is not a valid LinkML schema: {reason}",
        code=UNREADABLE,
        detail={"origin": origin, "reason": reason},
    )


def _read_yaml(text: str, origin: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _unreadable(origin, f"YAML syntax error: {exc}", exc) from exc
    if not isinstance(document, dict):
        raise _unreadable(origin, "the document is not a mapping")
    return document


@cache
def _meta_model_text() -> str:
    return META_MODEL_PATH.read_text(encoding="utf-8")


def _meta_model() -> dict[str, Any]:
    """A fresh copy of the meta-model with every slot's range spelled out, so its
    meaning does not depend on the domain file's `default_range`."""
    try:
        meta = yaml.safe_load(_meta_model_text())
    except (OSError, yaml.YAMLError) as exc:
        log.error("meta-model cannot be read", exc=exc, path=str(META_MODEL_PATH))
        raise OntologyError(
            "the meta-model cannot be read",
            code=UNREADABLE,
            detail={"origin": str(META_MODEL_PATH), "reason": str(exc)},
        ) from exc
    default = meta.get("default_range") or DEFAULT_RANGE
    for body in (meta.get("classes") or {}).values():
        attributes = body.get("attributes") or {}
        for name, slot in list(attributes.items()):
            slot = dict(slot or {})
            slot.setdefault("range", default)
            attributes[name] = slot
        body["attributes"] = attributes
    meta.setdefault("classes", {})
    meta.setdefault("enums", {})
    return meta


def _named_mapping(value: Any, section: str, origin: str) -> dict[str, Any]:
    """A `classes:`/`enums:` section as name -> body, accepting LinkML's list form."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(name): (body if body is not None else {}) for name, body in value.items()}
    if isinstance(value, list):
        mapping: dict[str, Any] = {}
        for item in value:
            if isinstance(item, str):
                mapping[item] = {}
            elif isinstance(item, dict) and "name" in item:
                mapping[str(item["name"])] = {k: v for k, v in item.items() if k != "name"}
            else:
                raise _unreadable(origin, f"malformed entry in {section}: {item!r}")
        return mapping
    raise _unreadable(origin, f"{section} must be a mapping")


def _merge(
    document: dict[str, Any], meta: dict[str, Any], origin: str, problems: list[Problem]
) -> dict[str, Any]:
    """The domain document with the meta-model's classes and enums added.

    Problems of the file as a whole (imports, reserved and clashing names) are
    recorded here; a definition clashing with the meta-model is left out so the
    rest of the file can still be analysed.
    """
    imports = document.get("imports") or []
    if isinstance(imports, str):
        imports = [imports]
    if not isinstance(imports, list):
        raise _unreadable(origin, "imports must be a list")
    for entry in imports:
        if entry != ALLOWED_IMPORT:
            problems.append(
                Problem(
                    "import.forbidden",
                    None,
                    None,
                    f"imports {entry!r}; a domain ontology may import only {ALLOWED_IMPORT}",
                )
            )

    classes = _named_mapping(document.get("classes"), "classes", origin)
    enums = _named_mapping(document.get("enums"), "enums", origin)
    types = _named_mapping(document.get("types"), "types", origin)
    meta_names = set(meta["classes"]) | set(meta["enums"])

    kept_classes: dict[str, Any] = {}
    for name, body in classes.items():
        if name in meta_names:
            problems.append(
                Problem("name.redefined", name, None, "redefines a meta-model class or enum")
            )
            continue
        if not CLASS_NAME.match(name):
            problems.append(
                Problem("name.class", name, None, f"class name must match {CLASS_NAME.pattern}")
            )
        if name.startswith(META_CLASS_PREFIX):
            problems.append(
                Problem(
                    "name.reserved",
                    name,
                    None,
                    f"names starting with {META_CLASS_PREFIX} are reserved for the meta-model",
                )
            )
        if name in enums or name in types:
            problems.append(
                Problem("name.duplicate", name, None, "the name is also used by an enum or type")
            )
        kept_classes[name] = body
    kept_enums: dict[str, Any] = {}
    for name, body in enums.items():
        if name in meta_names:
            problems.append(
                Problem("name.redefined", name, None, "redefines a meta-model class or enum")
            )
            continue
        if name in types:
            problems.append(
                Problem("name.duplicate", name, None, "the name is also used by a type")
            )
        kept_enums[name] = body

    merged = {
        key: value
        for key, value in document.items()
        if key not in ("imports", "classes", "enums", "prefixes", "default_range")
    }
    prefixes = document.get("prefixes") or {}
    if not isinstance(prefixes, dict):
        raise _unreadable(origin, "prefixes must be a mapping")
    merged["prefixes"] = {**prefixes, **(meta.get("prefixes") or {})}
    merged["imports"] = [ALLOWED_IMPORT]
    merged["default_range"] = document.get("default_range") or DEFAULT_RANGE
    merged["classes"] = {**meta["classes"], **kept_classes}
    merged["enums"] = {**meta["enums"], **kept_enums}
    _reject_inheritance_cycles(merged["classes"], origin)
    return merged


def _reject_inheritance_cycles(classes: dict[str, Any], origin: str) -> None:
    def parents(name: str) -> list[str]:
        body = classes.get(name)
        if not isinstance(body, dict):
            return []
        found = [body["is_a"]] if isinstance(body.get("is_a"), str) else []
        mixins = body.get("mixins") or []
        if isinstance(mixins, str):
            mixins = [mixins]
        found.extend(m for m in mixins if isinstance(m, str))
        return [p for p in found if p in classes]

    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def visit(name: str, path: list[str]) -> None:
        state[name] = 1
        for parent in parents(name):
            if state.get(parent) == 1:
                cycle = " -> ".join([*path[path.index(parent) :], parent])
                raise _unreadable(origin, f"inheritance cycle: {cycle}")
            if parent not in state:
                visit(parent, [*path, parent])
        state[name] = 2

    for name in classes:
        if name not in state:
            visit(name, [name])


def _schema_view(merged: dict[str, Any], origin: str) -> tuple[SchemaView, str]:
    """A SchemaView over the merged document with every class already induced, so
    that structural LinkML errors surface here as `ontology.unreadable`."""
    try:
        view = SchemaView(SchemaDefinition(**copy.deepcopy(merged)))
        for name in view.all_classes():
            view.class_induced_slots(name)
            view.class_ancestors(name)
        view.all_enums()
        view.all_types()
    except OntologyError:
        raise
    except Exception as exc:  # LinkML signals malformed schemas with assorted types
        raise _unreadable(origin, f"{type(exc).__name__}: {exc}", exc) from exc
    return view, str(merged["default_range"])


# -- analysis -------------------------------------------------------------------


def _annotations(element: Any) -> dict[str, Any]:
    """An element's own annotations as tag -> plain value."""
    raw = getattr(element, "annotations", None)
    if not raw:
        return {}
    pairs: Iterable[tuple[Any, Any]]
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        pairs = ((k, v) for k, v in vars(raw).items() if not k.startswith("_"))
    result: dict[str, Any] = {}
    for tag, annotation in pairs:
        value = getattr(annotation, "value", annotation)
        result[str(tag)] = str(value) if isinstance(value, str) else value
    return result


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class _Analysis:
    """Classifies every class, maps object types to descriptors and records problems."""

    def __init__(self, view: SchemaView, meta_classes: frozenset[str], default_range: str):
        self.view = view
        self.meta_classes = meta_classes
        self.default_range = default_range
        self._problems: dict[tuple, Problem] = {}
        self.classes = {str(name): c for name, c in view.all_classes().items()}
        self.enums = {str(name): e for name, e in view.all_enums().items()}
        self.custom_types = {str(name) for name in view.all_types(imports=False)}
        self.all_types = {str(name) for name in view.all_types()}
        self.induced = {name: list(view.class_induced_slots(name)) for name in self.classes}
        # Reflexive, is_a and mixins, nearest first.
        self.lineage = {name: [str(a) for a in view.class_ancestors(name)] for name in self.classes}
        self.chain = {name: self._is_a_chain(name) for name in self.classes}
        self.mixins = {name for name, c in self.classes.items() if c.mixin}
        used_as_mixin: set[str] = set()
        for c in self.classes.values():
            used_as_mixin.update(str(m) for m in c.mixins or [])
        self.used_as_mixin = used_as_mixin
        self.has_identifier = {
            name: any(s.identifier or s.key for s in slots) for name, slots in self.induced.items()
        }
        self.value_types = self._value_types()
        self.object_types = {
            name
            for name in self.classes
            if name not in self.mixins and name not in self.value_types
        }
        self.concrete = {name for name in self.object_types if not self.classes[name].abstract}
        self.abstract = {
            name: tuple(sorted(c for c in self.concrete if name in self.chain[c]))
            for name in self.object_types
            if self.classes[name].abstract
        }

    # -- bookkeeping

    def _add(self, rule: str, cls: str | None, slot: str | None, message: str) -> None:
        problem = Problem(rule, cls, slot, message)
        self._problems.setdefault((rule, cls, slot, message), problem)

    def problems(self) -> list[Problem]:
        return list(self._problems.values())

    def _is_a_chain(self, name: str) -> tuple[str, ...]:
        chain: list[str] = []
        parent = self.classes[name].is_a
        while parent:
            parent = str(parent)
            if parent == name or parent in chain or parent not in self.classes:
                break
            chain.append(parent)
            parent = self.classes[parent].is_a
        return tuple(chain)

    def _descendants(self, name: str) -> set[str]:
        return {c for c, chain in self.chain.items() if name in chain}

    def _value_types(self) -> set[str]:
        """Classes that only ever appear inlined inside another object's property.

        A class without an identifier that is the range of an inlined slot (or
        is related by is_a to one that is) and is never referenced by uid is a
        structure stored as JSON, not an object type with a table.
        """
        inlined: set[str] = set()
        referenced: set[str] = set()
        for slots in self.induced.values():
            for slot in slots:
                target = str(slot.range) if slot.range else None
                if target in self.classes:
                    (inlined if self.view.is_inlined(slot) else referenced).add(target)
        result = set()
        for name in self.classes:
            if name in self.mixins or self.has_identifier[name] or name in referenced:
                continue
            related = {name, *self.chain[name], *self._descendants(name)}
            if related & inlined:
                result.add(name)
        return result

    def _declarer(self, cls: str, slot: str) -> str:
        """The nearest class in `cls`'s lineage that declares or refines `slot`."""
        for ancestor in self.lineage[cls]:
            definition = self.classes[ancestor]
            if (
                slot in (definition.attributes or {})
                or slot in (definition.slots or [])
                or slot in (definition.slot_usage or {})
            ):
                return ancestor
        return cls

    def _ordered(self, cls: str, slots: list[SlotDefinition]) -> list[SlotDefinition]:
        """Induced slots ordered from the root ancestor down, mixins after the class
        that uses them, so sibling types share a column order."""
        sequence: list[str] = []

        def add(name: str) -> None:
            if name in sequence or name not in self.classes:
                return
            sequence.append(name)
            for mixin in self.classes[name].mixins or []:
                for ancestor in reversed((str(mixin), *self.chain.get(str(mixin), ()))):
                    add(ancestor)

        for ancestor in (*reversed(self.chain[cls]), cls):
            add(ancestor)
        order: dict[str, int] = {}
        for name in sequence:
            definition = self.classes[name]
            for slot_name in [*(definition.slots or []), *(definition.attributes or {})]:
                order.setdefault(str(slot_name), len(order))
        fallback = len(order)
        return sorted(slots, key=lambda s: order.get(str(s.name), fallback))

    # -- slots

    def _resolve(self, slot: SlotDefinition) -> tuple[Kind | None, str | None, str | None]:
        """(kind, target, reason it is unsupported) for a slot's range."""
        for expression in _RANGE_EXPRESSIONS:
            if getattr(slot, expression, None):
                return None, None, f"range expressions ({expression}) are not supported"
        if getattr(slot, "array", None):
            return None, None, "array slots are not supported"
        target = str(slot.range) if slot.range else self.default_range
        if target in self.enums:
            return Kind.enum, target, None
        if target in self.classes:
            if str(self.classes[target].class_uri or "") in _ANY_URIS:
                return None, None, f"range {target!r} (linkml:Any) is not supported"
            if self.view.is_inlined(slot):
                return Kind.json, None, None
            return Kind.ref, target, None
        if target in self.custom_types:
            return (
                None,
                None,
                f"custom type {target!r} is not supported; use a built-in type with constraints",
            )
        if target in _BUILTIN_KINDS:
            return _BUILTIN_KINDS[target], None, None
        if target in self.all_types:
            return None, None, f"range {target!r} is not supported"
        return None, None, f"range {target!r} is not defined"

    def _slot(self, cls: str, slot: SlotDefinition) -> Field | None:
        """Check one induced slot of `cls`; its descriptor when it maps cleanly."""
        name = str(slot.name)
        where = self._declarer(cls, name)
        if not FIELD_NAME.match(name):
            self._add("name.slot", where, name, f"slot name must match {FIELD_NAME.pattern}")
        for constraint in _UNSUPPORTED_SLOT_CONSTRAINTS:
            if getattr(slot, constraint, None) not in (None, [], {}):
                self._add("constraint.unsupported", where, name, f"{constraint} is not supported")
        annotations = _annotations(slot)
        for tag in annotations:
            if (tag.startswith("dta_") and tag not in _SLOT_ANNOTATIONS) or tag in (
                "link_source",
                "link_target",
            ):
                self._add(
                    "annotation.unknown", where, name, f"annotation {tag!r} is not known on a slot"
                )

        kind, target, unsupported = self._resolve(slot)
        if unsupported:
            self._add("range.unsupported", where, name, unsupported)
            return None
        assert kind is not None
        if slot.identifier or slot.key:
            return None  # checked with the class
        multivalued = bool(slot.multivalued)
        ok = True

        if kind is Kind.ref:
            assert target is not None
            if multivalued:
                self._add(
                    "reference.multivalued",
                    where,
                    name,
                    f"a multivalued reference to {target} is not allowed; model it as a link type",
                )
                ok = False
            if cls in self.value_types:
                self._add(
                    "reference.in_value",
                    where,
                    name,
                    "a value type stored as JSON may not hold a reference",
                )
                ok = False
            if target in self.mixins:
                self._add(
                    "reference.target", where, name, f"{target} is a mixin, not an object type"
                )
                ok = False
            elif not self.has_identifier[target]:
                self._add(
                    "reference.target",
                    where,
                    name,
                    f"{target} has no uid identifier and cannot be referenced",
                )
                ok = False
            elif target in self.abstract and not self.abstract[target]:
                self._add(
                    "reference.abstract_empty",
                    where,
                    name,
                    f"abstract {target} has no concrete descendant to refer to",
                )
                ok = False
        if kind is Kind.json:
            if str(slot.range) in self.mixins:
                self._add(
                    "range.unsupported",
                    where,
                    name,
                    f"mixin {slot.range} cannot be a property's range",
                )
                ok = False
            multivalued = False  # the JSON value holds the list itself

        role = Role.static
        if "dta_role" in annotations:
            try:
                role = Role(str(annotations["dta_role"]))
            except ValueError:
                self._add(
                    "role.unknown",
                    where,
                    name,
                    f"dta_role {annotations['dta_role']!r} is not one of "
                    f"{', '.join(r.value for r in Role)}",
                )
                ok = False

        long_text = False
        if "dta_long" in annotations:
            flag = _as_bool(annotations["dta_long"])
            if flag is None:
                self._add("annotation.value", where, name, "dta_long must be true or false")
                ok = False
            elif flag and kind is not Kind.string:
                self._add(
                    "annotation.value", where, name, "dta_long applies only to string properties"
                )
                ok = False
            else:
                long_text = flag

        bounds: dict[str, float | None] = {}
        for metaslot in ("minimum_value", "maximum_value"):
            raw = getattr(slot, metaslot, None)
            bounds[metaslot] = None
            if raw is None:
                continue
            number = _as_number(raw)
            if kind not in _NUMERIC:
                self._add("bound.invalid", where, name, f"{metaslot} on a {kind.value} property")
                ok = False
            elif multivalued:
                # Neither a column CHECK nor LinkML's generated model can bound list elements.
                self._add("bound.invalid", where, name, f"{metaslot} on a multivalued property")
                ok = False
            elif number is None:
                self._add("bound.invalid", where, name, f"{metaslot} {raw!r} is not a number")
                ok = False
            else:
                bounds[metaslot] = number
        low, high = bounds["minimum_value"], bounds["maximum_value"]
        if low is not None and high is not None and low > high:
            self._add("bound.invalid", where, name, f"minimum_value {low} > maximum_value {high}")
            ok = False

        pattern = str(slot.pattern) if slot.pattern else None
        if pattern is not None:
            if kind is not Kind.string:
                self._add("pattern.invalid", where, name, f"pattern on a {kind.value} property")
                ok = False
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    self._add("pattern.invalid", where, name, f"pattern does not compile: {exc}")
                    ok = False

        unit = None
        if slot.unit is not None:
            code = getattr(slot.unit, "ucum_code", None)
            if not code:
                self._add("unit.invalid", where, name, "a unit must give its ucum_code")
                ok = False
            else:
                unit = str(code)

        if not ok:
            return None
        return Field(
            name=name,
            kind=kind,
            required=bool(slot.required),
            multivalued=multivalued,
            target=target,
            role=role,
            unit=unit,
            minimum=low,
            maximum=high,
            pattern=pattern,
            long_text=long_text,
            description=str(slot.description or ""),
        )

    # -- classes

    def _check_identifier(self, cls: str) -> None:
        slots = self.induced[cls]
        identifiers = [s for s in slots if s.identifier or s.key]
        if not identifiers:
            if any(not self.has_identifier[a] for a in self.chain[cls] if a in self.object_types):
                return  # reported on the ancestor it inherits from
            has_uid = any(str(s.name) == UID_FIELD for s in slots)
            if has_uid:
                message = "uid is declared but not marked identifier: true"
            elif cls in self.used_as_mixin:
                message = "no uid identifier; a class used only as a mixin needs mixin: true"
            else:
                message = "no identifier; declare a slot uid with identifier: true"
            self._add("identifier.missing", cls, UID_FIELD if has_uid else None, message)
            return
        identifier = identifiers[0]
        if str(identifier.name) != UID_FIELD:
            self._add(
                "identifier.name",
                cls,
                str(identifier.name),
                f"the identifier slot must be named {UID_FIELD}",
            )
            return
        kind, _, _ = self._resolve(identifier)
        if kind is not Kind.string or identifier.multivalued:
            self._add("identifier.range", cls, UID_FIELD, "uid must be a single-valued string")

    def _check_parents(self, cls: str) -> None:
        domain = cls not in self.meta_classes
        for ancestor in self.chain[cls]:
            if ancestor in self.mixins:
                self._add("parent.invalid", cls, None, f"is_a names mixin {ancestor}; use mixins:")
            elif ancestor in self.value_types:
                self._add("parent.invalid", cls, None, f"is_a names value type {ancestor}")
            elif domain and ancestor in self.meta_classes:
                self._add(
                    "parent.invalid", cls, None, f"may not specialise meta-model class {ancestor}"
                )
        for mixin in self.classes[cls].mixins or []:
            if domain and str(mixin) in self.meta_classes:
                self._add("parent.invalid", cls, None, f"may not mix in meta-model class {mixin}")

    def _check_class_metaslots(self, cls: str) -> None:
        definition = self.classes[cls]
        for constraint in _UNSUPPORTED_CLASS_CONSTRAINTS:
            if getattr(definition, constraint, None) not in (None, [], {}):
                self._add("constraint.unsupported", cls, None, f"{constraint} is not supported")
        for tag in _annotations(definition):
            known = tag in _CLASS_ANNOTATIONS or tag.startswith(CHECK_PREFIX)
            if not known and (tag.startswith("dta_") or tag.startswith("link_")):
                self._add(
                    "annotation.unknown", cls, None, f"annotation {tag!r} is not known on a class"
                )

    def _effective_annotations(self, cls: str) -> dict[str, Any]:
        """Own annotations plus those inherited through is_a and mixins, nearest first."""
        merged: dict[str, Any] = {}
        for ancestor in self.lineage[cls]:
            for tag, value in _annotations(self.classes[ancestor]).items():
                merged.setdefault(tag, value)
        return merged

    def _link(
        self, cls: str, annotations: dict[str, Any], fields: dict[str, Field]
    ) -> tuple[str, str] | None:
        source, target = annotations.get("link_source"), annotations.get("link_target")
        if source is None and target is None:
            return None
        if source is None or target is None:
            present = "link_source" if source is not None else "link_target"
            self._add(
                "link.incomplete",
                cls,
                None,
                f"{present} is given without its counterpart; a link type needs both",
            )
            return None
        source, target = str(source), str(target)
        induced = {str(s.name): s for s in self.induced[cls]}
        ok = True
        for annotation, slot_name in (("link_source", source), ("link_target", target)):
            slot = induced.get(slot_name)
            if slot is None:
                self._add(
                    "link.missing", cls, slot_name, f"{annotation} names a slot the class lacks"
                )
                ok = False
                continue
            kind, _, _ = self._resolve(slot)
            if kind is not Kind.ref:
                self._add("link.not_reference", cls, slot_name, f"{annotation} must be a reference")
                ok = False
            elif not slot.required:
                self._add("link.not_required", cls, slot_name, f"{annotation} must be required")
                ok = False
            elif slot_name not in fields:
                ok = False  # the slot's own problem is already recorded
        if source == target:
            self._add(
                "link.same_slot", cls, source, "link_source and link_target name the same slot"
            )
            ok = False
        return (source, target) if ok else None

    def _checks(
        self, cls: str, annotations: dict[str, Any], columns: set[str]
    ) -> tuple[tuple[str, str], ...]:
        checks = []
        for tag in sorted(annotations):
            if not tag.startswith(CHECK_PREFIX):
                continue
            name = tag[len(CHECK_PREFIX) :]
            expression = annotations[tag]
            if not FIELD_NAME.match(name):
                self._add(
                    "check.name", cls, None, f"check name {name!r} must match {FIELD_NAME.pattern}"
                )
            for issue in screen_expression(expression, columns):
                self._add(issue.rule, cls, None, f"check {name!r}: {issue.message}")
            checks.append((name, str(expression)))
        return tuple(checks)

    def _event(self, cls: str, annotations: dict[str, Any]) -> bool:
        if "dta_event" not in annotations:
            return False
        flag = _as_bool(annotations["dta_event"])
        if flag is None:
            self._add("annotation.value", cls, None, "dta_event must be true or false")
            return False
        return flag

    def run(self, source: str) -> Ontology:
        types: list[ObjectType] = []
        for cls in sorted(self.classes):
            self._check_class_metaslots(cls)
            fields: list[Field] = []
            for slot in self._ordered(cls, self.induced[cls]):
                built = self._slot(cls, slot)
                if built is not None:
                    fields.append(built)
            if cls not in self.object_types:
                continue
            self._check_identifier(cls)
            self._check_parents(cls)
            if cls not in self.concrete:
                continue
            annotations = self._effective_annotations(cls)
            by_name = {f.name: f for f in fields}
            columns = {UID_FIELD, *(str(s.name) for s in self.induced[cls])}
            definition = self.classes[cls]
            types.append(
                ObjectType(
                    name=cls,
                    fields=tuple(fields),
                    ancestors=self.chain[cls],
                    link=self._link(cls, annotations, by_name),
                    meta=cls in self.meta_classes,
                    event=self._event(cls, annotations),
                    checks=self._checks(cls, annotations, columns),
                    description=str(definition.description or ""),
                )
            )
        for name, enum in self.enums.items():
            if not enum.permissible_values:
                self._add("enum.empty", name, None, "an enum needs at least one permissible value")
        types.sort(key=lambda t: (not t.meta, t.name))
        enums = {
            name: tuple(str(value) for value in (enum.permissible_values or {}))
            for name, enum in sorted(self.enums.items())
        }
        return Ontology(
            types=tuple(types),
            abstract=dict(sorted(self.abstract.items())),
            enums=enums,
            source=source,
        )
