"""Response and request documents, as the OpenAPI document describes them.

Handlers build their JSON directly from Arrow tables and return it as is;
these models describe that JSON for clients and agents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Direction = Literal["out", "in", "both"]


JobState = Literal["queued", "running", "succeeded", "failed"]


class JobError(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str


class JobOut(BaseModel):
    """Work the API does in the background. Jobs are kept in the memory of the API
    process, the last 200 of them; nothing in a job is a wall-clock time."""

    id: str = Field(description="32 hexadecimal characters", examples=["9f2c" * 8])
    kind: str = Field(description="What the job does: `sync` is one run of the sync pipeline")
    state: JobState
    params: dict[str, Any] = Field(
        description="What the job was asked to do; for `sync`, `sources` and `replay`",
        examples=[{"sources": None, "replay": None}],
    )
    idempotency_key: str | None = Field(description="The key the job was asked for with")
    trace_id: str = Field(
        description="Trace of the request that made the job; its work logs under it"
    )
    sequence: int = Field(description="Order in which this API process made its jobs")
    duration_ms: int | None = Field(description="How long the work took, once it has ended")
    result: dict[str, Any] | None = Field(
        description="What the work gave back; for `sync`, the run's report"
    )
    error: JobError | None = Field(
        description="Why the job failed: the run's error, or the error that stopped it. "
        "`job.cancelled`: the API stopped before the job started; `job.unexpected`: an error "
        "of the server, named by its type only"
    )


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
    job: JobOut | None = Field(
        default=None, description="With `sync.busy`: the sync job that is queued or running"
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


class CountedPage(Page):
    total: int | None = Field(
        default=None,
        description="With `count=true`: rows of the whole listing, all pages, after the "
        "filters; absent otherwise",
    )


class FoundObject(BaseModel):
    uid: str = Field(examples=["node:J1"])
    type: str = Field(description="The object's concrete type", examples=["Junction"])


class FoundPage(BaseModel):
    """Objects whose uid matches a pattern, in uid order."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "data": [
                        {"uid": "node:J1", "type": "Junction"},
                        {"uid": "node:J2", "type": "Junction"},
                    ],
                    "next_cursor": None,
                    "version": {
                        "id": "u0h2kqv3b4m5c6n7d8p9f0g1h2j3k4l5",
                        "seq": 21,
                        "t": "2026-01-05T21:00:00Z",
                        "branch": "main",
                    },
                    "total": 2,
                }
            ]
        }
    )

    data: list[FoundObject]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    version: VersionOut
    total: int | None = Field(
        default=None,
        description="With `count=true`: objects matched, all pages; absent otherwise",
    )


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


class ClockOut(BaseModel):
    """The twin's clock: where it reads the current time, and what it reads."""

    kind: str = Field(
        description="`wall`, or `source:<id>` for the clock of one of the twin's sources",
        examples=["wall"],
    )
    available: bool = Field(description="Whether the clock could be read")
    now: datetime | None = Field(
        description="The clock's time, ISO 8601 UTC; null when it cannot be read",
        examples=["2026-01-19T00:15:02Z"],
    )


class SourceFreshness(BaseModel):
    id: str
    status: str | None = Field(description="`ok`, `failing`, or null before it is first read")
    watermark: datetime | None = Field(description="Latest data time taken from the source")
    freshness_s: float | None = Field(description="Clock time less the watermark, in seconds")


class LastRun(BaseModel):
    uid: str = Field(examples=["twin_sync_run:20260119T000000Z-1a2b3c4d"])
    status: str = Field(description="`succeeded`, `partial` or `failed`")
    kind: str | None = Field(description="`incremental`, `replay` or `backfill`")
    started_at: datetime | None = Field(description="Data time of the run's first version")
    finished_at: datetime | None = Field(description="Data time of the run's last version")
    clock_at: datetime | None = Field(description="What the clock told when the run began")
    versions_written: int | None
    rows_written: int | None
    rows_rejected: int | None
    rows_late: int | None
    error: str | None


class ActiveJob(BaseModel):
    id: str
    state: Literal["queued", "running"]


