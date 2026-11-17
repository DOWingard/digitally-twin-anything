"""Manifest schema: the complete specification of one twin build.

Every field is optional at the type level so that a draft parses while it is
still being filled in. Whether a field is required, who owns it and under which
condition are carried as field metadata and evaluated in `status.py`. Collections
are maps keyed by id, so every field has a stable dotted path
(`contract.questions.q1.tolerance.value`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schema import FactKind

MANIFEST_VERSION = 1
_ID = r"^[a-z][a-z0-9_-]{0,62}$"


class Status(StrEnum):
    unknown = "unknown"  # no value yet
    proposed = "proposed"  # value set, waiting for the user to confirm it
    derived = "derived"  # computed by the toolkit from measurements
    confirmed = "confirmed"  # accepted by the field's owner


class Owner(StrEnum):
    user = "user"  # only the user can settle it; asked about in discussion
    agent = "agent"  # settled by the building agent; shown at sign-off, not asked


class Tier(StrEnum):
    static = "static"
    live = "live"


class QuestionKind(StrEnum):
    descriptive = "descriptive"
    diagnostic = "diagnostic"
    predictive = "predictive"
    prescriptive = "prescriptive"


class Autonomy(StrEnum):
    advisory = "advisory"
    supervised = "supervised"
    autonomous = "autonomous"


class ErrorMetric(StrEnum):
    """How the gap between the twin's answer and the truth is measured; lower is better."""

    mae = "mae"
    rmse = "rmse"
    mape = "mape"
    max_abs = "max_abs"
    error_rate = "error_rate"


class ConnectorKind(StrEnum):
    file = "file"
    sql = "sql"
    http = "http"
    custom = "custom"


def req(owner: Owner, description: str, when: str | None = None, **kwargs: Any) -> Any:
    """A required field. `when` names a condition from `status.CONDITIONS`."""
    meta = {"required": True, "owner": owner.value, "when": when}
    return Field(default=None, description=description, json_schema_extra={"dta": meta}, **kwargs)


def opt(owner: Owner, description: str, default: Any = None, **kwargs: Any) -> Any:
    """An optional field; never a gap."""
    meta = {"required": False, "owner": owner.value, "when": None}
    if callable(default):
        return Field(
            default_factory=default,
            description=description,
            json_schema_extra={"dta": meta},
            **kwargs,
        )
    return Field(
        default=default, description=description, json_schema_extra={"dta": meta}, **kwargs
    )


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# -- contract -----------------------------------------------------------------


class Tolerance(Base):
    metric: ErrorMetric | None = req(Owner.user, "Error measure the tolerance applies to")
    value: float | None = req(Owner.user, "Largest acceptable error", gt=0)
    unit: str | None = opt(Owner.user, "Unit of the value, as a UCUM code")


class Question(Base):
    text: str | None = req(Owner.user, "The information or decision wanted")
    kind: QuestionKind | None = req(
        Owner.user, "Descriptive, diagnostic, predictive or prescriptive"
    )
    priority: int | None = req(Owner.user, "1 is the most important", ge=1)
    tolerance: Tolerance | None = req(Owner.user, "Acceptable error of the answer")
    staleness: timedelta | None = req(Owner.user, "Oldest data the answer may rest on")
    targets: list[str] | None = req(
        Owner.agent, "Ontology properties the answer depends on, as Type.property"
    )
    horizon: timedelta | None = req(Owner.agent, "How far ahead the answer looks", when="forecast")
    function: str | None = req(Owner.agent, "Id of the function that answers it")
    example: dict[str, Any] = opt(
        Owner.agent,
        "Arguments the question is asked with when the twin checks that it can answer it",
        dict,
    )
    model: str | None = req(Owner.agent, "Id of the model spec behind it", when="forecast")
    provisional: bool = opt(
        Owner.agent, "Set when history is too short to validate the answer at build", False
    )


class Bounds(Base):
    minimum: float | None = opt(Owner.user, "Lowest value the twin may set")
    maximum: float | None = opt(Owner.user, "Highest value the twin may set")
    allowed: list[str | float | bool] | None = opt(Owner.user, "Discrete values the twin may set")
    max_step: float | None = opt(Owner.user, "Largest change in one action")


class Control(Base):
    description: str | None = req(Owner.user, "What the twin is responsible for controlling")
    target: str | None = req(Owner.user, "Ontology property the control sets, as Type.property")
    bounds: Bounds | None = req(Owner.user, "Limits the twin must stay inside")
    autonomy: Autonomy | None = req(Owner.user, "Advisory, supervised or autonomous")
    action: str | None = req(Owner.agent, "Id of the action type that carries it out")


