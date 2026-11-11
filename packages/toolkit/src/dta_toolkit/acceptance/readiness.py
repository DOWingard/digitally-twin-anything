"""Data readiness: whether the served twin answers what an agent needs, R1 to R5.

R1 the schema, R2 objects by type and by uid with their links, R3 the graph
around an object, R4 history (when anything is timed), R5 whole tables as
Arrow and Parquet. `smoke` sends one request per requirement, which is what a
build checks; the `readiness_*` functions are the full checks of acceptance.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.schema import Ontology

from .client import TwinApi
from .result import CheckResult, described, problem_of

log = Log("acceptance.readiness")

API = "/api/v1"
JSON_PAGE = 10_000
_HISTORY_CANDIDATES = 10
# Points of history that show a property changed over time.
_HISTORY_POINTS = 2
# Versions, ending at the head, of the slice R4 asks for. A slice grows with objects times
# versions and the API refuses a large one, so the check asks for one object over a bounded
# range, whatever the twin's size and age.
SLICE_VERSIONS = 200


@dataclass
class Context:
    """What the checks share: the API, the manifest, the ontology file, the served schema."""

    api: TwinApi
    manifest: Manifest
    ontology: Ontology
    _schema: dict[str, Any] | None = field(default=None, repr=False)

    def schema(self) -> dict[str, Any] | None:
        if self._schema is None:
            response = self.api.get(f"{API}/meta/schema")
            if response.status_code == 200:
                self._schema = response.json()
        return self._schema


def mapped_types(manifest: Manifest) -> list[str]:
    """Types some mapping fills, in the manifest's order."""
    found: dict[str, None] = {}
    for _sid, source in sorted((manifest.sources or {}).items()):
        for _mid, mapping in sorted((source.mappings or {}).items()):
            if mapping.object_type:
                found.setdefault(mapping.object_type)
    return list(found)


def timed_properties(manifest: Manifest) -> list[tuple[str, str]]:
    """(type, property) of every property a mapping with a valid time fills."""
    found: dict[tuple[str, str], None] = {}
    for _sid, source in sorted((manifest.sources or {}).items()):
        for _mid, mapping in sorted((source.mappings or {}).items()):
            if mapping.valid_time and mapping.object_type:
                for name in sorted(mapping.properties or {}):
                    found.setdefault((mapping.object_type, name))
    return list(found)


def domain_edge_types(schema: dict[str, Any], holders: list[str]) -> list[str]:
    """Edge types held by the given types, in the order the schema lists them."""
    return [
        edge["name"] for edge in schema.get("edge_types") or [] if edge.get("holder") in holders
    ]


def _path(*parts: str) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def _check(
    ident: str,
    examined: str,
    remedy: str,
    body: Callable[[dict[str, Any]], bool],
) -> CheckResult:
    """Run one check body; it fills the evidence and answers whether the check passed."""
    evidence: dict[str, Any] = {}
    passed = body(evidence)
    if not passed:
        log.warn("readiness check failed", check=ident, evidence=evidence)
    return CheckResult(id=ident, examined=examined, passed=passed, evidence=evidence, remedy=remedy)


def _fail(evidence: dict[str, Any], request: str, response: Any = None, why: str = "") -> bool:
    evidence["failing_request"] = request
    if response is not None:
        evidence["response"] = problem_of(response)
    if why:
        evidence["reason"] = why
    return False


# -- R1 -------------------------------------------------------------------------


