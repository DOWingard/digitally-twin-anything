"""What a built-in reads: a property of a type, the objects it is asked about, and
the versions it names, each read made at a pinned version and none started after
the deadline.

A property is written `Type.property`. `Type` may be an abstract class, which
stands for its concrete types: the property then has to be one they all hold,
of one kind and unit, and the rows read come from each of them.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.schema import UID_FIELD, Field, Kind, Ontology
from dta_core.store import InvalidInput, Selector, UnknownProperty, UnknownType, VersionRef

from .. import inforce
from ..filters import parse_where
from .context import Context
from .errors import BAD_PARAM, BAD_UNIT, TIMEOUT, FunctionError, refused
from .params import selector_of

log = Log("functions.reads")

_NUMERIC = (Kind.integer, Kind.float)


@dataclass(frozen=True)
class Target:
    """A property of a type, or of every concrete type under an abstract class."""

    type: str
    property: str
    # The concrete types whose objects hold it.
    members: tuple[str, ...]
    field: Field

    @property
    def unit(self) -> str | None:
        return self.field.unit

    @property
    def numeric(self) -> bool:
        return self.field.kind in _NUMERIC and not self.field.multivalued

    @property
    def name(self) -> str:
        return f"{self.type}.{self.property}"


def members_of(ontology: Ontology, type_name: str) -> tuple[str, ...]:
    """The concrete types a type name stands for."""
    if type_name in ontology.abstract:
        members = tuple(name for name in ontology.abstract[type_name] if ontology.has_type(name))
        if not members:
            raise UnknownType(
                f"abstract class {type_name!r} has no concrete type", detail={"type": type_name}
            )
        return members
    if not ontology.has_type(type_name):
        raise UnknownType(f"unknown type {type_name!r}", detail={"type": type_name})
    return (type_name,)


class Reads:
    """The reads of one run of a built-in."""

    def __init__(self, function_id: str, entry: str, context: Context) -> None:
        self.function = function_id
        self.entry = entry
        self.context = context
        self.count = 0

    # -- refusals -------------------------------------------------------------

    def bad(self, parameter: str, message: str, **detail: Any) -> FunctionError:
        return refused(
            BAD_PARAM,
            f"{self.function}: {message}",
            function=self.function,
            parameter=parameter,
            **detail,
        )

    def bad_unit(self, parameter: str, message: str, **detail: Any) -> FunctionError:
        return refused(
            BAD_UNIT,
            f"{self.function}: {message}",
            function=self.function,
            parameter=parameter,
            **detail,
        )

    def check(self) -> None:
        """Called before every read: none is started after the deadline."""
        self.guard()
        self.count += 1

    def guard(self) -> None:
        """Give up when the deadline has passed."""
        if time.monotonic() >= self.context.deadline:
            raise refused(
                TIMEOUT,
                f"{self.function}: the run was given up at its deadline, after {self.count} reads",
                function=self.function,
                entry=self.entry,
                reads=self.count,
            )

    # -- what the ontology decides ----------------------------------------------

    def field(self, parameter: str, type_name: str, members: Sequence[str], name: str) -> Field:
        """The property `name` as every member holds it."""
        ontology = self.context.ontology
        found: Field | None = None
        for member in members:
            object_type = ontology.type(member)
            if name == UID_FIELD or not object_type.has_field(name):
                raise UnknownProperty(
                    f"{member} has no property {name!r}"
                    + ("" if member == type_name else f" ({type_name} stands for it)"),
                    detail={"type": member, "property": name},
                )
            field = object_type.field(name)
            if found is None:
                found = field
            elif (field.kind, field.multivalued, field.unit) != (
                found.kind,
                found.multivalued,
                found.unit,
            ):
                raise self.bad(
                    parameter,
                    f"{type_name}.{name} is not one property: {member} holds it as "
                    f"{field.kind.value} in {field.unit or 'no unit'}, {members[0]} as "
                    f"{found.kind.value} in {found.unit or 'no unit'}",
                    type=type_name,
                    property=name,
                )
        assert found is not None
        return found

    def target(self, parameter: str, text: str, *, numeric: bool = False) -> Target:
        type_name, _, name = text.partition(".")
        members = members_of(self.context.ontology, type_name)
        field = self.field(parameter, type_name, members, name)
        found = Target(type=type_name, property=name, members=members, field=field)
        if numeric and not found.numeric:
            raise self.bad(
                parameter,
                f"{text} holds {'lists of ' if field.multivalued else ''}{field.kind.value} "
                f"values; {self.entry} needs numbers",
                type=type_name,
                property=name,
            )
        return found

    def conditions(self, member: str, clauses: Sequence[str] | None) -> list[tuple[str, str, Any]]:
        """`where` clauses as the store's filter over one concrete type."""
        if not clauses:
            return []
        fields = {field.name: field for field in self.context.ontology.type(member).fields}
        try:
            return parse_where(clauses, fields, member)
        except InvalidInput as exc:
            raise self.bad("where", exc.message) from exc

    # -- versions ---------------------------------------------------------------

    def version(self, parameter: str, selector: Selector | str) -> VersionRef:
        """The version a parameter names, as seen from the pinned one.

        `head` is the pinned version, and so is a time at or after it: a commit made
        since the call was pinned is not seen. A position or an id after it is refused.
        """
        pinned = self.context.version
        parsed = selector_of(self.function, parameter, selector)
        if parsed.kind == "head":
            return pinned
        self.check()
        found = self.context.store.resolve(parsed, pinned.branch)[0]
        if found.seq <= pinned.seq:
            return found
        if parsed.kind == "time":
            return pinned
        raise self.bad(
            parameter,
            f"{parameter} names version #{found.seq}, after #{pinned.seq} which the call reads at",
            version=found.seq,
            pinned=pinned.seq,
        )

    def span(self, first: Selector | str, last: Selector | str) -> tuple[VersionRef, VersionRef]:
        """The two ends of a range of versions, in order."""
        start, end = self.version("from", first), self.version("to", last)
        if start.seq > end.seq:
            raise self.bad(
                "from",
                f"from (#{start.seq}) is after to (#{end.seq})",
                **{"from": start.seq, "to": end.seq},
            )
        return start, end

    # -- rows -------------------------------------------------------------------

    def objects(
        self,
        target: Target,
        columns: Sequence[str],
        uids: Sequence[str] | None = None,
        where: Sequence[str] | None = None,
        at: VersionRef | None = None,
    ) -> pa.Table:
        """`uid` and `columns` of the objects of the target's type that are among `uids`
        and pass `where`, in uid order. One read per concrete type."""
        context = self.context
        parts = []
        for member in target.members:
            conditions = self.conditions(member, where)
            if uids:
                conditions = [(UID_FIELD, "in", list(uids)), *conditions]
            self.check()
            parts.append(
                context.store.table(
                    member,
                    columns=list(columns),
                    filter=conditions or None,
                    at=at or context.version,
                    ontology=context.ontology,
                )
            )
        table = parts[0] if len(parts) == 1 else pa.concat_tables(parts)
        order = pc.sort_indices(table, sort_keys=[(UID_FIELD, "ascending")])
        return table.take(order).combine_chunks()

    def events(
        self,
        target: Target,
        first: VersionRef,
        last: VersionRef,
        uids: Sequence[str] | None = None,
        where: Sequence[str] | None = None,
    ) -> pa.Table:
        """The state of the target's property at `first` and every change after it up to
        `last`, one row per event, for the objects among `uids` that pass `where` at `last`.

        Two reads per concrete type, whatever the range; `where` costs one more.
        """
        context = self.context
        passing = None
        if where:
            passing = self.objects(target, [], uids, where, at=last).column(UID_FIELD)
        parts = []
        for member in target.members:
            self.check()
            parts.append(
                inforce.events(
                    context.store,
                    context.ontology,
                    member,
                    first,
                    last,
                    properties=[target.property],
                    uids=list(uids) if uids else None,
                )
            )
        table = parts[0] if len(parts) == 1 else pa.concat_tables(parts)
        if passing is not None:
            # Kept in memory rather than sent as a filter: the objects that pass may be
            # most of the type, and the events are already read.
            table = table.filter(
                pc.is_in(table.column(inforce.UID), value_set=passing.combine_chunks())
            )
        return table.combine_chunks()
