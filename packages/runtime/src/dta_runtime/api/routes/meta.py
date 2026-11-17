"""What the twin is: its identity, manifest and ontology at a version."""

from __future__ import annotations

from typing import Any

from dta_core.manifest import to_plain
from dta_core.ontology import type_signature
from dta_core.schema import Ontology
from dta_core.store import LIVE_BRANCH, VersionRef
from fastapi import APIRouter
from starlette.responses import Response

from ...graph import edge_kinds
from .. import registry
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors, versioned
from ..formats import json_response
from ..jobs import COMPUTE_LANE, KEEP
from ..live import sync_document
from ..models import ManifestOut, SchemaOut, TwinHealth
from ..params import At, Branch
from ..services import Services
from ..syncing import SYNC_KIND
from ..tables import version_headers, version_json

router = APIRouter(prefix=f"{API_PREFIX}/meta", tags=["meta"])

FIDELITY_UNKNOWN = "unknown"
QUESTION_STATUSES = ("validated", "provisional", "failing")


YAML_MEDIA = "application/yaml"


class YamlResponse(Response):
    media_type = YAML_MEDIA


def schema_document(ontology: Ontology) -> dict[str, Any]:
    types = []
    for object_type in ontology.types:
        link = None
        if object_type.link is not None:
            source, target = object_type.link
            link = {
                "source": source,
                "target": target,
                "source_type": object_type.field(source).target,
                "target_type": object_type.field(target).target,
            }
        types.append(
            {
                "name": object_type.name,
                "kind": "link" if link else "object",
                "ancestors": list(object_type.ancestors),
                "link": link,
                "meta": object_type.meta,
                "event": object_type.event,
                "description": object_type.description,
                "properties": [
                    {
                        "name": field.name,
                        "kind": field.kind.value,
                        "required": field.required,
                        "multivalued": field.multivalued,
                        "target": field.target,
                        "role": field.role.value,
                        "unit": field.unit,
                        "minimum": field.minimum,
                        "maximum": field.maximum,
                        "pattern": field.pattern,
                        "long_text": field.long_text,
                        "description": field.description,
                    }
                    for field in object_type.fields
                ],
                "checks": [
                    {"name": name, "expression": expression}
                    for name, expression in object_type.checks
                ],
            }
        )
    return {
        "types": types,
        "abstract": {name: list(members) for name, members in sorted(ontology.abstract.items())},
        "enums": {name: list(values) for name, values in sorted(ontology.enums.items())},
        "edge_types": [
            {
                "name": kind.name,
                "kind": "link" if kind.is_link else "reference",
                "holder": kind.holder,
                "source_types": sorted(kind.source_types),
                "target_types": sorted(kind.target_types),
            }
            for kind in edge_kinds(ontology).values()
        ],
        "hash": ontology.hash,
        "type_signature": type_signature(ontology),
    }


def _compute(services: Services, version: VersionRef) -> dict[str, Any]:
    """The worker, the questions by status at a version, and the compute job in progress."""

    def count() -> dict[str, int]:
        found = dict.fromkeys(QUESTION_STATUSES, 0)
        for row in registry.rows(services, registry.QUESTION_TYPE, version):
            if row["status"] in found:
                found[row["status"]] += 1
        return found

    jobs = services.jobs.list(lane=COMPUTE_LANE, limit=KEEP)
    # Newest first: the one running, else the one that waits longest.
    current = next((job for job in jobs if job["state"] == "running"), None) or next(
        (job for job in reversed(jobs) if job["state"] == "queued"), None
    )
    return {
        "worker": {"configured": services.compute.configured, "ok": services.compute.healthy()},
        "questions": services.counted(version, count),
        "job": None
        if current is None
        else {"id": current["id"], "kind": current["kind"], "state": current["state"]},
    }


@router.get(
    "/health",
    summary="Identity of the twin and the state of a branch",
    description="Twin id and name, contract tier, head version, number of versions, fidelity "
    "state, manifest revision and hash, and the ontology in force at the head. `clock` is "
    "what the twin's clock tells now (`available` false when it cannot be read; the answer "
    "is still 200). For a live twin, `sync` says how far the twin is behind its sources: "
    "the freshness of each source on that clock, each question that states a staleness "
    "against the sources it is answered from, the last run that recorded itself and the "
    "sync job in progress. `compute` says what the twin computes with: whether it has a "
    "worker and whether the worker answers (asked at most every 5 s), how many questions "
    "are validated, provisional and failing at the head, and the job of the compute lane "
    "in progress. "
    "Meant to be polled: what depends on the head is read once per head version.",
    response_model=TwinHealth,
    responses=versioned(400, 404),
)
def twin_health(services: ServicesDep, branch: Branch = LIVE_BRANCH) -> Response:
    version = services.resolve("head", branch)
    state = services.head_state(version)
    record = state.record
    reading = services.clocks.read(state.manifest, state.manifest_hash)
    active = services.jobs.active(SYNC_KIND) if state.live else None
    return json_response(
        {
            "twin_id": record.get("twin_id"),
            "name": record.get("name"),
            "tier": record.get("tier"),
            "head": version_json(version),
            "versions": version.seq + 1,
            "fidelity": FIDELITY_UNKNOWN,
            "manifest": {
                "revision": record.get("revision"),
                "hash": record.get("manifest_hash"),
            },
            "ontology_hash": state.ontology.hash,
            "type_signature": state.type_signature,
            "clock": reading.document(),
            "sync": sync_document(
                state,
                reading,
                services.jobs.document(active) if active is not None else None,
            ),
            "compute": _compute(services, version),
        },
        version,
    )


@router.get(
    "/manifest",
    summary="The manifest in force at a version",
    response_model=ManifestOut,
    responses=versioned(400, 404),
)
def manifest(services: ServicesDep, at: At = "head", branch: Branch = LIVE_BRANCH) -> Response:
    version = services.resolve(at, branch)
    record = services.twin.manifest_record(at=version)
    document = to_plain(services.twin.manifest(at=version))
    return json_response(
        {
            "manifest": document,
            "revision": record.get("revision"),
            "manifest_hash": record.get("manifest_hash"),
            "version": version_json(version),
        },
        version,
    )


@router.get(
    "/schema",
    summary="The ontology in force at a version",
    description="Object and link types with their ancestors, link ends and properties (kind, "
    "required, multivalued, target, role, unit, bounds, description), abstract classes with "
    "their concrete descendants, enums, the edge types of the graph, the ontology's hash and "
    "the type signature.",
    response_model=SchemaOut,
    responses=versioned(400, 404),
)
def schema(services: ServicesDep, at: At = "head", branch: Branch = LIVE_BRANCH) -> Response:
    version = services.resolve(at, branch)
    document = schema_document(services.ontology(version))
    document["version"] = version_json(version)
    return json_response(document, version)


@router.get(
    "/schema/linkml",
    summary="The LinkML text of the ontology in force at a version",
    response_class=YamlResponse,
    responses={
        200: {
            "content": {
                YAML_MEDIA: {
                    "schema": {
                        "type": "object",
                        "description": "A LinkML schema document",
                    }
                }
            },
            "description": "The domain ontology as LinkML YAML",
            "headers": VERSION_HEADERS,
        },
        **errors(400, 404),
    },
)
def schema_linkml(services: ServicesDep, at: At = "head", branch: Branch = LIVE_BRANCH) -> Response:
    version = services.resolve(at, branch)
    return YamlResponse(content=services.ontology(version).source, headers=version_headers(version))
