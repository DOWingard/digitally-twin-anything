"""Engine-neutral description of an ontology: its types, properties and constraints.

This is the form every other part works from. It is produced from a LinkML
file, stored with the data so that each version can be read under the ontology
that was in force when it was written, and turned into tables by the store.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

UID_FIELD = "uid"
UID_MAX_LENGTH = 190
META_CLASS_PREFIX = "Twin"
META_TABLE_PREFIX = "dta_"

CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_case(name: str) -> str:
    """`PumpCurvePoint` -> `pump_curve_point`."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class Kind(StrEnum):
    string = "string"
    integer = "integer"
    float = "float"
    boolean = "boolean"
    datetime = "datetime"
    date = "date"
    enum = "enum"
    ref = "ref"  # reference to another object, stored as its uid
    json = "json"  # structured value: an inlined object or a list of them


class FactKind(StrEnum):
    """How a stored value was obtained."""

    observed = "observed"  # reported by a source
    inferred = "inferred"  # estimated by the twin from other facts
    assumed = "assumed"  # a default or a stated assumption
    simulated = "simulated"  # produced by running a model forward


class Role(StrEnum):
    static = "static"  # fixed attribute of the object
    state = "state"  # changes with time
    derived = "derived"  # computed by the twin


@dataclass(frozen=True)
class Field:
    name: str
    kind: Kind
    required: bool = False
    # A list of scalars of `kind`, stored as a JSON array. Never set for references.
    multivalued: bool = False
    # Referenced class for `ref` (may be abstract); enum name for `enum`.
    target: str | None = None
    role: Role = Role.static
    unit: str | None = None  # UCUM code
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = None
    long_text: bool = False
    description: str = ""

    @property
    def stored_as_json(self) -> bool:
        return self.multivalued or self.kind == Kind.json


@dataclass(frozen=True)
class ObjectType:
    """A concrete object or link type; every instance is one row of its table."""

    name: str
    # Own and inherited properties, without the uid.
    fields: tuple[Field, ...]
    # Nearest ancestor first; abstract and concrete ancestors alike.
    ancestors: tuple[str, ...] = ()
    # (source field, target field) when the type is a link between two objects.
    link: tuple[str, str] | None = None
    meta: bool = False  # belongs to the meta-model shared by every twin
    event: bool = False  # append-only record of something that happened
    # (name, SQL boolean expression over this type's own columns)
    checks: tuple[tuple[str, str], ...] = ()
    description: str = ""

    @property
    def table(self) -> str:
        if self.meta:
            return META_TABLE_PREFIX + snake_case(self.name.removeprefix(META_CLASS_PREFIX))
        return snake_case(self.name)

    @property
    def scope(self) -> str:
        """Namespace of this type's uids: its top-most ancestor, so that a
        reference to an abstract type can be formed without knowing the concrete type."""
        root = self.ancestors[-1] if self.ancestors else self.name
        return snake_case(root)

    def field(self, name: str) -> Field:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.name} has no property {name}")

    def has_field(self, name: str) -> bool:
        return any(candidate.name == name for candidate in self.fields)

    def references(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.kind == Kind.ref)


def make_uid(object_type: ObjectType, key: str) -> str:
    """The uid of the object of this type with the given source key."""
    uid = f"{object_type.scope}:{key}"
    if len(uid) > UID_MAX_LENGTH:
        raise ValueError(f"uid longer than {UID_MAX_LENGTH} characters: {uid[:40]}...")
    return uid


@dataclass(frozen=True)
class Ontology:
    # Concrete types in a stable order: the meta-model's first, then by name.
    types: tuple[ObjectType, ...]
    # Abstract class name -> names of its concrete descendants.
    abstract: dict[str, tuple[str, ...]] = field(default_factory=dict)
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The LinkML text of the domain ontology this was loaded from.
    source: str = ""

    def type(self, name: str) -> ObjectType:
        for candidate in self.types:
            if candidate.name == name:
                return candidate
        raise KeyError(f"unknown type {name}")

    def has_type(self, name: str) -> bool:
        return any(candidate.name == name for candidate in self.types)

    def concrete(self, name: str) -> tuple[str, ...]:
        """The concrete types a reference to `name` may point at."""
        if name in self.abstract:
            return self.abstract[name]
        if self.has_type(name):
            descendants = tuple(t.name for t in self.types if name in t.ancestors)
            return (name, *descendants)
        raise KeyError(f"unknown type {name}")

    def domain_types(self) -> tuple[ObjectType, ...]:
        return tuple(t for t in self.types if not t.meta)

    def link_types(self) -> tuple[ObjectType, ...]:
        return tuple(t for t in self.types if t.link is not None)

    # -- serialisation ------------------------------------------------------

    def to_json(self) -> str:
        """Canonical form stored with the data; `from_json` restores an equal object."""
        document = {
            "format": 1,
            "types": [asdict(t) for t in self.types],
            "abstract": {name: list(members) for name, members in sorted(self.abstract.items())},
            "enums": {name: list(values) for name, values in sorted(self.enums.items())},
            "source": self.source,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Ontology:
        document: dict[str, Any] = json.loads(text)
        if document.get("format") != 1:
            raise ValueError(f"unsupported ontology format: {document.get('format')!r}")
        types = []
        for raw in document["types"]:
            fields = tuple(
                Field(**{**f, "kind": Kind(f["kind"]), "role": Role(f["role"])})
                for f in raw["fields"]
            )
            types.append(
                ObjectType(
                    name=raw["name"],
                    fields=fields,
                    ancestors=tuple(raw["ancestors"]),
                    link=tuple(raw["link"]) if raw["link"] else None,
                    meta=raw["meta"],
                    event=raw["event"],
                    checks=tuple((name, expression) for name, expression in raw["checks"]),
                    description=raw["description"],
                )
            )
        return cls(
            types=tuple(types),
            abstract={name: tuple(members) for name, members in document["abstract"].items()},
            enums={name: tuple(values) for name, values in document["enums"].items()},
            source=document["source"],
        )

    @property
    def hash(self) -> str:
        """Identifies the ontology exactly, properties and constraints included."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Violation:
    """One way in which a row breaks the ontology's constraints."""

    type: str
    uid: str | None
    property: str | None
    rule: str  # stable code, such as "type", "required", "enum", "range", "reference"
    message: str


class RowValidator(Protocol):
    """Checks rows of one type against the ontology before they are written.

    With `partial` true a row holds only the properties being changed, so
    missing required properties are not violations.
    """

    def validate(
        self, type_name: str, rows: list[dict[str, Any]], partial: bool = False
    ) -> list[Violation]: ...
