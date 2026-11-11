"""The versions of the twin: commit log, single versions, differences, branches."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import LIVE_BRANCH, Cause, Store, UnknownType, Version, VersionRef
from fastapi import APIRouter, Path, Query, Request
from starlette.responses import Response

from ..deps import ServicesDep
from ..docs import API_PREFIX, errors, tabular, versioned
from ..formats import json_response, negotiate, rows, table_response
from ..models import BranchList, DiffOut, VersionList, VersionOne
from ..params import Branch, Cursor, FormatParam, Limit, json_limit, split_list
from ..problems import ApiError
from ..tables import (
    decode_cursor,
    encode_cursor,
    fingerprint,
    iso,
    readable,
    version_json,
)

router = APIRouter(prefix=f"{API_PREFIX}/history", tags=["history"])
log = Log("api.history")

_CHANGES = ("added", "modified", "removed")
SUMMARY_SCHEMA = pa.schema(
    [
        pa.field("type", pa.string()),
        pa.field("added", pa.int64()),
        pa.field("modified", pa.int64()),
        pa.field("removed", pa.int64()),
    ]
)

From = Annotated[
    str,
    Query(
        alias="from",
        description="First version: any selector (`#<seq>`, commit id, time, `head`)",
        examples=["#0"],
        max_length=200,
    ),
]
To = Annotated[
    str,
    Query(description="Last version: any selector", examples=["head"], max_length=200),
]


def version_detail(version: Version) -> dict[str, Any]:
    return {
        **version_json(version),
        "cause": version.cause.value,
        "actor": version.actor,
        "run": version.run,
        "trace": version.trace,
        "valid_from": iso(version.valid_from) if version.valid_from else None,
        "valid_to": iso(version.valid_to) if version.valid_to else None,
        "written_at": iso(version.written_at) if version.written_at else None,
        "note": version.note,
        "extra": dict(version.extra),
        "parents": list(version.parents),
    }


@router.get(
    "/versions",
    summary="The commit log of a branch",
    description="Versions from `from` to `to`, both included, with everything recorded about "
    "each, filtered by cause, actor, run and whether they were written by a backfill. Pages "
    "follow `seq`.",
    response_model=VersionList,
    responses=versioned(400, 404),
)
def list_versions(
    services: ServicesDep,
    from_: Annotated[
        str,
        Query(
            alias="from",
            description="First version of the range: any selector",
            examples=["#0"],
            max_length=200,
        ),
    ] = "#0",
    to: To = "head",
    branch: Branch = LIVE_BRANCH,
    cause: Annotated[Cause | None, Query(description="Only versions with this cause")] = None,
    actor: Annotated[
        str | None, Query(description="Only versions written by this actor", max_length=200)
    ] = None,
    run: Annotated[
        str | None, Query(description="Only versions of this sync run", max_length=200)
    ] = None,
    backfill: Annotated[
        bool | None,
        Query(description="Only versions written by a backfill (true) or not (false)"),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"], Query(description="Oldest first (asc) or newest first (desc)")
    ] = "asc",
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    per_page = json_limit(limit)
    query = fingerprint("versions", from_, to, branch, cause, actor, run, backfill, order)
    after = None
    if cursor:
        after = decode_cursor(cursor, "versions", query).get("p")
        if not isinstance(after, int) or isinstance(after, bool):
            raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
    selected, pinned = services.resolve_range(f"{from_}..{to}", branch)
    end = selected[-1]
    found = services.store.versions(
        pinned,
        end.branch,
        cause=cause,
        actor=actor,
        run=run,
        extra={"backfill": True} if backfill else None,
        newest_first=order == "desc",
    )
    if backfill is False:
        found = [version for version in found if version.extra.get("backfill") is not True]
    if after is not None:
        found = [
            version
            for version in found
            if (version.seq > after if order == "asc" else version.seq < after)
        ]
    chosen = found[:per_page]
    next_cursor = None
    if len(found) > per_page:
        next_cursor = encode_cursor({"k": "versions", "q": query, "p": chosen[-1].seq})
    return json_response(
        {
            "data": [version_detail(version) for version in chosen],
            "next_cursor": next_cursor,
            "version": version_json(end),
        },
        end,
    )


@router.get(
    "/versions/{selector}",
    summary="Resolve one version selector",
    description="The version a selector names on a branch: `head`, a commit id, `#<seq>` "
    "(written `%23<seq>` in a path) or an ISO 8601 time with an offset.",
    response_model=VersionOne,
    responses=versioned(400, 404),
)
def one_version(
    services: ServicesDep,
    selector: Annotated[str, Path(description="The selector, percent-encoded", max_length=200)],
    branch: Branch = LIVE_BRANCH,
) -> Response:
    version = services.resolve(selector, branch)
    detail = services.store.versions(version.id, version.branch)[0]
    return json_response({"data": version_detail(detail)}, version)


def _types(ontology: Ontology, names: list[str] | None) -> list[str] | None:
    """Type names with abstract classes replaced by their concrete descendants."""
    if names is None:
        return None
    expanded: list[str] = []
    for name in names:
        if name in ontology.abstract:
            expanded.extend(ontology.abstract[name])
        elif ontology.has_type(name):
            expanded.append(name)
        else:
            raise UnknownType(f"unknown type {name!r}", detail={"type": name})
    return list(dict.fromkeys(expanded))


def _empty_diff(store: Store, ontology: Ontology, type_name: str, at: VersionRef) -> pa.Table:
    """A diff table of a type with no rows, for a type that did not change."""
    base = store.table(type_name, limit=0, at=at, ontology=ontology)
    properties = [field for field in base.schema if field.name != "uid"]
    return pa.schema(
        [
            pa.field("_change", pa.string()),
            pa.field("uid", pa.string()),
            *properties,
            pa.field("_before", pa.struct(properties)),
        ]
    ).empty_table()


@router.get(
    "/diff",
    summary="What changed between two versions",
    description="`detail=summary`: rows added, modified and removed per type. `detail=rows`: "
    "the changed rows of each type: `_change`, the properties at `to`, and `_before`, the "
    "properties at `from`; JSON caps each type at `limit` rows. Arrow and Parquet render the "
    "summary, or with `detail=rows` the rows of the one type named in `types`.",
    response_model=DiffOut,
    responses=tabular(400, 404),
)
def diff(
    request: Request,
    services: ServicesDep,
    from_: From,
    to: To = "head",
    branch: Branch = LIVE_BRANCH,
    types: Annotated[
        list[str] | None,
        Query(description="Types to compare, repeated or comma-separated; all if absent"),
    ] = None,
    detail: Annotated[
        Literal["summary", "rows"], Query(description="Counts per type, or the changed rows")
    ] = "summary",
    limit: Limit = None,
    fmt: FormatParam = None,
) -> Response:
    chosen = negotiate(fmt, request.headers.get("accept"))
    first = services.resolve(from_, branch)
    last = services.resolve(to, branch)
    ontology = services.ontology(last)
    wanted = _types(ontology, split_list(types))
    if chosen != "json" and detail == "rows" and (wanted is None or len(wanted) != 1):
        raise ApiError(
            "Arrow and Parquet render the changed rows of one type; name it in types",
            code="api.bad_request",
        )
    found = services.store.diff(first, last, types=wanted, ontology=ontology)
    counts = []
    for type_name, table in found.items():
        tally = dict.fromkeys(_CHANGES, 0)
        for entry in pc.value_counts(table.column("_change")).to_pylist():
            tally[entry["values"]] = entry["counts"]
        counts.append({"type": type_name, **tally})
    summary = pa.Table.from_pylist(counts, schema=SUMMARY_SCHEMA)
    if chosen != "json":
        if detail == "summary":
            return table_response(summary, chosen, last)
        assert wanted is not None
        type_name = wanted[0]
        table = found.get(type_name)
        if table is None:
            table = _empty_diff(services.store, ontology, type_name, last)
        if limit is not None:
            table = table.slice(0, limit)
        return table_response(readable(table, ontology.type(type_name)), chosen, last)
    document: dict[str, Any] = {
        "summary": rows(summary),
        "from": version_json(first),
        "to": version_json(last),
        "version": version_json(last),
    }
    if detail == "rows":
        cap = json_limit(limit)
        document["rows"] = {
            type_name: rows(readable(table.slice(0, cap), ontology.type(type_name)))
            for type_name, table in found.items()
        }
        document["truncated"] = {
            type_name: table.num_rows > cap for type_name, table in found.items()
        }
    return json_response(document, last)


@router.get(
    "/branches",
    summary="The branches of the twin and their heads",
    response_model=BranchList,
    responses=errors(),
)
def branches(services: ServicesDep) -> Response:
    data = []
    for branch in services.store.branches():
        head = None
        try:
            head = version_json(services.store.head(branch.name))
        except DtaError as exc:
            log.warn(
                "the head of a branch cannot be read; listed without it",
                branch=branch.name,
                code=exc.code,
            )
        data.append({"name": branch.name, "head": head})
    head_of_main = services.store.head(LIVE_BRANCH)
    return json_response({"data": data}, head_of_main)