class Constraint(Base):
    description: str | None = req(Owner.user, "A limit that must hold while optimising")
    expression: str | None = opt(Owner.agent, "Machine-checkable form over ontology properties")


class Objective(Base):
    description: str | None = req(Owner.user, "The outcome to improve")
    direction: Literal["minimize", "maximize"] | None = req(Owner.user, "Which way is better")
    kpi: str | None = req(Owner.agent, "Id of the function that computes it")
    constraints: dict[str, Constraint] = opt(Owner.user, "Limits on the optimisation", dict)


class Boundary(Base):
    """Roles of ontology properties: which are state, controls, drivers, observations."""

    state: list[str] | None = req(Owner.agent, "Properties that make up the system state")
    controls: list[str] = opt(Owner.agent, "Properties that can be set", list)
    drivers: list[str] = opt(Owner.agent, "Outside influences that cannot be set", list)
    observations: list[str] | None = req(Owner.agent, "Properties that sources report")


class Notification(Base):
    kind: Literal["webhook", "log"] | None = req(Owner.user, "Where failures are raised")
    webhook_env: str | None = req(
        Owner.user, "Name of the variable holding the webhook address", when="webhook"
    )


class Addons(Base):
    sdk_python: bool = opt(Owner.user, "Generate a typed Python client", False)
    mcp: bool = opt(Owner.user, "Generate an MCP adapter from the API description", False)
    executors: bool = opt(Owner.user, "Let actions call external systems", False)
    llm_extraction: bool = opt(
        Owner.user, "Run a language model inside the twin to re-read documents", False
    )
    readonly_sql: bool = opt(Owner.user, "Expose read-only SQL over the history", False)


class Contract(Base):
    purpose: str | None = req(Owner.user, "What the twin is for")
    tier: Tier | None = req(Owner.user, "Static (one load) or live (recurring sync)")
    cadence_preference: timedelta | None = opt(Owner.user, "Sync interval the user asked for")
    questions: dict[str, Question] | None = req(Owner.user, "What the twin must answer or decide")
    controls: dict[str, Control] = opt(Owner.user, "Controls delegated to the twin", dict)
    objective: Objective | None = req(
        Owner.user, "What control and advice optimise", when="objective"
    )
    boundary: Boundary | None = req(Owner.agent, "Roles of the ontology's properties")
    notification: Notification | None = req(Owner.user, "Where failures are raised", when="live")
    addons: Addons = opt(Owner.user, "Optional parts of the build", Addons)
    hosting: Literal["local", "cloud"] = opt(Owner.user, "Where the twin is meant to run", "local")


# -- ontology -----------------------------------------------------------------


class OntologyRef(Base):
    path: str | None = req(
        Owner.agent, "LinkML file of the domain ontology, relative to the workspace"
    )
    type_signature: str | None = req(
        Owner.user, "Hash of the object and link types the user confirmed"
    )


# -- sources ------------------------------------------------------------------


class ColumnProfile(Base):
    dtype: str
    null_fraction: float = Field(ge=0, le=1)
    distinct_count: int = Field(ge=0)
    unique: bool = False
    minimum: float | None = None
    maximum: float | None = None
    noise_sigma: float | None = Field(default=None, ge=0, description="Noise floor of the signal")
    change_time: timedelta | None = Field(default=None, description="Characteristic time of change")


class DatasetProfile(Base):
    rows: int = Field(ge=0)
    columns: dict[str, ColumnProfile]
    candidate_keys: list[list[str]] = Field(default_factory=list)
    timestamp_columns: list[str] = Field(default_factory=list)
    time_column: str | None = Field(
        default=None, description="The timestamp column the history is ordered by"
    )
    entity_columns: list[str] = Field(
        default_factory=list,
        description="Columns that, with the time column, identify one row of a time series",
    )
    history_start: datetime | None = None
    history_end: datetime | None = None
    refresh_period: timedelta | None = Field(
        default=None, description="Median gap between consecutive timestamps"
    )
    sampled_rows: int | None = Field(
        default=None, ge=0, description="Rows the statistics were computed on, when not all"
    )


class SourceProfile(Base):
    """Written by the toolkit's read-only profiler."""

    profiled_at: datetime
    datasets: dict[str, DatasetProfile]
    refresh_period: timedelta | None = None
    rate_limit_period: timedelta | None = None


