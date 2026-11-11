"""Reading the objects of a type, abstract classes included, a page at a time.

Pages follow uid order and are read with a keyset filter (`uid > last`), so
each page is one bounded read per concrete type. Sorting by another property
sorts the whole filtered result in memory, which is allowed up to
`SORT_MAX_ROWS` rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.schema import UID_FIELD, Field, ObjectType, Ontology
from dta_core.store import Store, StoredObject, UnknownProperty, UnknownType, VersionRef

from .problems import ApiError
from .tables import TYPE_COLUMN, readable

SORT_MAX_ROWS = 10_000


@dataclass(frozen=True)
class Shape:
    """What a listing of a type reads: its concrete types and the properties they share."""

    name: str
    members: tuple[str, ...]
    fields: dict[str, Field]
    abstract: bool

    @property
    def template(self) -> ObjectType:
        return ObjectType(self.name, tuple(self.fields.values()))


@dataclass(frozen=True)
class Listing:
    table: pa.Table
    # Where the next page starts: the last uid, or an offset into a sorted result.
    next_position: Any | None


def shape_of(ontology: Ontology, name: str) -> Shape:
    if name in ontology.abstract:
        members = tuple(member for member in ontology.abstract[name] if ontology.has_type(member))
        if not members:
            raise UnknownType(
                f"abstract class {name!r} has no concrete type", detail={"type": name}
            )
        first = ontology.type(members[0])
        shared = [
            field
            for field in first.fields
            if all(ontology.type(member).has_field(field.name) for member in members[1:])
        ]
        return Shape(name, members, {field.name: field for field in shared}, abstract=True)
    if not ontology.has_type(name):
        raise UnknownType(f"unknown type {name!r}", detail={"type": name})
    object_type = ontology.type(name)
    fields = {field.name: field for field in object_type.fields}
    return Shape(name, (name,), fields, abstract=False)


def _columns(shape: Shape, columns: Sequence[str] | None) -> list[str]:
    if columns is None:
        return list(shape.fields)
    for name in columns:
        if name != UID_FIELD and name not in shape.fields:
            raise UnknownProperty(
                f"{shape.name} has no property {name!r}"
                + (" shared by all its concrete types" if shape.abstract else ""),
                detail={"type": shape.name, "property": name},
            )
    return [name for name in dict.fromkeys(columns) if name != UID_FIELD]


def _sort_key(shape: Shape, sort: str) -> tuple[str, bool]:
    descending = sort.startswith("-")
    name = sort.lstrip("-+").strip()
    if name != UID_FIELD:
        if name not in shape.fields:
            raise UnknownProperty(
                f"{shape.name} has no property {name!r} to sort by",
                detail={"type": shape.name, "property": name},
            )
        if shape.fields[name].stored_as_json:
            raise ApiError(
                f"{name} holds lists or structured values and cannot be sorted by",
                code="api.bad_request",
            )
    return name, descending


def read_objects(
    store: Store,
    ontology: Ontology,
    version: VersionRef,
    shape: Shape,
    *,
    columns: Sequence[str] | None,
    filters: Sequence[tuple[str, str, Any]],
    prov: bool,
    limit: int | None,
    position: Any | None,
    sort: str | None,
) -> Listing:
    names = _columns(shape, columns)
    key, descending = _sort_key(shape, sort) if sort else (UID_FIELD, False)
    by_uid = key == UID_FIELD and not descending
    read_names = names if key in (UID_FIELD, *names) else [*names, key]
    conditions = list(filters)
    if by_uid:
        if position is not None:
            if not isinstance(position, str):
                raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
            conditions.append((UID_FIELD, ">", position))
        fetch = None if limit is None else limit + 1
    else:
        fetch = SORT_MAX_ROWS + 1
    parts = []
    for member in shape.members:
        part = store.table(
            member,
            columns=read_names,
            filter=conditions,
            limit=fetch,
            at=version,
            prov=prov,
            ontology=ontology,
        )
        if shape.abstract:
            part = part.add_column(
                1,
                pa.field(TYPE_COLUMN, pa.string()),
                pa.array([member] * part.num_rows, pa.string()),
            )
        parts.append(part)
    table = parts[0] if len(parts) == 1 else pa.concat_tables(parts, promote_options="permissive")
    if by_uid:
        # The store orders rows only when it is given a limit.
        if len(parts) > 1 or fetch is None:
            table = table.take(pc.sort_indices(table, sort_keys=[(UID_FIELD, "ascending")]))
        next_position = None
        if limit is not None and table.num_rows > limit:
            table = table.slice(0, limit)
            next_position = table.column(UID_FIELD)[-1].as_py()
        return Listing(readable(table, shape.template), next_position)
    if table.num_rows > SORT_MAX_ROWS:
        raise ApiError(
            f"sorting by {key} is possible for at most {SORT_MAX_ROWS} rows; narrow the "
            "listing with where, or page in uid order",
            code="api.sort_too_large",
        )
    order = pc.sort_indices(
        table,
        sort_keys=[
            (key, "descending" if descending else "ascending", "at_end"),
            (UID_FIELD, "ascending", "at_end"),
        ],
    )
    table = table.take(order)
    if key not in (UID_FIELD, *names):
        table = table.drop_columns([key])
    offset = position or 0
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
    next_position = None
    if limit is not None:
        if offset + limit < table.num_rows:
            next_position = offset + limit
        table = table.slice(offset, limit)
    elif offset:
        table = table.slice(offset)
    return Listing(readable(table, shape.template), next_position)


def is_a(ontology: Ontology, concrete: str, name: str) -> bool:
    return concrete == name or (
        ontology.has_type(concrete) and name in ontology.type(concrete).ancestors
    )


def known_type(ontology: Ontology, name: str) -> None:
    if not ontology.has_type(name) and name not in ontology.abstract:
        raise UnknownType(f"unknown type {name!r}", detail={"type": name})


def get_object(
    store: Store, ontology: Ontology, version: VersionRef, uid: str, type_name: str | None
) -> StoredObject:
    """The object with this uid at the version, which must be of `type_name` when given."""
    if type_name is not None:
        known_type(ontology, type_name)
    found = store.get(uid, at=version, ontology=ontology)
    if found is None or (type_name is not None and not is_a(ontology, found.type, type_name)):
        what = f"no {type_name} " if type_name else "no object "
        raise ApiError(
            f"{what}with uid {uid!r} exists at version #{version.seq} of {version.branch}",
            code="api.unknown_object",
        )
    return found
