"""A property written `Type.property`, as a scenario's models and interventions name it.

`Type` is a concrete type or an abstract class; an abstract class stands for its
concrete types, which then all have to hold the property as one thing (one kind,
one unit), the way the built-in functions read it.
"""

from __future__ import annotations

from dataclasses import dataclass

from dta_core.schema import UID_FIELD, Field, Kind, Ontology
from dta_core.store import StoreError

from ..functions.reads import members_of

_NUMERIC = (Kind.integer, Kind.float)


@dataclass(frozen=True)
class Target:
    text: str
    type: str
    property: str
    # The concrete types whose objects hold it.
    members: tuple[str, ...]
    field: Field

    @property
    def numeric(self) -> bool:
        return self.field.kind in _NUMERIC and not self.field.multivalued

    def cells(self) -> set[tuple[str, str]]:
        """(concrete type, property) of every column it names."""
        return {(member, self.property) for member in self.members}


def resolve(ontology: Ontology, text: object) -> Target:
    """The target a text names. Raises `ValueError` saying what is wrong with it."""
    if not isinstance(text, str):
        raise ValueError(f"{text!r} is not written Type.property")
    type_name, dot, name = text.partition(".")
    if not dot or not type_name or not name or "." in name:
        raise ValueError(f"{text!r} is not written Type.property")
    try:
        members = members_of(ontology, type_name)
    except StoreError as exc:
        raise ValueError(exc.message) from exc
    found: Field | None = None
    for member in members:
        object_type = ontology.type(member)
        if name == UID_FIELD or not object_type.has_field(name):
            stands = "" if member == type_name else f" ({type_name} stands for it)"
            raise ValueError(f"{member} has no property {name!r}{stands}")
        field = object_type.field(name)
        if found is None:
            found = field
        elif (field.kind, field.multivalued, field.unit) != (
            found.kind,
            found.multivalued,
            found.unit,
        ):
            raise ValueError(
                f"{text} is not one property: {member} holds it as {field.kind.value} in "
                f"{field.unit or 'no unit'}, {members[0]} as {found.kind.value} in "
                f"{found.unit or 'no unit'}"
            )
    assert found is not None
    return Target(text=text, type=type_name, property=name, members=members, field=found)
