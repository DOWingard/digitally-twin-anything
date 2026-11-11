"""Objects of the twin as of a version: listings, single objects, links and histories."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from dta_core.store import LIVE_BRANCH, StoredObject, UnknownProperty
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ...graph import links as object_links
from ..deps import ServicesDep
from ..docs import API_PREFIX, tabular, versioned
from ..filters import parse_where
from ..formats import json_response, negotiate, table_response
from ..models import ObjectOne, Page
from ..objects import get_object, is_a, read_objects, shape_of
from ..paging import next_cursor, paged_response, start, start_range
from ..params import (
    At,
    Branch,
    Columns,
    Cursor,
    EdgeTypes,
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
        "Another property than uid sorts the filtered result in memory, which is refused "
        "with api.sort_too_large above 10,000 rows",
        max_length=200,
    ),
]
Direction = Annotated[
    Literal["out", "in", "both"],
    Query(description="Edges leaving the object, entering it, or both"),
]


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
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    wanted = split_list(columns)
    query = fingerprint(kind, type_name, wanted, where, prov, sort)
    version, position = start(services, cursor, kind, query, at, branch)
    ontology = services.ontology(version)
    shape = shape_of(ontology, type_name)
    listing = read_objects(
        services.store,
        ontology,
        version,
        shape,
        columns=wanted,
        filters=parse_where(where or [], shape.fields, type_name),
        prov=prov,
        limit=per_page,
        position=position,
        sort=sort,
    )
    following = next_cursor(kind, query, version, listing.next_position)
    return table_response(listing.table, chosen, version, following)


@router.get(
    "/objects/{type}",
    summary="Objects of a type at a version",
    description="One row per object: `uid`, then the properties. An abstract class lists its "
    "concrete descendants with the properties they all have and a `_type` column. Paged in "
    "uid order with `cursor`.",
    response_model=Page,
    responses=tabular(400, 404),
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
    )


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
    "end with its type.",
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
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    kinds = split_list(edge_types)
    query = fingerprint("links", type, uid, direction, kinds)
    version, position = start(services, cursor, "links", query, at, branch)
    ontology = services.ontology(version)
    found = get_object(services.store, ontology, version, uid, type)
    table = object_links(services.store, ontology, version, uid, found.type, direction, kinds)
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