class PropertyMapping(Base):
    column: str | None = opt(Owner.agent, "Source column the value is read from")
    expression: str | None = opt(
        Owner.agent, "Transformation applied when one column is not enough"
    )
    unit: str | None = opt(Owner.agent, "Unit in the source, as a UCUM code")


class Mapping(Base):
    dataset: str | None = req(Owner.agent, "Table, file pattern or endpoint inside the source")
    object_type: str | None = req(Owner.agent, "Ontology object or link type the rows become")
    identity: list[str] | None = req(Owner.agent, "Columns that identify one object")
    properties: dict[str, PropertyMapping] | None = req(
        Owner.agent, "Ontology property to source column"
    )
    valid_time: str | None = opt(
        Owner.agent, "Column carrying when the fact was true; else time of read"
    )
    kind: FactKind = opt(Owner.agent, "How the facts were obtained", FactKind.observed)
    mode: Literal["upsert", "snapshot"] = opt(
        Owner.agent,
        "upsert keeps an object that is missing from a read; snapshot deletes it",
        "upsert",
    )
    filter: str | None = opt(Owner.agent, "Condition a row must meet to be mapped")
    cursor: str | None = opt(
        Owner.agent, "Column that only grows, so a later read can take only new rows"
    )


class Source(Base):
    description: str | None = opt(Owner.user, "What the source is")
    connector: ConnectorKind | None = req(Owner.agent, "How the source is read")
    location: str | None = req(Owner.agent, "Address with ${VARIABLE} placeholders; never a secret")
    env: list[str] = opt(Owner.agent, "Names of the variables the connector needs", list)
    options: dict[str, Any] = opt(Owner.agent, "Connector settings such as format or paging", dict)
    refresh_period: timedelta | None = req(
        Owner.agent, "How often the source hands over something new", when="live_series"
    )
    rate_limit_period: timedelta | None = opt(Owner.agent, "Shortest allowed gap between reads")
    edits_past: bool = opt(Owner.agent, "The source rewrites its own history", False)
    host_local: bool = opt(Owner.agent, "A changing file on the user's machine", False)
    views: dict[str, str] = opt(
        Owner.agent, "Named SELECT statements over the datasets read in one run", dict
    )
    incremental: dict[str, str] = opt(
        Owner.agent,
        "Datasets read on from where the last run stopped, each with the column holding the "
        "time of its rows. A view over one sees only the new rows, so each of its rows must "
        "depend only on rows of the dataset with the same time",
        dict,
    )
    profile: SourceProfile | None = req(Owner.agent, "Result of read-only profiling")
    mappings: dict[str, Mapping] | None = req(Owner.agent, "How rows become ontology objects")


# -- sync ---------------------------------------------------------------------


class SyncDerivation(Base):
    """Inputs of the interval rule, kept so the result can be re-derived and audited."""

    source_period: timedelta | None = None
    rate_period: timedelta | None = None
    min_staleness: timedelta | None = None
    # The longest interval that keeps every question inside its staleness: the least,
    # over questions and the sources they rest on, of staleness less refresh period.
    freshness_bound: timedelta | None = None
    change_time: timedelta | None = None
    rule: str = "max(source_period, rate_period, min(freshness_bound, change_time / 2))"


class Sync(Base):
    interval: timedelta | None = req(Owner.agent, "Time between sync runs", when="live")
    minimum_interval: timedelta | None = opt(Owner.agent, "Shortest interval correction may choose")
    maximum_interval: timedelta | None = opt(Owner.agent, "Longest interval correction may choose")
    derivation: SyncDerivation | None = opt(Owner.agent, "How the interval was derived")
    aggregation: Literal["last", "mean", "min", "max"] = opt(
        Owner.agent, "How signals faster than the interval are reduced before commit", "last"
    )
    backfill_interval: timedelta | None = opt(
        Owner.agent, "Width of the versions in which existing history is loaded"
    )
    assimilation: str = opt(Owner.agent, "How observations are merged into state", "direct")


# -- behaviour and functions --------------------------------------------------


class Candidate(Base):
    family: str | None = req(Owner.agent, "Model family")
    params: dict[str, Any] = opt(Owner.agent, "Fixed settings of the family", dict)


class Backtest(Base):
    scheme: Literal["walk_forward"] = opt(Owner.agent, "Evaluation scheme", "walk_forward")
    initial_window: timedelta | None = req(Owner.agent, "History used before the first forecast")
    step: timedelta | None = req(Owner.agent, "Time between forecast origins")
    min_folds: int = opt(Owner.agent, "Fewest origins for a valid backtest", 3, ge=1)