def readiness_r1(ctx: Context) -> CheckResult:
    def body(evidence: dict[str, Any]) -> bool:
        request = described("GET", f"{API}/meta/schema")
        response = ctx.api.get(f"{API}/meta/schema")
        if response.status_code != 200:
            return _fail(evidence, request, response)
        served = {entry["name"]: entry for entry in response.json().get("types") or []}
        missing_types, missing_properties, units = [], [], []
        expected = ctx.ontology.domain_types()
        for object_type in expected:
            entry = served.get(object_type.name)
            if entry is None:
                missing_types.append(object_type.name)
                continue
            properties = {prop["name"]: prop for prop in entry.get("properties") or []}
            for field_ in object_type.fields:
                prop = properties.get(field_.name)
                if prop is None:
                    missing_properties.append(f"{object_type.name}.{field_.name}")
                elif field_.unit and prop.get("unit") != field_.unit:
                    units.append(
                        {
                            "property": f"{object_type.name}.{field_.name}",
                            "expected": field_.unit,
                            "served": prop.get("unit"),
                        }
                    )
        evidence.update(
            {
                "request": request,
                "types_expected": len(expected),
                "types_served": len(served),
                "missing_types": missing_types,
                "missing_properties": missing_properties,
                "unit_mismatches": units,
            }
        )
        return not (missing_types or missing_properties or units)

    return _check(
        "readiness.R1",
        "/meta/schema lists every concrete type of the ontology file with its properties and units",
        "Install the twin with the current ontology file again: dta build --from install.",
        body,
    )


# -- R2 -------------------------------------------------------------------------


def readiness_r2(ctx: Context) -> CheckResult:
    def body(evidence: dict[str, Any]) -> bool:
        types = mapped_types(ctx.manifest)
        per_type = []
        evidence["types"] = per_type
        ok = bool(types)
        if not types:
            evidence["reason"] = "no mapping fills any type"
        for type_name in types:
            entry: dict[str, Any] = {"type": type_name}
            per_type.append(entry)
            params = {"limit": 1}
            listing = ctx.api.get(f"{API}/objects/{_path(type_name)}", params=params)
            if listing.status_code != 200 or not listing.json().get("data"):
                entry["failing_request"] = described("GET", f"{API}/objects/{type_name}", params)
                entry["response"] = problem_of(listing)
                entry["reason"] = "no object listed" if listing.status_code == 200 else "error"
                ok = False
                continue
            uid = listing.json()["data"][0]["uid"]
            entry["uid"] = uid
            one = ctx.api.get(f"{API}/objects/{_path(type_name, uid)}")
            if one.status_code != 200 or one.json().get("data", {}).get("uid") != uid:
                entry["failing_request"] = described("GET", f"{API}/objects/{type_name}/{uid}")
                entry["response"] = problem_of(one)
                ok = False
                continue
            links = ctx.api.get(
                f"{API}/objects/{_path(type_name, uid)}/links", params={"limit": 100}
            )
            if links.status_code != 200 or not isinstance(links.json().get("data"), list):
                entry["failing_request"] = described(
                    "GET", f"{API}/objects/{type_name}/{uid}/links"
                )
                entry["response"] = problem_of(links)
                ok = False
                continue
            entry["links"] = len(links.json()["data"])
        return ok

    return _check(
        "readiness.R2",
        "every mapped type lists an object, returns it by uid and returns its links",
        "Check the mappings of the types without objects and the load report "
        "(.dta/build.json); then dta build --from load.",
        body,
    )


# -- R3 -------------------------------------------------------------------------


def _first_edge(ctx: Context, evidence: dict[str, Any]) -> dict[str, Any] | None:
    schema = ctx.schema()
    kinds = domain_edge_types(schema or {}, mapped_types(ctx.manifest))
    if not kinds:
        evidence["reason"] = "the ontology gives the mapped types no edge"
        return None
    params = {"edge_types": ",".join(kinds), "limit": 1}
    response = ctx.api.get(f"{API}/graph/edges", params=params)
    if response.status_code != 200:
        _fail(evidence, described("GET", f"{API}/graph/edges", params), response)
        return None
    rows = response.json().get("data") or []
    if not rows:
        _fail(evidence, described("GET", f"{API}/graph/edges", params), why="the graph has no edge")
        return None
    return rows[0]


