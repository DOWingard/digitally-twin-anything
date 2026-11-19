"""Response and request documents, as the OpenAPI document describes them.

Handlers build their JSON directly from Arrow tables and return it as is;
these models describe that JSON for clients and agents.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from dta_core.schema import UID_MAX_LENGTH
from pydantic import BaseModel, ConfigDict, Field

Direction = Literal["out", "in", "both"]


JobState = Literal["queued", "running", "succeeded", "failed"]
QuestionStatus = Literal["validated", "provisional", "failing"]


class JobError(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str


class JobOut(BaseModel):
    """Work the API does in the background. Jobs are kept in the memory of the API
    process, the last 200 of them; nothing in a job is a wall-clock time."""

    id: str = Field(description="32 hexadecimal characters", examples=["9f2c" * 8])
    kind: str = Field(description="What the job does: `sync` is one run of the sync pipeline")
    lane: str = Field(
        description="The queue the job waits in: jobs of one lane run one at a time, in "
        "order; `sync` holds the sync runs, `compute` everything else that takes long",
        examples=["sync"],
    )
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
    fidelity: str = Field(
        description="`unknown` until a fidelity check has completed, then `healthy`, "
        "`correcting` or `failed`"
    )
    fidelity_detail: FidelityDetail
    manifest: TwinManifestInfo
    ontology_hash: str
    type_signature: str
    clock: ClockOut
    sync: SyncHealth | None = Field(description="Null for a twin whose manifest sets no interval")
    compute: ComputeHealth = Field(
        description="The worker, the questions by status and the compute job in progress"
    )


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


class ArtifactStored(BaseModel):
    sha256: str = Field(description="SHA-256 of the content; the name the file is kept under")
    size_bytes: int
    location: str = Field(description="Path inside the twin's artifact volume")


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


# -- compute: functions, models, questions, scenarios, derived data ---------------------


class WorkerHealth(BaseModel):
    configured: bool = Field(description="Whether the twin's stack has a worker")
    ok: bool | None = Field(
        description="Whether the worker answers; null without one. Asked at most every 5 s"
    )


class ComputeJob(BaseModel):
    id: str
    kind: str = Field(description="`evaluate`, `measure` or `scenario`")
    state: Literal["queued", "running"]


class QuestionCounts(BaseModel):
    validated: int
    provisional: int
    failing: int


class ComputeHealth(BaseModel):
    """What the twin computes with, and what its questions may claim."""

    worker: WorkerHealth
    questions: QuestionCounts = Field(description="Questions by status at the head")
    job: ComputeJob | None = Field(description="The job of the compute lane in progress, if any")


class RegisterRequest(BaseModel):
    """What registering needs beside the manifest: the twin's own code, when it has any."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"code": None}]})

    code: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the packed code, as `POST /artifacts` kept it; needed when "
        "the manifest names a function or a model family supplied as code",
    )


class RegistryChanges(BaseModel):
    created: list[str]
    changed: list[str]
    unchanged: list[str]
    disabled: list[str] | None = Field(
        default=None, description="Functions whose manifest entry is gone"
    )
    retired: list[str] | None = Field(default=None, description="Models whose entry is gone")
    orphaned: list[str] | None = Field(
        default=None, description="Questions the manifest no longer has; left as they are"
    )


class ActionRegistryChanges(BaseModel):
    created: list[str] = Field(description="Uids of the records made; a new loop is disabled")
    changed: list[str]
    unchanged: list[str]
    orphaned: list[str] = Field(
        description="Declared by an earlier manifest and no longer: disabled, and a loop is "
        "disabled as well"
    )


class Registered(BaseModel):
    functions: RegistryChanges
    models: RegistryChanges
    questions: RegistryChanges
    actions: ActionRegistryChanges = Field(description="The action types (`TwinAction`)")
    loops: ActionRegistryChanges = Field(description="The control loops (`TwinControlLoop`)")
    version: VersionOut = Field(description="The version that holds the registries now")