class Acceptance(Base):
    metric: ErrorMetric | None = req(Owner.agent, "Error measure used to accept a model")
    threshold: float | None = req(Owner.agent, "Largest acceptable backtest error", gt=0)
    baseline: Literal["persistence", "seasonal"] | None = opt(Owner.agent, "Naive model to beat")


class ModelSpec(Base):
    question: str | None = req(Owner.agent, "Question the model serves")
    target: str | None = req(Owner.agent, "Ontology property predicted, as Type.property")
    candidates: dict[str, Candidate] | None = req(Owner.agent, "Families the twin may select among")
    # None are needed by a family that predicts a target from its own history, or by one
    # that reads what it needs from the twin itself.
    features: list[str] = opt(Owner.agent, "Ontology properties used as inputs", list)
    backtest: Backtest | None = req(Owner.agent, "How candidates are evaluated")
    acceptance: Acceptance | None = req(Owner.agent, "When a candidate is good enough")
    interventional: bool = opt(Owner.agent, "Valid for what-if changes to controls", False)


class FunctionSpec(Base):
    description: str | None = opt(Owner.agent, "What it computes")
    kind: Literal["builtin", "registered"] | None = req(
        Owner.agent, "Shipped with the runtime or supplied as code"
    )
    entry: str | None = req(
        Owner.agent, "Built-in name, or module:function inside the code artifact"
    )
    params: dict[str, Any] = opt(Owner.agent, "Fixed arguments", dict)
    complexity: str | None = req(Owner.agent, "Cost in objects, links and versions, such as O(|E|)")
    latency_budget: timedelta | None = req(Owner.agent, "Longest acceptable run time")
    trigger: Literal["on_request", "on_commit", "interval"] = opt(
        Owner.agent, "When it runs", "on_request"
    )
    interval: timedelta | None = opt(Owner.agent, "Period when the trigger is interval")
    env: list[str] = opt(
        Owner.agent, "Names of the variables the function's code may read; it sees no other", list
    )


# -- actions and control ------------------------------------------------------


class Executor(Base):
    kind: Literal["http"] | None = req(Owner.agent, "How the external system is called")
    url: str | None = req(Owner.agent, "Address with ${VARIABLE} placeholders; never a secret")
    method: Literal["POST", "PUT", "PATCH"] = opt(Owner.agent, "HTTP method", "POST")
    env: list[str] = opt(Owner.agent, "Names of the variables the call needs", list)
    body: dict[str, Any] = opt(
        Owner.agent, "Request body template over the action's parameters", dict
    )
    headers: dict[str, str] = opt(
        Owner.agent, "Header templates; values may reference variables", dict
    )


class Parameter(Base):
    type: Literal["number", "integer", "string", "boolean"] | None = req(Owner.agent, "Value type")
    unit: str | None = opt(Owner.agent, "Unit, as a UCUM code")
    minimum: float | None = opt(Owner.agent, "Lowest accepted value")
    maximum: float | None = opt(Owner.agent, "Highest accepted value")
    allowed: list[str | float | bool] | None = opt(Owner.agent, "Accepted values")


class ActionType(Base):
    description: str | None = opt(Owner.agent, "What the action does to the real system")
    control: str | None = opt(Owner.agent, "Delegated control it carries out")
    object_type: str | None = opt(Owner.agent, "Ontology type the action is applied to")
    binding: Literal["advisory", "executor"] | None = req(
        Owner.agent, "Recommendation only, or an external call"
    )
    executor: Executor | None = req(Owner.agent, "The external call", when="executor")
    parameters: dict[str, Parameter] = opt(Owner.agent, "Arguments of the action", dict)
    precondition: str | None = opt(Owner.agent, "Condition on the graph that must hold")
    expected_effect: str | None = opt(Owner.agent, "What should be observed afterwards")


class ControlLoop(Base):
    description: str | None = opt(Owner.agent, "What the loop regulates")
    controls: list[str] | None = req(Owner.agent, "Delegated controls it drives")
    policy: str | None = req(Owner.agent, "Id of the function that chooses the action")
    interval: timedelta | None = req(Owner.agent, "Time between decisions")


# -- fidelity -----------------------------------------------------------------