class QuestionFreshness(BaseModel):
    """One question that states a staleness, against the sources it is answered from."""

    id: str
    staleness: str = Field(description="The oldest its data may be, ISO 8601", examples=["PT1H"])
    freshness_s: float | None = Field(
        description="The largest freshness among the sources that map the question's targets "
        "(all sources when its targets lead to none); null when the clock cannot be read or "
        "none of them has a watermark"
    )
    fresh: bool | None = Field(description="Whether `freshness_s` is within `staleness`")
    source: str | None = Field(description="The source that `freshness_s` is the freshness of")


class SyncHealth(BaseModel):
    """How far a live twin is behind its sources."""

    interval: str = Field(description="Time between sync runs, ISO 8601", examples=["PT15M"])
    staleness: str | None = Field(
        description="The staleness of the question nearest its limit, ISO 8601; the shortest "
        "of the contract when none can be measured; null when no question states one",
        examples=["PT1H"],
    )
    freshness_s: float | None = Field(
        description="The freshness of the question nearest its limit. When no question states "
        "a staleness: the largest freshness of the sources. Null when it cannot be measured"
    )
    fresh: bool | None = Field(
        description="False when a question's data is older than its staleness, true when "
        "every question that can be measured is within it, null when none can"
    )
    questions: list[QuestionFreshness] = Field(
        description="The questions that state a staleness, in the contract's order"
    )
    sources: list[SourceFreshness] = Field(description="The manifest's sources")
    last_run: LastRun | None = Field(description="The latest run that recorded itself")
    job: ActiveJob | None = Field(description="The sync job queued or running, if any")


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
    clock: ClockOut
    sync: SyncHealth | None = Field(description="Null for a twin whose manifest sets no interval")


class SyncReplay(BaseModel):
    """A dataset to read again whole, whatever its cursor says."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=200)
    dataset: str = Field(min_length=1, max_length=200)


class SyncRunRequest(BaseModel):
    """What one run is asked to do; an empty body runs every source."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{}, {"sources": ["telemetry"]}]},
    )

    sources: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None,
        min_length=1,
        max_length=1_000,
        description="Keep the run to the mappings of these sources; all if absent",
    )
    replay: SyncReplay | None = Field(
        default=None,
        description="Keep the run to the mappings that read this dataset, and read it whole",
    )


class JobAccepted(BaseModel):
    job: JobOut


class JobOne(BaseModel):
    data: JobOut


class JobList(BaseModel):
    data: list[JobOut] = Field(description="Newest first")


class SyncRunOne(BaseModel):
    data: dict[str, Any] = Field(description="The run's stored values and its uid")
    version: VersionOut


class RejectsOut(BaseModel):
    data: list[dict[str, Any]] = Field(
        description="Rows set aside, in file order: run, source, mapping, type, uid, property, "
        "rule, message, t, and `also` for further rules the row broke"
    )
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    total: int = Field(description="Lines that pass the filters")
    skipped: int = Field(description="Lines of the file that are not JSON objects")


class DatasetOut(BaseModel):
    name: str
    cursor_column: str | None
    cursor: str | None = Field(description="How far it has been read: a time or a number")
    rows_read: int | None
    last_read_at: datetime | None


class MappingOut(BaseModel):
    id: str
    dataset: str | None
    object_type: str | None
    kind: Literal["static", "state", "event"]
    properties: list[str]


class SourceOut(BaseModel):
    id: str
    connector: str | None
    description: str | None
    status: str | None = Field(description="`ok`, `failing`, `disabled`; null before it is read")
    last_error: str | None
    watermark: datetime | None
    last_success_at: datetime | None
    freshness_s: float | None
    datasets: list[DatasetOut]
    mappings: list[MappingOut]


class SourceList(BaseModel):
    data: list[SourceOut] = Field(
        description="In the manifest's order; sources it no longer names last, disabled"
    )
    clock: ClockOut
    version: VersionOut


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
    edge_where: list[str] | None = Field(
        None,
        description="Follow only the links that pass: `<LinkType>.<property>:<op>:<value>` "
        "clauses, written as for `where`; an abstract class stands for every link type under "
        "it. Edges of other link types and references are unaffected",
        examples=[["Pipe.status:eq:open"]],
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
