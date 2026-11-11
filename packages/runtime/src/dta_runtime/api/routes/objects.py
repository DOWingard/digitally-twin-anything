"""Objects of the twin as of a version: listings, single objects, links and histories."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import LIVE_BRANCH, StoredObject, UnknownProperty, UnknownType, VersionRef
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ...filters import edge_where_identity, parse_edge_where, parse_where
from ...graph import links as object_links
from ..deps import ServicesDep
from ..docs import API_PREFIX, TOTAL_HEADER, tabular, versioned
from ..formats import Format, json_response, negotiate, table_response
from ..models import CountedPage, FoundPage, ObjectOne, Page
from ..objects import count_objects, get_object, is_a, read_objects, shape_of
from ..paging import next_cursor, offset_of, paged_response, start, start_range
from ..params import (
    At,
    Branch,
    Columns,
    Cursor,
    EdgeTypes,
    EdgeWhere,
    FormatParam,
    Limit,
    TypeName,
    Uid,
    page_limit,
    split_list,
)
from ..problems import ApiError
from ..services import Services
from ..tables import fingerprint, provenance_json, readable, version_json

router = APIRouter(prefix=API_PREFIX, tags=["objects"])
log = Log("api.objects")

Where = Annotated[
    list[str] | None,
    Query(
        description="Filters, repeated: `<property>:<op>:<value>` with op one of eq, ne, lt, le, "
        "gt, ge, in (comma-separated values), like (SQL pattern), null, notnull (no value). "
        "Values are read by the property's kind: numbers, true/false, ISO 8601 times with an "
        "offset, dates, text",
        examples=[["elevation:gt:10"]],
    ),
]
Include = Annotated[
    Literal["provenance"] | None,
    Query(description="`provenance` adds the provenance of each value"),
]
Sort = Annotated[
    str | None,
    Query(
        description="Property to sort by, `-` in front for descending; uid order if absent. "
        "Rows without a value come last either way, rows of equal value in uid order; text "
        "is ordered by its UTF-8 bytes, an enumeration as its text. Any number of rows can "
        "be sorted (`api.sort_too_large` is no longer given), but a sorted page costs a scan "
        "of the type's rows that pass the filters, and a deeper page more; the cursor of a "
        "sorted listing is an offset into it",
        max_length=200,
    ),
]
Count = Annotated[
    bool,
    Query(
        description="Also give the number of rows of the whole listing, all pages: `total` in "
        "JSON and the header `x-dta-total` in every format. Costs a count of the selection; "
        "may be asked on any page"
    ),
]
Direction = Annotated[
    Literal["out", "in", "both"],
    Query(description="Edges leaving the object, entering it, or both"),
]
UidLike = Annotated[
    str,
    Query(
        description="SQL pattern the uid matches: `%` stands for any run of characters and "
        "`_` for one, so `node:J%` finds the uids that start with `node:J`",
        examples=["node:J%"],
        min_length=1,
        max_length=200,
    ),
]
Types = Annotated[
    list[str] | None,
    Query(
        description="Only objects of these types, repeated or comma-separated; an abstract "
        "class stands for its concrete types. All domain types if absent",
        examples=[["Node"]],
    ),
]
IncludeMeta = Annotated[
    bool,
    Query(
        description="Also the twin's own records (the types the schema marks `meta`, such as "
        "TwinManifest), which are otherwise left out unless `types` names them"
    ),
]


def counted_response(
    table: pa.Table,
    chosen: Format,
    version: VersionRef,
    following: str | None,
    total: int | None,
) -> Response:
    """A page, with the listing's total in the JSON document and a header when it was counted."""
    response = table_response(
        table, chosen, version, following, None if total is None else {"total": total}
    )
    if total is not None:
        response.headers[TOTAL_HEADER] = str(total)
    return response