def readiness_r3(ctx: Context) -> CheckResult:
    def body(evidence: dict[str, Any]) -> bool:
        edge = _first_edge(ctx, evidence)
        if edge is None:
            return False
        seed, other = edge["src"], edge["dst"]
        evidence.update({"seed": seed, "linked_to": other, "edge_type": edge["edge_type"]})
        for depth in (1, 2):
            params = {"seed": seed, "depth": depth}
            response = ctx.api.get(f"{API}/graph/neighbors", params=params)
            if response.status_code != 200:
                return _fail(evidence, described("GET", f"{API}/graph/neighbors", params), response)
            nodes = [
                node for node in response.json().get("nodes") or [] if node.get("depth", 0) > 0
            ]
            evidence[f"neighbors_depth_{depth}"] = len(nodes)
            if not nodes:
                return _fail(
                    evidence,
                    described("GET", f"{API}/graph/neighbors", params),
                    why=f"no neighbour at depth {depth}",
                )
        request = {"seeds": [seed], "depth": 2}
        response = ctx.api.request("POST", f"{API}/graph/subgraph", json=request)
        if response.status_code != 200:
            return _fail(evidence, f"POST {API}/graph/subgraph {request}", response)
        document = response.json()
        evidence["subgraph_nodes"] = len(document.get("nodes") or [])
        evidence["subgraph_edges"] = len(document.get("edges") or [])
        if not document.get("edges"):
            return _fail(evidence, f"POST {API}/graph/subgraph {request}", why="no edge around it")
        params = {"source": seed, "target": other}
        response = ctx.api.get(f"{API}/graph/paths", params=params)
        if response.status_code != 200:
            return _fail(evidence, described("GET", f"{API}/graph/paths", params), response)
        paths = response.json().get("paths") or []
        evidence["paths"] = len(paths)
        if not paths:
            return _fail(evidence, described("GET", f"{API}/graph/paths", params), why="no path")
        return True

    return _check(
        "readiness.R3",
        "neighbours at depth 1 and 2, a subgraph and a path around an object that has links",
        "Map the reference or link properties that connect the objects, then "
        "dta build --from load.",
        body,
    )


# -- R4 -------------------------------------------------------------------------


def _first_data_version(ctx: Context, evidence: dict[str, Any]) -> dict[str, Any] | None:
    params = {"cause": "sync", "order": "asc", "limit": 1}
    response = ctx.api.get(f"{API}/history/versions", params=params)
    if response.status_code != 200:
        _fail(evidence, described("GET", f"{API}/history/versions", params), response)
        return None
    rows = response.json().get("data") or []
    if not rows:
        _fail(evidence, described("GET", f"{API}/history/versions", params), why="no data version")
        return None
    return rows[0]


def _history_points(ctx: Context, evidence: dict[str, Any]) -> bool:
    for type_name, prop in timed_properties(ctx.manifest):
        listing = ctx.api.get(
            f"{API}/objects/{_path(type_name)}",
            params={"limit": _HISTORY_CANDIDATES, "columns": prop},
        )
        if listing.status_code != 200:
            continue
        for row in listing.json().get("data") or []:
            params = {"range": "#0..head", "properties": prop, "limit": _HISTORY_POINTS}
            path = f"{API}/objects/{_path(type_name, row['uid'])}/history"
            response = ctx.api.get(path, params=params)
            if response.status_code != 200:
                return _fail(
                    evidence,
                    described("GET", f"{API}/objects/{type_name}/{row['uid']}/history", params),
                    response,
                )
            points = len(response.json().get("data") or [])
            if points >= _HISTORY_POINTS:
                evidence["history"] = {
                    "property": f"{type_name}.{prop}",
                    "uid": row["uid"],
                    "points_at_least": points,
                }
                return True
    evidence["reason"] = "no object has two points of history in any timed property"
    return False


def slice_request(first_seq: int, head_seq: int, type_name: str, prop: str, uid: str) -> dict:
    """The slice R4 asks for: one property of one object over the last versions up to the
    head, never from before the first version that holds data."""
    start = max(first_seq, head_seq - SLICE_VERSIONS + 1)
    return {
        "range": f"#{start}..head",
        "mode": "state",
        "layout": "long",
        "uids": uid,
        "properties": prop,
        "limit": SLICE_VERSIONS,
    }


