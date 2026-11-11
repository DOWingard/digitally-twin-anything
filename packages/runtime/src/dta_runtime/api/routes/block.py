"""Whole tables for analysis and learning: node tables, edge tables and slices over versions."""

from __future__ import annotations

from typing import Annotated, Literal

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.schema import UID_FIELD
from dta_core.store import LIVE_BRANCH, UnknownProperty, UnknownType
from fastapi import APIRouter, Path, Query, Request
from starlette.responses import Response

from ...graph import EDGE_SCHEMA, select_kinds
from ..deps import ServicesDep
from ..docs import API_PREFIX, tabular
from ..formats import negotiate, table_response
from ..models import Page, TensorPage
from ..paging import next_cursor, paged_response, start, start_range
from ..params import (
    At,
    Branch,
    Columns,
    Cursor,
    FormatParam,
    Limit,
    TypeName,
    page_limit,
    split_list,
)
from ..problems import ApiError
from ..slices import changes_slice, numeric_properties, state_slice, tensor, tensor_indexes
from ..tables import fingerprint, readable
from .objects import list_type

router = APIRouter(prefix=f"{API_PREFIX}/block", tags=["block"])

EdgeTypePath = Annotated[
    str,
    Path(description="A link type, or `Type.property` for a reference", max_length=200),
]
Range = Annotated[
    str,
    Query(
        description="Versions of the slice: `<a>..<b>` with any selectors, optionally `/<step>`",
        examples=["#0..head"],
        max_length=400,
    ),
]
Uids = Annotated[
    list[str] | None,
    Query(description="Objects to include, repeated or comma-separated; all if absent"),
]
Mode = Annotated[
    Literal["state", "changes"],
    Query(
        description="`state`: each object as of each version of the range; `changes`: each "
        "change as recorded"
    ),
]
Layout = Annotated[
    Literal["long", "tensor"],
    Query(
        description="`long`: one row per object and version (or change); `tensor`: coordinate "
        "rows (uid_index, property_index, version_index, value) of the numeric properties"
    ),
]