def list_type(
    request: Request,
    services: Services,
    type_name: str,
    *,
    at: str,
    branch: str,
    columns: list[str] | None,
    where: list[str] | None,
    prov: bool,
    sort: str | None,
    limit: int | None,
    cursor: str | None,
    fmt: str | None,
    kind: str,
    count: bool = False,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    wanted = split_list(columns)
    # `count` is left out: it does not change the rows, so it may be asked on any page.
    query = fingerprint(kind, type_name, wanted, where, prov, sort)
    version, position = start(services, cursor, kind, query, at, branch)
    ontology = services.ontology(version)
    shape = shape_of(ontology, type_name)
    filters = parse_where(where or [], shape.fields, type_name)
    listing = read_objects(
        services.store,
        ontology,
        version,
        shape,
        columns=wanted,
        filters=filters,
        prov=prov,
        limit=per_page,
        position=position,
        sort=sort,
    )
    total = count_objects(services.store, ontology, version, shape, filters) if count else None
    following = next_cursor(kind, query, version, listing.next_position)
    return counted_response(listing.table, chosen, version, following, total)


@router.get(
    "/objects/{type}",
    summary="Objects of a type at a version",
    description="One row per object: `uid`, then the properties. An abstract class lists its "
    "concrete descendants with the properties they all have and a `_type` column. Paged in "
    "uid order with `cursor`, or in the order of `sort`. With `count=true` the answer also "
    "gives the number of rows of the whole listing.",
    response_model=CountedPage,
    responses=tabular(400, 404, counted=True),
)
def list_objects(
    request: Request,
    services: ServicesDep,
    type: TypeName,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    columns: Columns = None,
    where: Where = None,
    include: Include = None,
    sort: Sort = None,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
    count: Count = False,
) -> Response:
    return list_type(
        request,
        services,
        type,
        at=at,
        branch=branch,
        columns=columns,
        where=where,
        prov=include == "provenance",
        sort=sort,
        limit=limit,
        cursor=cursor,
        fmt=fmt,
        kind="objects",
        count=count,
    )


def selected_types(
    ontology: Ontology, named: Sequence[str] | None, include_meta: bool
) -> list[str] | None:
    """The concrete types a search covers; None for every type."""
    if named:
        found: list[str] = []
        for name in named:
            if name in ontology.abstract:
                found.extend(m for m in ontology.abstract[name] if ontology.has_type(m))
            elif ontology.has_type(name):
                found.append(name)
            else:
                raise UnknownType(f"unknown type {name!r}", detail={"type": name})
        return list(dict.fromkeys(found))
    if include_meta:
        return None
    return [object_type.name for object_type in ontology.types if not object_type.meta]


@router.get(
    "/objects",
    summary="Objects of any type whose uid matches a pattern",
    description="`uid` and `type` of each object whose uid matches `uid_like`, across all "
    "types or those of `types`, in uid order and paged with `cursor`. The twin's own records "
    "are left out unless `types` names them or `include_meta` is true. With `count=true` the "
    "answer also gives the number of objects matched.",
    response_model=FoundPage,
    responses=tabular(400, 404, counted=True),
)
def find_objects(
    request: Request,
    services: ServicesDep,
    uid_like: UidLike,
    types: Types = None,
    include_meta: IncludeMeta = False,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
    count: Count = False,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    named = split_list(types)
    query = fingerprint("find", uid_like, sorted(named) if named else None, include_meta)
    version, position = start(services, cursor, "find", query, at, branch)
    offset = offset_of(position)
    covered = selected_types(services.ontology(version), named, include_meta)
    started = time.perf_counter()
    # One row more than the page tells whether another page follows.
    fetch = None if per_page is None else per_page + 1
    table = services.store.find(uid_like, types=covered, limit=fetch, offset=offset, at=version)
    following = None
    if per_page is not None and table.num_rows > per_page:
        table = table.slice(0, per_page)
        following = next_cursor("find", query, version, offset + per_page)
    total = None
    if count:
        if following is None and (table.num_rows or not offset):
            # This page reaches the end of the selection, so it gives the total itself.
            total = offset + table.num_rows
        else:
            total = services.store.count_found(uid_like, types=covered, at=version)
    log.debug(
        "objects found by uid",
        types=None if covered is None else len(covered),
        offset=offset,
        rows=table.num_rows,
        more=following is not None,
        total=total,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return counted_response(table, chosen, version, following, total)


def object_document(found: StoredObject, prov: bool) -> dict[str, Any]:
    document: dict[str, Any] = {"uid": found.uid, "type": found.type, "values": found.values}
    if prov:
        document["provenance"] = provenance_json(found.provenance)
    return document


@router.get(
    "/objects/{type}/{uid}",
    summary="One object as of a version",
    response_model=ObjectOne,
    responses=versioned(400, 404),
)
def one_object(
    services: ServicesDep,
    type: TypeName,
    uid: Uid,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    include: Include = None,
) -> Response:
    version = services.resolve(at, branch)
    found = get_object(services.store, services.ontology(version), version, uid, type)
    return json_response(
        {
            "data": object_document(found, include == "provenance"),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/object/{uid}",
    summary="One object as of a version, whatever its type",
    response_model=ObjectOne,
    responses=versioned(400, 404),
)
def any_object(
    services: ServicesDep,
    uid: Uid,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    include: Include = None,
) -> Response:
    version = services.resolve(at, branch)
    found = get_object(services.store, services.ontology(version), version, uid, None)
    return json_response(
        {
            "data": object_document(found, include == "provenance"),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/objects/{type}/{uid}/links",
    summary="The edges of one object",
    description="Each edge with its direction seen from the object, its type (a link type, or "
    "`Type.property` for a reference), its ends, the link's uid, and the object at the other "
    "end with its type. `edge_where` keeps, of a filtered link type, the edges whose link "
    "passes.",
    response_model=Page,
    responses=tabular(400, 404),
)
def object_links_route(
    request: Request,
    services: ServicesDep,
    type: TypeName,
    uid: Uid,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    direction: Direction = "both",
    edge_types: EdgeTypes = None,
    edge_where: EdgeWhere = None,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    kinds = split_list(edge_types)
    query = fingerprint("links", type, uid, direction, kinds, *edge_where_identity(edge_where))
    version, position = start(services, cursor, "links", query, at, branch)
    ontology = services.ontology(version)
    edge_filter = parse_edge_where(edge_where, ontology)
    found = get_object(services.store, ontology, version, uid, type)
    table = object_links(
        services.store, ontology, version, uid, found.type, direction, kinds, edge_filter
    )
    return paged_response(table, chosen, version, per_page, position, "links", query)


@router.get(
    "/objects/{type}/{uid}/history",
    summary="How one object changed over a range of versions",
    description="One row per version of the range in which the object changed: `_version`, "
    "`_seq`, `_t`, `_change` (added, modified, removed), then the object as it was after the "
    "change. With `properties`, only versions in which one of them changed.",
    response_model=Page,
    responses=tabular(400, 404),
)
def object_history(
    request: Request,
    services: ServicesDep,
    type: TypeName,
    uid: Uid,
    range: Annotated[
        str,
        Query(
            description="Versions to look at: `<a>..<b>` with any selectors, optionally "
            "`/<step>`; a single selector looks at that version only",
            examples=["#0..head"],
            max_length=400,
        ),
    ] = "#0..head",
    branch: Branch = LIVE_BRANCH,
    properties: Columns = None,
    include: Include = None,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    wanted = split_list(properties)
    query = fingerprint("history", type, uid, wanted, include)
    selected, pinned, position = start_range(services, cursor, "history", query, range, branch)
    last = selected[-1]
    ontology = services.ontology(last)
    shape = shape_of(ontology, type)
    for name in wanted or ():
        if name not in shape.fields:
            raise UnknownProperty(
                f"{type} has no property {name!r}", detail={"type": type, "property": name}
            )
    existing = services.store.get(uid, at=last, ontology=ontology)
    concrete = type if not shape.abstract else None
    if existing is not None:
        if not is_a(ontology, existing.type, type):
            raise ApiError(f"{uid!r} is a {existing.type}, not a {type}", code="api.unknown_object")
        concrete = existing.type
    table = services.store.history(
        uid,
        properties=wanted,
        range=pinned,
        branch=last.branch,
        type=concrete,
        prov=include == "provenance",
        ontology=ontology,
    )
    if existing is None and table.num_rows == 0:
        raise ApiError(
            f"no {type} with uid {uid!r} exists in the versions selected on {last.branch}",
            code="api.unknown_object",
        )
    object_type = ontology.type(concrete) if concrete and ontology.has_type(concrete) else None
    return paged_response(
        readable(table, object_type),
        chosen,
        last,
        per_page,
        position,
        "history",
        query,
        more={"r": pinned},
    )