class Correction(Base):
    recalibrate_attempts: int = opt(
        Owner.agent, "Tries of re-fitting parameters before escalating", 2, ge=0
    )
    reselect: bool = opt(Owner.agent, "May switch among the declared candidates", True)
    agent_remodel: bool = opt(Owner.agent, "May ask the managing agent to re-model", True)
    retry_budget: int = opt(
        Owner.agent, "Correction cycles before the twin is declared failed", 3, ge=1
    )


class Fidelity(Base):
    interval: timedelta | None = req(Owner.agent, "Time between fidelity checks", when="live")
    window: timedelta | None = req(
        Owner.agent, "History each check measures error over", when="live"
    )
    correction: Correction = opt(Owner.agent, "Limits on self-correction", Correction)


# -- operation ----------------------------------------------------------------


class Interfaces(Base):
    base_url_env: str = opt(Owner.agent, "Variable holding the API address", "DTA_BASE_URL")
    token_env: str = opt(Owner.agent, "Variable holding the API token", "DTA_API_TOKEN")
    api_port_env: str = opt(
        Owner.agent, "Variable holding the API port on the host", "DTA_API_PORT"
    )
    ui_port_env: str = opt(Owner.agent, "Variable holding the UI port on the host", "DTA_UI_PORT")
    ui: bool = opt(Owner.agent, "Serve the browser UI", True)


class EnvVar(Base):
    description: str | None = req(Owner.agent, "What the variable is for")
    secret: bool = opt(Owner.agent, "Value must never be shown", True)


class Runtime(Base):
    store_image: str = opt(
        Owner.agent, "Image of the versioned store", "dolthub/dolt-sql-server:2.7.3"
    )
    image: str = opt(Owner.agent, "Image the twin's own services run from", "dta-runtime:0.1.0")
    clock: str = opt(
        Owner.agent,
        "Where the twin reads the current time: wall, or source:<source id>",
        "wall",
        pattern=r"^(wall|source:[a-z][a-z0-9_-]*)$",
    )
    networks: list[str] = opt(
        Owner.agent,
        "Existing container networks the twin joins to reach sources running on this machine",
        list,
    )
    scheduler_poll: timedelta = opt(
        Owner.agent,
        "Wall time between two looks of the scheduler at the twin's clock; shorter than the "
        "sync interval takes on the wall when the clock runs faster than it",
        timedelta(seconds=1),
        gt=timedelta(milliseconds=50),
        le=timedelta(hours=1),
    )
    code: str | None = opt(
        Owner.agent,
        "Directory in the workspace holding the twin's own code: registered functions and "
        "model families, named as module:name",
    )
    worker_requirements: str | None = opt(
        Owner.agent,
        "Requirements file in the workspace, installed into the image registered code runs in",
    )
    worker_cpus: float = opt(Owner.agent, "CPU limit of the worker for registered code", 1.0, gt=0)
    worker_memory_mb: int = opt(Owner.agent, "Memory limit of the worker", 2048, ge=128)


class Twin(Base):
    id: str | None = req(Owner.agent, "Short name used for the stack and the skill", pattern=_ID)
    name: str | None = req(Owner.agent, "Display name")
    revision: int = opt(Owner.agent, "Counts manifest revisions", 1, ge=1)


class Manifest(Base):
    manifest_version: Literal[1] = MANIFEST_VERSION
    twin: Twin | None = req(Owner.agent, "Identity of the twin")
    contract: Contract | None = req(Owner.user, "What the twin must do and within which limits")
    ontology: OntologyRef | None = req(Owner.agent, "Domain ontology")
    sources: dict[str, Source] | None = req(Owner.agent, "Where the data comes from")
    sync: Sync | None = req(Owner.agent, "Recurring sync", when="live")
    models: dict[str, ModelSpec] = opt(Owner.agent, "Predictive models", dict)
    functions: dict[str, FunctionSpec] | None = req(
        Owner.agent, "Computations that answer questions"
    )
    actions: dict[str, ActionType] = opt(Owner.agent, "Typed actions on the real system", dict)
    control_loops: dict[str, ControlLoop] = opt(
        Owner.agent, "Loops that drive delegated controls", dict
    )
    fidelity: Fidelity | None = req(Owner.agent, "How error is measured and corrected", when="live")
    interfaces: Interfaces = opt(Owner.agent, "API and UI settings", Interfaces)
    env: dict[str, EnvVar] = opt(Owner.agent, "Every variable the twin needs, by name", dict)
    runtime: Runtime = opt(Owner.agent, "Container resources", Runtime)
    field_status: dict[str, Status] = Field(
        default_factory=dict, description="Explicit status per dotted field path"
    )