def readiness_r4(ctx: Context) -> CheckResult:
    def body(evidence: dict[str, Any]) -> bool:
        timed = timed_properties(ctx.manifest)
        if not timed:
            evidence["applicable"] = False
            evidence["reason"] = "no mapping has a valid time"
            return True
        evidence["applicable"] = True
        first = _first_data_version(ctx, evidence)
        if first is None:
            return False
        evidence["first_data_version"] = {"seq": first.get("seq"), "t": first.get("t")}
        if not _history_points(ctx, evidence):
            return False
        params = {"from": first["id"], "to": "head"}
        response = ctx.api.get(f"{API}/history/diff", params=params)
        if response.status_code != 200:
            return _fail(evidence, described("GET", f"{API}/history/diff", params), response)
        summary = response.json().get("summary") or []
        changed = sum(
            row.get("added", 0) + row.get("modified", 0) + row.get("removed", 0) for row in summary
        )
        evidence["diff_changes"] = changed
        if not changed:
            return _fail(
                evidence,
                described("GET", f"{API}/history/diff", params),
                why="nothing changed between the first data version and head",
            )
        health = ctx.api.get(f"{API}/meta/health")
        if health.status_code != 200:
            return _fail(evidence, described("GET", f"{API}/meta/health", None), health)
        head_seq = int(health.json()["head"]["seq"])
        # The object whose history was just read: it exists at the head and has changed.
        type_name, prop = evidence["history"]["property"].split(".", 1)
        uid = evidence["history"]["uid"]
        params = slice_request(int(first["seq"]), head_seq, type_name, prop, uid)
        evidence["slice"] = {"type": type_name, **params}
        response = ctx.api.get(f"{API}/block/slice/{_path(type_name)}", params=params)
        if response.status_code != 200:
            return _fail(
                evidence, described("GET", f"{API}/block/slice/{type_name}", params), response
            )
        rows = len(response.json().get("data") or [])
        evidence["slice_rows"] = rows
        if not rows:
            return _fail(
                evidence,
                described("GET", f"{API}/block/slice/{type_name}", params),
                why="the slice is empty",
            )
        return True

    return _check(
        "readiness.R4",
        "property history, a diff since the first data version and a slice of one object over "
        "the latest versions",
        "Check that the timed mappings read their valid-time column and that the source holds "
        "more than one point in time, then dta build --from load.",
        body,
    )


# -- R5 -------------------------------------------------------------------------


def _json_total(ctx: Context, type_name: str) -> int | None:
    total, cursor = 0, None
    while True:
        params: dict[str, Any] = {"limit": JSON_PAGE, "columns": "uid"}
        if cursor:
            params["cursor"] = cursor
        response = ctx.api.get(f"{API}/objects/{_path(type_name)}", params=params)
        if response.status_code != 200:
            return None
        document = response.json()
        total += len(document.get("data") or [])
        cursor = document.get("next_cursor")
        if not cursor:
            return total


def _table_rows(content: bytes, fmt: str) -> int:
    if fmt == "arrow":
        return ipc.open_stream(io.BytesIO(content)).read_all().num_rows
    return pq.read_table(io.BytesIO(content)).num_rows


def _tables(ctx: Context, path: str, label: str, evidence: dict[str, Any]) -> dict[str, int] | None:
    counts = {}
    for fmt in ("arrow", "parquet"):
        params = {"format": fmt}
        response = ctx.api.get(path, params=params)
        if response.status_code != 200:
            _fail(evidence, described("GET", label, params), response)
            return None
        try:
            counts[fmt] = _table_rows(response.content, fmt)
        except (pa.ArrowException, OSError, ValueError) as exc:
            _fail(
                evidence,
                described("GET", label, params),
                why=f"does not parse: {type(exc).__name__}",
            )
            return None
    return counts