class FunctionParameter(BaseModel):
    name: str
    kind: str | None = Field(description="Kind of value; null for code of the twin's own")
    description: str | None
    required: bool
    default: Any = None
    choices: list[str]
    fixed: bool = Field(description="Set by the registration; a call may not give it")


class FunctionOut(BaseModel):
    id: str = Field(examples=["f_latest"])
    uid: str = Field(examples=["twin_function:f_latest"])
    kind: Literal["builtin", "registered"]
    entry: str = Field(description="Name of a built-in, or `module:function` of the twin's code")
    description: str | None
    params: dict[str, Any] = Field(description="Arguments fixed by the registration")
    latency_budget_ms: int | None
    trigger: str | None
    env: list[str]
    artifact: str | None = Field(description="SHA-256 of the code of a registered function")
    origin: Literal["manifest", "runtime"]
    status: Literal["active", "disabled"]
    parameters: list[FunctionParameter] | None = Field(
        default=None, description="With one function: what the entry takes"
    )


class FunctionList(BaseModel):
    data: list[FunctionOut]
    next_cursor: str | None
    version: VersionOut


class FunctionOne(BaseModel):
    data: FunctionOut
    version: VersionOut


class FunctionRegistration(BaseModel):
    """A function registered while the twin runs."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"id": "tank_levels", "entry": "latest", "params": {"property": "Tank.level"}}
            ]
        },
    )

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    entry: str = Field(
        min_length=1, max_length=300, description="A built-in, or `module:function` with `artifact`"
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Fixed arguments")
    description: str | None = Field(default=None, max_length=2_000)
    latency_budget: timedelta | None = Field(
        default=None, description="ISO 8601; 30 s when absent", examples=["PT5S"]
    )
    env: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]] = Field(
        default_factory=list, max_length=100
    )
    artifact: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AnswerInterval(BaseModel):
    low: float
    high: float
    level: float


class AnswerOut(BaseModel):
    """What a function or a question answers. A table is in `value` as its rows, a page
    at a time; as Arrow or Parquet the rest of this document is in the schema metadata
    under `dta:answer`."""

    value: Any = Field(description="A JSON value, or the rows of a table")
    unit: str | None = Field(description="UCUM code of the value")
    interval: AnswerInterval | None
    notes: list[str] = Field(
        description="Short codes: `outside_validity`, `no_intervals`, `question_failing`, …"
    )
    version: VersionOut = Field(description="The version that was read")
    function: str
    model: str | None = Field(description="uid of the model that predicted, for a forecast")
    provisional: bool
    reasons: list[str] = Field(description="Why the answer is provisional")
    next_cursor: str | None = Field(default=None, description="For a table: the next page")
    wrote: VersionOut | None = Field(
        default=None, description="The version a function's derived rows were written as"
    )
    rows_written: int | None = None


class ModelOut(BaseModel):
    uid: str = Field(examples=["twin_model:level/theta"])
    spec: str
    candidate: str
    family: str
    code: str | None
    params: dict[str, Any]
    question: str | None
    target: str
    features: list[str]
    reads: list[str] | None = Field(
        default=None,
        description="Every observed property a prediction reads from the twin besides its "
        "features; absent when not declared, and code of the twin's own is then taken to "
        "read every property",
    )
    interventional: bool
    origin: Literal["manifest", "runtime"]
    status: Literal["candidate", "active", "rejected", "retired"]
    fitted: str | None = Field(description="SHA-256 of the fitted state")
    fitted_from: datetime | None
    fitted_to: datetime | None
    fitted_at_version: str | None
    validity: dict[str, Any] | None
    state: dict[str, Any] | None = Field(
        default=None,
        description="With one model: the header of its fitted state (fitted range, series, "
        "validity, library versions), read without loading the state",
    )


class ModelList(BaseModel):
    data: list[ModelOut]
    next_cursor: str | None
    version: VersionOut


class ModelOne(BaseModel):
    data: ModelOut
    version: VersionOut


class ModelRegistration(BaseModel):
    """A candidate registered while the twin runs."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "spec": "level",
                    "candidate": "naive",
                    "family": "persistence",
                    "target": "Tank.level",
                }
            ]
        },
    )

    spec: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    candidate: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    family: str | None = Field(default=None, max_length=100, description="A shipped family")
    artifact: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    entry: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$",
        description="`module:name` of a family in the code `artifact`",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    target: str = Field(max_length=200, description="`Type.property`")
    features: list[Annotated[str, Field(max_length=2_000)]] = Field(
        default_factory=list,
        max_length=200,
        description="Each a `Type.property`, or the JSON of a feature (property, lag, via, agg)",
    )
    reads: list[Annotated[str, Field(max_length=200)]] | None = Field(
        default=None,
        max_length=200,
        description="Each a `Type.property`: every observed property a prediction reads from "
        "the twin besides its features. Absent, code of the twin's own is taken to read "
        "every property",
    )
    question: str | None = Field(default=None, max_length=100)
    interventional: bool = False


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})

    spec: str | None = Field(
        default=None, max_length=100, description="One specification; all when absent"
    )
    budget_s: float | None = Field(
        default=None, gt=0, le=86_400, description="Seconds the job may start new work for"
    )


class PredictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"horizon": 4}]})

    horizon: int | timedelta | None = Field(
        default=None,
        description="Steps of the twin's grid, or an ISO 8601 duration; the horizon of the "
        "model's question when absent",
        examples=[4, "PT1H"],
    )
    levels: list[Annotated[float, Field(gt=0, lt=100)]] | None = Field(
        default=None, max_length=10, description="Coverage of the intervals in percent"
    )
    interventions: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=1_000,
        description="Assumed changes: `{target, uid, value, start, end?}`; only for a model "
        "that is interventional",
    )


class PredictionOut(Page):
    notes: list[str] = Field(description="`outside_validity`, `no_intervals`, …")
    model: str


class QuestionOut(BaseModel):
    id: str
    uid: str
    status: Literal["validated", "provisional", "failing"]
    reason: str | None
    metric: str | None
    measured_error: float | None
    tolerance: float | None
    unit: str | None
    basis: Literal["backtest", "noise_floor", "check", "latency"] | None
    function: str | None = Field(description="Id of the function that answers it")
    model: str | None = Field(description="uid of the active model")
    backtest: str | None
    measured_at: datetime | None = Field(description="Data time of the version measured at")


class QuestionList(BaseModel):
    data: list[QuestionOut]
    next_cursor: str | None
    version: VersionOut


class QuestionOne(BaseModel):
    data: QuestionOut
    version: VersionOut


class MeasureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})

    questions: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, min_length=1, max_length=1_000, description="Ids; all when absent"
    )


class ScenarioOut(BaseModel):
    id: str = Field(description="Twelve hexadecimal characters", examples=["0a1b2c3d4e5f"])
    uid: str = Field(examples=["twin_scenario:0a1b2c3d4e5f"])
    name: str
    description: str | None = None
    branch: str = Field(examples=["scenario-0a1b2c3d4e5f"])
    base_version: str
    base_t: datetime
    interventions: list[dict[str, Any]]
    horizon: str | None
    step: str | None
    models: list[str]
    status: Literal["created", "running", "done", "failed", "deleted"]
    error: str | None = None
    exists: bool | None = Field(default=None, description="Whether its branch exists now")
    head: dict[str, Any] | None = Field(default=None, description="`seq` and `t` of the branch")
    created: bool | None = Field(default=None, description="On creation: whether it was made")


class ScenarioList(BaseModel):
    data: list[ScenarioOut]
    next_cursor: str | None
    version: VersionOut


