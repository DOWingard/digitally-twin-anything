"""Response and request documents, as the OpenAPI document describes them.

Handlers build their JSON directly from Arrow tables and return it as is;
these models describe that JSON for clients and agents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Direction = Literal["out", "in", "both"]


class Problem(BaseModel):
    """An error, as RFC 9457 describes it."""

    type: str = Field(description="`urn:dta:error:<code>`")
    title: str
    status: int
    detail: str
    code: str = Field(description="Stable machine-readable error code")
    trace_id: str | None = Field(description="Trace id of the request, for the logs")
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Constraint violations or invalid parameters"
    )


class Health(BaseModel):
    status: Literal["ok"]


class VersionOut(BaseModel):
    """The version a response describes."""

    id: str = Field(description="Commit id")
    seq: int = Field(description="Position on the branch; the installation is #0")
    t: datetime = Field(description="Version time (data time), UTC")
    branch: str


class VersionDetail(VersionOut):
    cause: str
    actor: str
    run: str | None
    trace: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    written_at: datetime | None = Field(description="Wall-clock time of the write, for audit")
    note: str
    extra: dict[str, Any]
    parents: list[str]


class VersionList(BaseModel):
    data: list[VersionDetail]
    next_cursor: str | None
    version: VersionOut = Field(description="The end of the selected range")


class VersionOne(BaseModel):
    data: VersionDetail


class Branch(BaseModel):
    name: str
    head: VersionOut | None


class BranchList(BaseModel):
    data: list[Branch]


class Page(BaseModel):
    """Rows of a table. Values follow the table's columns; times are ISO 8601 UTC."""

    data: list[dict[str, Any]]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    version: VersionOut


class TensorPage(Page):
    uids: list[str] = Field(description="uid of each uid_index")
    properties: list[str] = Field(description="Property of each property_index")
    versions: list[VersionOut] = Field(description="Version of each version_index")


class ProvenanceOut(BaseModel):
    source: str
    kind: str
    confidence: float
    t_valid: datetime


class ObjectOut(BaseModel):
    uid: str
    type: str
    values: dict[str, Any]
    provenance: dict[str, ProvenanceOut] | None = Field(
        default=None, description="By property; `*` covers properties without their own entry"
    )


class ObjectOne(BaseModel):
    data: ObjectOut
    version: VersionOut


class TwinManifestInfo(BaseModel):
    revision: int | None
    hash: str | None


class TwinHealth(BaseModel):
    twin_id: str | None
    name: str | None
    tier: str | None
    head: VersionOut
    versions: int = Field(description="Number of versions on the branch")
    fidelity: str = Field(description="`unknown` until fidelity is measured")
    manifest: TwinManifestInfo
    ontology_hash: str
    type_signature: str


class ManifestOut(BaseModel):
    manifest: dict[str, Any]
    revision: int | None
    manifest_hash: str | None
    version: VersionOut


class PropertyOut(BaseModel):
    name: str
    kind: str
    required: bool
    multivalued: bool
    target: str | None = Field(description="Referenced class, or enum name")
    role: str
    unit: str | None = Field(description="UCUM code")
    minimum: float | None
    maximum: float | None
    pattern: str | None
    long_text: bool
    description: str


class LinkEnds(BaseModel):
    source: str = Field(description="Property holding the source object")
    target: str = Field(description="Property holding the target object")
    source_type: str | None
    target_type: str | None


class TypeOut(BaseModel):
    name: str
    kind: Literal["object", "link"]
    ancestors: list[str]
    link: LinkEnds | None
    meta: bool
    event: bool
    description: str
    properties: list[PropertyOut]
    checks: list[dict[str, str]]


class EdgeTypeOut(BaseModel):
    name: str
    kind: Literal["link", "reference"]
    holder: str = Field(description="Type whose objects hold the edge")
    source_types: list[str]
    target_types: list[str]


class SchemaOut(BaseModel):
    types: list[TypeOut]
    abstract: dict[str, list[str]] = Field(description="Abstract class -> concrete descendants")
    enums: dict[str, list[str]]
    edge_types: list[EdgeTypeOut]
    hash: str = Field(description="Identifies the ontology exactly")
    type_signature: str = Field(description="Fingerprint of the domain types' shape")
    version: VersionOut


class DiffCount(BaseModel):
    type: str
    added: int
    modified: int
    removed: int


class DiffOut(BaseModel):
    summary: list[DiffCount]
    rows: dict[str, list[dict[str, Any]]] | None = Field(
        default=None, description="With detail=rows: changed rows by type"
    )
    truncated: dict[str, bool] | None = Field(
        default=None, description="With detail=rows: whether a type's rows were cut at limit"
    )
    from_version: VersionOut = Field(alias="from")
    to_version: VersionOut = Field(alias="to")
    version: VersionOut

    model_config = ConfigDict(populate_by_name=True)


class NodeOut(BaseModel):
    uid: str
    type: str | None
    depth: int


class EdgeOut(BaseModel):
    src: str
    dst: str
    edge_type: str
    link_uid: str | None


class GraphOut(BaseModel):
    nodes: list[dict[str, Any]] = Field(
        description="uid, type and depth of each node, then any properties asked for"
    )
    edges: list[EdgeOut]
    truncated: bool = Field(description="Whether nodes were left out because of the limit")
    version: VersionOut


class PathOut(BaseModel):
    nodes: list[str]
    edges: list[EdgeOut]
    hops: int
    cost: float


class PathsOut(BaseModel):
    source: str
    target: str
    paths: list[PathOut]
    version: VersionOut


class SubgraphRequest(BaseModel):
    """A bounded part of the graph around some objects."""

    model_config = ConfigDict(extra="forbid")

    seeds: list[str] = Field(min_length=1, max_length=1_000, description="uids to start from")
    depth: int = Field(2, ge=0, le=16, description="Hops from the seeds")
    direction: Direction = Field("both", description="Edge direction to follow")
    edge_types: list[str] | None = Field(
        None, description="Edge types to follow (link type names, `Type.property`); all if absent"
    )
    node_types: list[str] | None = Field(
        None,
        description="Return only nodes of these types (abstract names allowed), besides the "
        "seeds; the traversal passes through every node",
    )
    limit: int = Field(1_000, ge=1, le=100_000, description="Most nodes returned")
    properties: list[str] | None = Field(None, description="Properties to add to each node")


class LogsOut(BaseModel):
    data: list[dict[str, Any]] = Field(description="Log lines, newest first, with `service`")
    skipped: int = Field(description="Lines read that were not JSON log records")