def readiness_r5(ctx: Context) -> CheckResult:
    def body(evidence: dict[str, Any]) -> bool:
        types = mapped_types(ctx.manifest)
        nodes, edges = [], []
        evidence.update({"node_tables": nodes, "edge_tables": edges})
        for type_name in types:
            label = f"{API}/block/nodes/{type_name}"
            counts = _tables(ctx, f"{API}/block/nodes/{_path(type_name)}", label, evidence)
            if counts is None:
                return False
            total = _json_total(ctx, type_name)
            nodes.append({"type": type_name, **counts, "json_total": total})
            if total is None or not counts["arrow"] == counts["parquet"] == total:
                return _fail(evidence, described("GET", label), why="row counts differ")
        schema = ctx.schema()
        if schema is None:
            return _fail(evidence, described("GET", f"{API}/meta/schema"), why="schema not served")
        kinds = domain_edge_types(schema, types)
        if not kinds:
            evidence["reason"] = "the mapped types have no edge type"
            return False
        for kind in kinds:
            label = f"{API}/block/edges/{kind}"
            counts = _tables(ctx, f"{API}/block/edges/{_path(kind)}", label, evidence)
            if counts is None:
                return False
            edges.append({"edge_type": kind, **counts})
            if counts["arrow"] != counts["parquet"]:
                return _fail(evidence, described("GET", label), why="row counts differ")
        return True

    return _check(
        "readiness.R5",
        "node and edge tables as Arrow and Parquet, node rows equal to the JSON listing",
        "Look at the API's logs (dta logs api) for the failing table request.",
        body,
    )


# -- the smoke requests of a build ---------------------------------------------


def smoke(ctx: Context, document: dict[str, Any]) -> list[dict[str, Any]]:
    """One request per requirement; each answers 200 with the expected shape or is reported."""
    paths = set((document.get("paths") or {}).keys())
    types = mapped_types(ctx.manifest)
    first = types[0] if types else None
    results: list[dict[str, Any]] = []

    def record(
        requirement: str, template: str, request: str, response: Any, shape_ok: bool
    ) -> None:
        documented = template in paths
        ok = documented and response is not None and response.status_code == 200 and shape_ok
        entry = {"requirement": requirement, "request": request, "documented": documented, "ok": ok}
        if response is not None:
            entry["response"] = problem_of(response)
        results.append(entry)

    response = ctx.api.get(f"{API}/meta/schema")
    record(
        "R1",
        f"{API}/meta/schema",
        described("GET", f"{API}/meta/schema"),
        response,
        response.status_code == 200 and isinstance(response.json().get("types"), list),
    )
    uid = None
    if first is not None:
        params = {"limit": 1}
        response = ctx.api.get(f"{API}/objects/{_path(first)}", params=params)
        shape = response.status_code == 200 and isinstance(response.json().get("data"), list)
        if shape and response.json()["data"]:
            uid = response.json()["data"][0].get("uid")
        record(
            "R2",
            f"{API}/objects/{{type}}",
            described("GET", f"{API}/objects/{first}", params),
            response,
            shape,
        )
    else:
        record("R2", f"{API}/objects/{{type}}", "no mapped type", None, False)
    if uid is not None:
        params = {"seed": uid, "depth": 1}
        response = ctx.api.get(f"{API}/graph/neighbors", params=params)
        shape = response.status_code == 200 and isinstance(response.json().get("nodes"), list)
        record(
            "R3",
            f"{API}/graph/neighbors",
            described("GET", f"{API}/graph/neighbors", params),
            response,
            shape,
        )
    else:
        params = {"limit": 1}
        response = ctx.api.get(f"{API}/graph/edges", params=params)
        shape = response.status_code == 200 and isinstance(response.json().get("data"), list)
        record(
            "R3",
            f"{API}/graph/edges",
            described("GET", f"{API}/graph/edges", params),
            response,
            shape,
        )
    params = {"from": "#0", "to": "head"}
    response = ctx.api.get(f"{API}/history/diff", params=params)
    shape = response.status_code == 200 and isinstance(response.json().get("summary"), list)
    record(
        "R4",
        f"{API}/history/diff",
        described("GET", f"{API}/history/diff", params),
        response,
        shape,
    )
    if first is not None:
        params = {"format": "arrow"}
        response = ctx.api.get(f"{API}/block/nodes/{_path(first)}", params=params)
        shape = False
        if response.status_code == 200:
            try:
                _table_rows(response.content, "arrow")
                shape = True
            except (pa.ArrowException, OSError, ValueError) as exc:
                log.warn("node table does not parse as Arrow", type=first, error=type(exc).__name__)
        record(
            "R5",
            f"{API}/block/nodes/{{type}}",
            described("GET", f"{API}/block/nodes/{first}", params),
            response,
            shape,
        )
    else:
        record("R5", f"{API}/block/nodes/{{type}}", "no mapped type", None, False)
    return results