class ScenarioOne(BaseModel):
    data: ScenarioOut
    version: VersionOut


class ScenarioRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})

    budget_s: float | None = Field(
        default=None, gt=0, le=86_400, description="Seconds every model has to answer in"
    )


class DerivedRequest(BaseModel):
    """Rows that were worked out, to be written as one version."""

    model_config = ConfigDict(extra="forbid")

    branch: str = Field(min_length=1, max_length=100)
    type: str = Field(min_length=1, max_length=200)
    rows: list[dict[str, Any]] = Field(description="Each `uid` and the properties written")
    kind: Literal["inferred", "assumed", "simulated", "observed"]
    source: str = Field(
        min_length=1, max_length=190, description="uid of the function or model that made them"
    )
    inputs_version: str = Field(
        min_length=1, max_length=200, description="Id of the version they were worked out from"
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    t_valid: datetime | None = Field(default=None, description="When they were true")


class DerivedWritten(BaseModel):
    version: VersionOut
    rows: int


class TrainingPage(Page):
    meta: dict[str, Any] = Field(
        description="Range, step, grid, columns, `backfill_until` and a note on what the rows hold"
    )


class SearchPage(BaseModel):
    data: list[dict[str, Any]] = Field(description="`uid`, `type` and `name` of each object")
    next_cursor: str | None
    version: VersionOut
    total: int | None = None


# -- fidelity ------------------------------------------------------------------------------

FidelityState = Literal["unknown", "healthy", "correcting", "failed"]
IncidentKind = Literal["breach", "stale", "signal", "reconcile"]
IncidentStatus = Literal["open", "remodel_requested", "failed", "resolved"]
IncidentFilter = Literal["open", "remodel_requested", "failed", "resolved", "unresolved"]
SignalStatus = Literal["suspect", "ok"]
CorrectionRung = Literal["recalibrate", "reselect"]

_CHECK_UID = "twin_fidelity_check:20260119T001500Z"
_INCIDENT_UID = "twin_incident:breach-level_ahead-20260119T001500Z"
_SIGNAL_UID = "twin_signal:Gauge.level/gauge-1"


class FidelityCheckRef(BaseModel):
    uid: str = Field(examples=[_CHECK_UID])
    at: datetime | None = Field(description="The check's time on the twin's clock")


class FidelityQuestion(BaseModel):
    """What the last check measured of one predictive question."""

    id: str = Field(examples=["level_ahead"])
    model: str | None = Field(description="The active model the check measured")
    origin: datetime | None = Field(description="The origin scored, or tried, at that check")
    scored: bool
    error: float | None = Field(description="The scored origin's error in the question's metric")
    n: int | None = Field(description="Points that error was measured on")
    window_error: float | None = Field(description="Mean error of the origins in the window")
    window_n: int | None = Field(description="Origins in the window")
    tolerance: float | None = Field(description="The question's tolerance, ε")
    metric: str | None = Field(examples=["mae"])
    breach: bool = Field(description="At least 3 origins in the window, and their mean above ε")
    warning_sum: float | None = Field(description="The drift warning's running sum")
    reason: dict[str, Any] | None = Field(
        description="Why nothing was scored at that check: `code` and `message`",
        examples=[{"code": "already_scored", "message": "this origin was scored before"}],
    )


class IncidentBrief(BaseModel):
    uid: str = Field(examples=[_INCIDENT_UID])
    kind: IncidentKind
    subject: str = Field(description="The question, signal or object it is about")
    status: IncidentStatus
    rung: int | None = Field(description="1 recalibrate, 2 reselect, 3 re-model")


_SIGNAL_VALUE = (
    "What is in force for the property while the signal is suspect: `inferred` (the "
    "forecast made when it was flagged stands in) or `unknown` (that forecast ran out, "
    "or was not finite or outside the property's range: no value, and whatever reads it "
    "is refused with `fidelity.input_unknown`); null once cleared"
)


class SignalBrief(BaseModel):
    uid: str = Field(examples=[_SIGNAL_UID])
    property: str = Field(examples=["Gauge.level"])
    object: str = Field(examples=["gauge-1"])
    reason: str | None = Field(examples=["stuck"])
    since: datetime | None
    value: Literal["inferred", "unknown"] | None = Field(default=None, description=_SIGNAL_VALUE)


class FidelityOut(BaseModel):
    """The twin's fidelity: its state and what the last check found."""

    state: FidelityState
    since: datetime | None = Field(description="Data time the state began")
    check: FidelityCheckRef | None = Field(description="The last check; null before the first")
    questions: list[FidelityQuestion]
    freshness: dict[str, Any] = Field(
        description="As the last check measured it: per question `fresh`, `age_s`, "
        "`staleness_s`, `source`; per source what the twin knows of it"
    )
    coverage: dict[str, Any] = Field(
        description="Questions by status, and the rows the sync runs in the window set aside"
    )
    incidents: list[IncidentBrief] = Field(description="Incidents not resolved, oldest first")
    suspect_signals: list[SignalBrief]
    version: VersionOut


class FidelityCheckOut(BaseModel):
    """One check's record."""

    uid: str = Field(examples=[_CHECK_UID])
    at: datetime | None
    at_version: str | None = Field(description="Id of the head the check measured")
    state: str | None = Field(description="The twin's state the check left")
    questions: dict[str, Any] = Field(description="Per question what was measured")
    freshness: dict[str, Any]
    signals: dict[str, Any] = Field(description="Signals tested, flagged, cleared, inferred")
    coverage: dict[str, Any]
    duration_ms: int | None
    trace_id: str | None


class FidelityCheckList(BaseModel):
    data: list[FidelityCheckOut] = Field(description="Newest first")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    version: VersionOut


class FidelityCheckOne(BaseModel):
    data: FidelityCheckOut
    version: VersionOut


class IncidentOut(BaseModel):
    """An incident and every step it went through."""

    uid: str = Field(examples=[_INCIDENT_UID])
    kind: IncidentKind
    subject: str | None
    status: IncidentStatus
    opened_at: datetime | None
    opened_version: str | None
    resolved_at: datetime | None
    rung: int | None
    attempt: int | None
    cycle: int | None
    awaiting: datetime | None = Field(
        description="End of the data of the model a correction put in place, while it is judged"
    )
    requested_at: datetime | None = Field(description="When a re-model was asked for")
    detail: dict[str, Any] = Field(
        description="Question, specification, pending correction; for a breach, "
        "`next_cycle_at`: when its next correction cycle is due, if one is"
    )
    steps: list[dict[str, Any]] = Field(
        description="Every step, oldest first; a step of a breach's ladder carries the "
        "`cycle` it belongs to and `next_cycle_at`, when the next cycle is due (null: none)"
    )
    resolved_version: str | None = Field(
        default=None, description="On one incident: the version that resolved it"
    )


class IncidentList(BaseModel):
    data: list[IncidentOut] = Field(description="Newest first")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    version: VersionOut


class IncidentOne(BaseModel):
    data: IncidentOut
    version: VersionOut


class SignalOut(BaseModel):
    """An observed signal found suspect at some time."""

    uid: str = Field(examples=[_SIGNAL_UID])
    property: str | None
    object: str | None
    status: SignalStatus
    reason: str | None
    since: datetime | None
    value_at_flag: Any = Field(description="The value when it was flagged")
    reading: Any = Field(description="The latest raw reading sync kept instead of the value")
    reading_at: datetime | None
    clear_checks: int
    value: Literal["inferred", "unknown"] | None = Field(default=None, description=_SIGNAL_VALUE)
    unknown: dict[str, Any] | None = Field(
        default=None,
        description="While the value is unknown: `since`, `why` (`horizon_passed`, "
        "`not_finite`, `out_of_range`, `no_inference`), `message` and the `questions` left "
        "without an answer",
    )
    detail: dict[str, Any] = Field(
        description="The stuck or biased test's numbers; `inference`: the forecast made "
        "when it was flagged (`question`, `model`, `origin`, `horizon_steps`, `values`, "
        "`range`) or why there is none (`refused`); `estimate`: the inferred value in force "
        "and its `lead_steps`; `episodes`: each time it was suspect"
    )


class SignalList(BaseModel):
    data: list[SignalOut] = Field(description="By property, then object")
    version: VersionOut


class OperatorStep(BaseModel):
    """Who takes a step by hand, and why."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"by": "operator@plant", "note": "sensor replaced"}]},
    )

    by: str = Field(min_length=1, max_length=200, description="Who takes the step")
    note: str | None = Field(default=None, max_length=2_000)


class IncidentStepped(BaseModel):
    data: IncidentOut
    events: list[dict[str, Any]] = Field(description="What was told the notifier, in order")
    version: VersionOut | None = Field(description="The version written; null when none was")


class SignalCleared(BaseModel):
    data: SignalOut
    events: list[dict[str, Any]]
    version: VersionOut | None


class CorrectionAsked(BaseModel):
    """A correction asked for by hand."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"question": "level_ahead", "rung": "recalibrate"}]},
    )

    question: str = Field(min_length=1, max_length=190)
    rung: CorrectionRung = Field(
        description="`recalibrate`: re-fit the active model; `reselect`: evaluate every "
        "candidate that is not retired and make the best accepted one active"
    )


