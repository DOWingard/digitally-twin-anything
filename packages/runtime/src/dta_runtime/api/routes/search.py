"""Finding objects of any type by what they are called."""

from __future__ import annotations

import time
from typing import Annotated

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import UID_FIELD, Kind, Ontology
from dta_core.store import LIVE_BRANCH, VersionRef
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ..deps import ServicesDep
from ..docs import API_PREFIX, tabular
from ..formats import negotiate
from ..models import SearchPage
from ..paging import next_cursor, offset_of, start
from ..params import At, Branch, Cursor, FormatParam, Limit, page_limit, split_list
from ..services import Services
from ..tables import fingerprint
from .objects import counted_response, selected_types

router = APIRouter(prefix=f"{API_PREFIX}/search", tags=["objects"])
log = Log("api.search")

NAME_FIELD = "name"
SCHEMA = pa.schema(
    [
        pa.field(UID_FIELD, pa.string()),
        pa.field("type", pa.string()),
        pa.field(NAME_FIELD, pa.string()),
    ]
)


def _named_types(ontology: Ontology, covered: list[str] | None) -> list[str]:
    """The concrete types searched that have a `name` to match."""
    names = covered if covered is not None else [found.name for found in ontology.types]
    return [
        name
        for name in names
        if ontology.type(name).has_field(NAME_FIELD)
        and ontology.type(name).field(NAME_FIELD).kind is Kind.string
        and not ontology.type(name).field(NAME_FIELD).multivalued
    ]


def _matches(
    services: Services,
    version: VersionRef,
    pattern: str,
    covered: list[str] | None,
    most: int | None,
) -> dict[str, tuple[str, str | None]]:
    """uid -> (type, name where it was read) of the first `most` objects, in uid order,
    whose uid matches, and of the first `most` of each type whose name does: together
    they hold the first `most` of all that match either way."""
    store, ontology = services.store, services.ontology(version)
    found: dict[str, tuple[str, str | None]] = {}
    by_uid = store.find(pattern, types=covered, limit=most, at=version)
    for uid, type_name in zip(
        by_uid.column(UID_FIELD).to_pylist(), by_uid.column("type").to_pylist(), strict=True
    ):
        found[uid] = (type_name, None)
    for type_name in _named_types(ontology, covered):
        by_name = store.table(
            type_name,
            columns=[NAME_FIELD],
            filter=[(NAME_FIELD, "like", pattern)],
            limit=most,
            at=version,
            ontology=ontology,
        )
        for uid, name in zip(
            by_name.column(UID_FIELD).to_pylist(),
            by_name.column(NAME_FIELD).to_pylist(),
            strict=True,
        ):
            found[uid] = (type_name, name)
    return found


def _with_names(
    services: Services, version: VersionRef, page: list[tuple[str, tuple[str, str | None]]]
) -> pa.Table:
    """The page as a table; an object found by its uid alone is given its name too."""
    ontology = services.ontology(version)
    missing: dict[str, list[str]] = {}
    for uid, (type_name, name) in page:
        if name is None and _named_types(ontology, [type_name]):
            missing.setdefault(type_name, []).append(uid)
    names: dict[str, str | None] = {}
    for type_name, uids in missing.items():
        read = services.store.table(
            type_name,
            columns=[NAME_FIELD],
            filter=[(UID_FIELD, "in", uids)],
            at=version,
            ontology=ontology,
        )
        names.update(
            zip(
                read.column(UID_FIELD).to_pylist(),
                read.column(NAME_FIELD).to_pylist(),
                strict=True,
            )
        )
    return pa.Table.from_arrays(
        [
            pa.array([uid for uid, _ in page], pa.string()),
            pa.array([found[0] for _, found in page], pa.string()),
            pa.array(
                [found[1] if found[1] is not None else names.get(uid) for uid, found in page],
                pa.string(),
            ),
        ],
        schema=SCHEMA,
    )


@router.get(
    "",
    summary="Objects of any type whose uid or name matches a pattern",
    description="`uid`, `type` and `name` of each object whose uid or whose `name` matches "
    "`q`, an SQL pattern (`%` for any run of characters, `_` for one), across all types or "
    "those of `types`, in uid order and paged with `cursor`. The twin's own records are "
    "left out unless `types` names them or `include_meta` is true. With `count=true` the "
    "answer also gives the number of objects matched.",
    response_model=SearchPage,
    responses=tabular(400, 404, counted=True),
)
def search(
    request: Request,
    services: ServicesDep,
    q: Annotated[
        str,
        Query(
            description="SQL pattern matched against the uid and the name",
            min_length=1,
            max_length=200,
            examples=["%tank%"],
        ),
    ],
    types: Annotated[
        list[str] | None,
        Query(description="Types searched, repeated or comma-separated; all if absent"),
    ] = None,
    include_meta: Annotated[
        bool, Query(description="Also the twin's own records (types marked `meta`)")
    ] = False,
    at: At = "head",
    branch: Branch = LIVE_BRANCH,
    limit: Limit = None,
    cursor: Cursor = None,
    fmt: FormatParam = None,
    count: Annotated[bool, Query(description="Also give the number of objects matched")] = False,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    per_page = page_limit(chosen, limit)
    named = split_list(types)
    query = fingerprint("search", q, sorted(named) if named else None, include_meta)
    version, position = start(services, cursor, "search", query, at, branch)
    offset = offset_of(position)
    covered = selected_types(services.ontology(version), named, include_meta)
    started = time.perf_counter()
    whole = per_page is None or count
    found = _matches(
        services,
        version,
        q,
        covered,
        None if whole else offset + per_page + 1,  # type: ignore[operator]
    )
    ordered = sorted(found.items())
    end = len(ordered) if per_page is None else offset + per_page
    following = next_cursor("search", query, version, end) if end < len(ordered) else None
    table = _with_names(services, version, ordered[offset:end])
    total = len(ordered) if count else None
    log.debug(
        "objects found by uid or name",
        types=None if covered is None else len(covered),
        offset=offset,
        rows=table.num_rows,
        more=following is not None,
        total=total,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return counted_response(table, chosen, version, following, total)