@router.get(
    "/nodes/{type}",
    summary="The node table of a type",
    description="Every object of the type at a version: `uid`, then the properties; an "
    "abstract class gives its concrete descendants with a `_type` column. Meant for Arrow or "
    "Parquet; JSON is paged in uid order.",
    response_model=Page,
    responses=tabular(400, 404),
)
def nodes(
    request: Request,
    services: ServicesDep,
    type: TypeName,
    columns: Columns = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    return list_type(
        request,
        services,
        type,
        at=at,
        branch=branch,
        columns=columns,
        where=None,
        prov=False,
        sort=None,
        limit=limit,
        cursor=cursor,
        fmt=fmt,
        kind="nodes",
    )


@router.get(
    "/edges/{edge_type}",
    summary="The edge table of one edge type",
    description="`src`, `dst`, `edge_type`, `link_uid` (null for a reference), then for a link "
    "type the link's properties: all but its two ends unless `columns` names some. In link uid "
    "order for a link type, in uid order of the referring object for a reference.",
    response_model=Page,
    responses=tabular(400, 404),
)
def edges(
    request: Request,
    services: ServicesDep,
    edge_type: EdgeTypePath,
    columns: Columns = None,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    wanted = split_list(columns)
    query = fingerprint("block_edges", edge_type, wanted)
    version, position = start(services, cursor, "block_edges", query, at, branch)
    ontology = services.ontology(version)
    kind = select_kinds(ontology, [edge_type])[0]
    holder = ontology.type(kind.holder)
    if kind.is_link:
        assert kind.source is not None
        ends = (kind.source, kind.target)
        for name in wanted or ():
            if name == UID_FIELD or not holder.has_field(name):
                raise UnknownProperty(
                    f"{holder.name} has no property {name!r}",
                    detail={"type": holder.name, "property": name},
                )
        properties = wanted or [field.name for field in holder.fields if field.name not in ends]
        read = [*ends, *(name for name in properties if name not in ends)]
        conditions: list[tuple[str, str, object]] = [
            (kind.source, "not_null", None),
            (kind.target, "not_null", None),
        ]
    else:
        if wanted:
            raise ApiError(
                f"{edge_type} is a reference and has no properties of its own",
                code="api.bad_request",
            )
        properties, read = [], [kind.target]
        conditions = [(kind.target, "not_null", None)]
    if position is not None:
        if not isinstance(position, str):
            raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
        conditions.append((UID_FIELD, ">", position))
    table = services.store.table(
        kind.holder,
        columns=read,
        filter=conditions,
        limit=None if per_page is None else per_page + 1,
        at=version,
        ontology=ontology,
    )
    following = None
    if per_page is None:
        # The store orders rows only when it is given a limit.
        table = table.take(pc.sort_indices(table, sort_keys=[(UID_FIELD, "ascending")]))
    elif table.num_rows > per_page:
        table = table.slice(0, per_page)
        following = table.column(UID_FIELD)[-1].as_py()
    count = table.num_rows
    if kind.is_link:
        assert kind.source is not None
        src, dst = table.column(kind.source), table.column(kind.target)
        link_uid = table.column(UID_FIELD)
    else:
        src, dst = table.column(UID_FIELD), table.column(kind.target)
        link_uid = pa.chunked_array([pa.nulls(count, pa.string())])
    result = pa.Table.from_arrays(
        [src, dst, pa.chunked_array([pa.array([edge_type] * count, pa.string())]), link_uid],
        schema=EDGE_SCHEMA,
    )
    for name in properties:
        result = result.append_column(table.schema.field(name), table.column(name))
    return table_response(
        readable(result, holder),
        chosen,
        version,
        next_cursor("block_edges", query, version, following),
    )


@router.get(
    "/slice/{type}",
    summary="A type's objects over a range of versions",
    description="`mode=state`, `layout=long`: one row per object and version of the range at "
    "which the object exists (`_version`, `_seq`, `_t`, `uid`, properties as of that version). "
    "`mode=changes`: one row per recorded change (`_change` added, modified or removed). "
    "`layout=tensor`: coordinate rows (uid_index, property_index, version_index, value) of the "
    "numeric properties, with the uid, property and version index tables in the Arrow schema "
    "metadata (`dta:uids`, `dta:properties`, `dta:versions`) and as fields of the JSON document.",
    response_model=Page | TensorPage,
    responses=tabular(400, 404),
)
def block_slice(
    request: Request,
    services: ServicesDep,
    type: TypeName,
    range: Range = "#0..head",
    branch: Branch = LIVE_BRANCH,
    properties: Columns = None,
    uids: Uids = None,
    mode: Mode = "state",
    layout: Layout = "long",
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    wanted = split_list(properties)
    objects = split_list(uids)
    query = fingerprint("slice", type, wanted, objects, mode, layout)
    versions, pinned, position = start_range(services, cursor, "slice", query, range, branch)
    last = versions[-1]
    ontology = services.ontology(last)
    if not ontology.has_type(type):
        if type in ontology.abstract:
            raise ApiError(
                f"{type} is abstract; a slice is of one concrete type", code="api.bad_request"
            )
        raise UnknownType(f"unknown type {type!r}", detail={"type": type})
    object_type = ontology.type(type)
    for name in wanted or ():
        if not object_type.has_field(name):
            raise UnknownProperty(
                f"{type} has no property {name!r}", detail={"type": type, "property": name}
            )
    chosen_properties = wanted
    if layout == "tensor":
        numeric = numeric_properties(object_type)
        other = [name for name in wanted or () if name not in numeric]
        if other:
            raise ApiError(
                f"the tensor layout holds numbers only; {other[0]} is not a number",
                code="api.bad_request",
            )
        chosen_properties = wanted or numeric
        if not chosen_properties:
            raise ApiError(f"{type} has no numeric property", code="api.bad_request")
    if mode == "state":
        long = state_slice(services.store, ontology, versions, type, chosen_properties, objects)
    else:
        long = changes_slice(
            services.store, ontology, pinned, last.branch, type, chosen_properties, objects
        )
    if layout == "tensor":
        assert chosen_properties is not None
        table = tensor(long, versions, chosen_properties)
        extra = tensor_indexes(table)
    else:
        table, extra = readable(long, object_type), None
    return paged_response(
        table, chosen, last, per_page, position, "slice", query, extra=extra, more={"r": pinned}
    )