class ScheduleEntryOut(BaseModel):
    id: str = Field(examples=["sync"])
    kind: str = Field(description="`sync` entries are asked for first", examples=["sync"])
    interval: str = Field(description="ISO 8601 duration", examples=["PT15M"])
    method: str = Field(examples=["POST"])
    path: str = Field(examples=["/api/v1/sync/runs"])
    key: str = Field(
        description="The Idempotency-Key to send, `{boundary}` replaced by the boundary in "
        "ISO 8601",
        examples=["sync:{boundary}"],
    )
    body: dict[str, Any]


class ScheduleOut(BaseModel):
    now: datetime | None = Field(description="The twin's clock; null when it cannot be read")
    entries: list[ScheduleEntryOut]


class NotificationOut(BaseModel):
    sequence: int
    id: str = Field(description="The event's CloudEvents id")
    type: str = Field(examples=["twin.fidelity.state"])
    subject: str | None
    time: str = Field(description="Data time of what it reports")
    severity: Literal["warning", "critical"] | None = Field(
        default=None,
        description="The event's `severity` extension attribute, when it says how urgent it "
        "is; `critical` is the highest",
    )
    status: Literal["queued", "delivered", "failed", "dropped", "logged", "unsent"]
    tries: int
    http_status: int | None = Field(description="The status of the webhook's last answer")
    error: str | None = Field(description="Why the last try failed, without the address")
    data: dict[str, Any] | None


class NotificationList(BaseModel):
    data: list[NotificationOut] = Field(description="Newest first, the last 200")
    kind: Literal["webhook", "log"]


class FidelityDetail(BaseModel):
    since: datetime | None
    check: FidelityCheckRef | None
    incidents: dict[str, int] = Field(
        description="`open` (not resolved and not failed) and `failed`",
        examples=[{"open": 1, "failed": 0}],
    )
    suspect_signals: int


# -- actions and control loops -------------------------------------------------------------

ActionStatus = Literal["proposed", "rejected", "approved", "blocked", "executed", "failed"]
ReconciliationStatus = Literal["pending", "ok", "mismatch"]
RecordOrigin = Literal["failsafe", "loop", "proposal"]
Autonomy = Literal["advisory", "supervised", "autonomous"]

_RECORD_UID = "twin_action_record:5b1f0c2e9d4a47b8a3c6e1f0d2b4a6c8"


class ActionTypeOut(BaseModel):
    """An action type: what it sets, within which bounds, at which autonomy, and how."""

    id: str = Field(examples=["set_pump"])
    uid: str = Field(examples=["twin_action:set_pump"])
    description: str | None
    control: str | None = Field(
        description="The control it carries out; null for one that only advises"
    )
    target: str | None = Field(description="`Type.property` the control sets")
    object_type: str | None = Field(description="Type of the objects it acts on")
    binding: str | None = Field(description="`executor` (an external call) or `advisory`")
    autonomy: Autonomy = Field(description="The autonomy of its control")
    parameters: dict[str, Any] = Field(description="The parameters a proposal gives, by name")
    bounds: dict[str, Any] | None = Field(
        description="`minimum`, `maximum`, `allowed`, `max_step` of the control"
    )
    executor: dict[str, Any] | None = Field(
        description="The HTTP call: method, templates of the address, headers and body, and "
        "the names of the variables it needs; their values are never shown"
    )
    settle: str | None = Field(
        description="ISO 8601 duration after which its effect must be observable"
    )
    tolerance: float = Field(description="How far the observed value may lie from the value set")
    precondition: str | None
    expected_effect: str | None
    origin: str | None
    status: str | None = Field(description="`active`, or `disabled` once no longer declared")
    executable: bool = Field(
        description="Whether the twin can execute it: bound to an executor, supervised or "
        "autonomous, and an executor configured"
    )
    on_failure: dict[str, Any] | None = Field(
        default=None,
        description="What its control sends, once per failure, to each object the twin "
        "commanded under it when the twin can no longer vouch for itself, as the manifest "
        "says: `mode` (`hold`, `release`, `safe_value`), `action` (the action type sent; for "
        "a safe value the control's own unless another is named), `parameters`, "
        "`after_stale`; null when the control says nothing or it carries out no control. "
        "This is not a safety function: a twin that is not running sends nothing",
        examples=[{"mode": "safe_value", "action": "set_pump", "parameters": {"value": False}}],
    )


class ActionTypeList(BaseModel):
    data: list[ActionTypeOut] = Field(description="By id")
    executor_configured: bool = Field(
        description="Whether the executors add-on is on and the API makes external calls"
    )
    version: VersionOut


class ActionTypeOne(BaseModel):
    data: ActionTypeOut
    executor_configured: bool
    version: VersionOut


class ActionProposal(BaseModel):
    """An action proposed on one object."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "object": "link:PMP-10",
                    "params": {"value": True},
                    "reason": "the tank is low",
                    "source": "operator@plant",
                }
            ]
        },
    )

    object: str = Field(min_length=1, max_length=UID_MAX_LENGTH, description="Uid of the object")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="The parameters of the action type; `value` for one that sets a control",
    )
    reason: str | None = Field(default=None, max_length=2_000)
    source: str = Field(
        min_length=1, max_length=200, description="Who proposes: an agent's or a person's name"
    )


class ActionRecordOut(BaseModel):
    """What became of one proposal."""

    id: str
    uid: str = Field(examples=[_RECORD_UID])
    action: str | None = Field(description="Id of the action type")
    object: str | None
    params: dict[str, Any]
    reason: str | None
    source: str | None = Field(
        description="Who proposed it: a name, a control loop's uid, or `failsafe:<control>` "
        "for what a control's `on_failure` sent"
    )
    origin: RecordOrigin = Field(
        description="Where it came from, as its source tells: a control's fail-safe, a control "
        "loop's run, or anyone else's proposal"
    )
    proposed_at: datetime | None = Field(description="The twin's clock when it was proposed")
    at_version: str | None = Field(description="Id of the head it was judged at")
    fidelity: str | None = Field(description="The twin's fidelity state it was judged in")
    status: ActionStatus | None
    reasons: list[dict[str, Any]] = Field(
        description="Why it was rejected or blocked: `member` and `message` each"
    )
    approval: dict[str, Any] | None = Field(description="`decision`, `by`, `note`, `at`")
    execution: dict[str, Any] | None = Field(
        description="`at`, `effective_at`, `http_status`, `excerpt` of the answer, "
        "`duration_ms`, `attempts`, `error`, `idempotency_key`"
    )
    effective_at: datetime | None = Field(description="When the change took effect")
    reconciliation: ReconciliationStatus | None = Field(
        description="Whether its effect was observed, for one that set a control"
    )
    observed: dict[str, Any] | None = Field(
        description="What reconciliation compared: `value`, `expected`, `difference`, "
        "`tolerance`, `t`, `source`, `kind`, `version`"
    )
    reconciled_at: datetime | None


class ActionRecordOne(BaseModel):
    data: ActionRecordOut
    version: VersionOut


class ActionRecordList(BaseModel):
    data: list[ActionRecordOut] = Field(description="Newest proposal first")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page")
    version: VersionOut


class ActionDecided(BaseModel):
    data: ActionRecordOut
    created: bool = Field(
        description="False when the Idempotency-Key gave back the record made for it before"
    )
    version: VersionOut = Field(description="The head once the request was answered")


class ActionStepped(BaseModel):
    data: ActionRecordOut
    version: VersionOut = Field(description="The head once the request was answered")


class ReconciledOut(BaseModel):
    """One executed action compared with what was observed after it."""

    record: str = Field(examples=[_RECORD_UID])
    action: str
    control: str
    object: str
    target: str = Field(description="`Type.property` compared")
    value: Any = Field(description="What the action set")
    tolerance: float
    outcome: Literal["ok", "mismatch"]
    observed: Any = Field(description="The observed value in force")
    observed_at: datetime | None = Field(description="Valid time of that value")
    observed_source: str | None
    effective_at: datetime
    reconciled_at: datetime
    incidents: list[str] = Field(
        description="The reconcile incident a mismatch opened (or found open), or those an "
        "`ok` closed"
    )


class ReconcileOut(BaseModel):
    data: list[ReconciledOut] = Field(description="In the order the actions took effect")
    opened: list[str] = Field(description="Uids of the incidents opened")
    closed: list[str] = Field(description="Uids of the incidents closed")
    events: list[dict[str, Any]] = Field(description="What was told the notifier, in order")
    now: datetime = Field(description="The twin's clock the records were compared at")
    version: VersionOut = Field(description="The head once it was written")


class ControlLoopOut(BaseModel):
    """A control loop and what its last run did."""

    id: str = Field(examples=["pumping"])
    uid: str = Field(examples=["twin_control_loop:pumping"])
    description: str | None
    controls: list[str] = Field(description="The controls it drives")
    policy: str | None = Field(description="Id of its policy function")
    interval: str | None = Field(description="ISO 8601 duration between two runs")
    origin: str | None
    status: str | None = Field(description="`active`, or `disabled` once no longer declared")
    enabled: bool
    last_run_at: datetime | None
    last_run: dict[str, Any] | None = Field(
        description="`key`, `at`, `status`, how many actions were `proposed`, `executed`, "
        "`blocked`, `rejected`, `failed` or are `awaiting` a decision, the `records`, the "
        "policy's `notes` and the `error` of a failed run"
    )


class ControlLoopList(BaseModel):
    data: list[ControlLoopOut] = Field(description="By id")
    version: VersionOut


class ControlLoopOne(BaseModel):
    data: ControlLoopOut
    version: VersionOut


class LoopSwitch(BaseModel):
    """Who enables or disables a loop."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"by": "operator@plant"}]}
    )

    by: str = Field(min_length=1, max_length=200, description="Who takes the step")


# Defined above the fidelity models it refers to.
TwinHealth.model_rebuild()
