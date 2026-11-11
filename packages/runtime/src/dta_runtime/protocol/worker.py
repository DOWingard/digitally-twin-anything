"""What the API asks of the worker, and what the worker answers.

`POST /run` takes one of the requests below as JSON. A request names what to
read, never the data: the worker reads frames through the API and stores what
it produces (a fitted state, residuals, rows to write) as artifacts through the
API, answering with their hashes. An answer is JSON; when it has a table
(a prediction, a function's table) it is an Arrow stream of the table with the
JSON result under `RESULT_KEY` in the schema's metadata.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import pyarrow as pa

    from .answers import Artifacts, Reader

RESULT_KEY = "dta:result"
OPS = ("function", "fit", "backtest", "predict")
SHA256 = r"^[0-9a-f]{64}$"
ENTRY = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"

Sha = Annotated[str, Field(pattern=SHA256)]
Entry = Annotated[str, Field(pattern=ENTRY)]
Levels = Annotated[list[float], Field(default_factory=list)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameRef(_Model):
    """A frame by reference: the target's values in force on a regular grid over a range
    of versions, one series per object, with the features beside it. It is the body of
    `POST /api/v1/block/training-set` without a horizon."""

    # `Type.property`.
    target: str
    # Every object of the type when absent.
    uids: list[str] | None = None
    # Version ids, pinned by the API when the job starts.
    start: str
    end: str
    branch: str = "live"
    # The grid: a row per series every `step` from the first version's time on.
    step: timedelta
    # `Type.property` each, read as the value in force on the same grid.
    features: list[str] = Field(default_factory=list)


class FamilyRef(_Model):
    """A shipped family by name, or code of the twin's own by artifact and entry."""

    name: str | None = None
    artifact: Sha | None = None
    entry: Entry | None = None

    @model_validator(mode="after")
    def _one_of(self) -> FamilyRef:
        code = self.artifact is not None and self.entry is not None
        if (self.name is not None) == code or (self.artifact is None) != (self.entry is None):
            raise ValueError("a family is a shipped name, or an artifact with an entry")
        return self


class InterventionRef(_Model):
    target: str
    uid: str
    value: Any
    start: datetime
    end: datetime | None = None


class _Run(_Model):
    # The longest the run may take on the wall; the worker stops it then.
    budget_s: float = Field(gt=0)
    # Trace of the request or job the run belongs to; the worker logs under it.
    trace_id: str


class FunctionRequest(_Run):
    op: Literal["function"] = "function"
    artifact: Sha
    entry: Entry
    args: dict[str, Any] = Field(default_factory=dict)
    # Id of the version the function reads at, and its branch.
    at: str
    branch: str = "live"
    # Names of the variables the function declared; it sees these and no other.
    env: list[str] = Field(default_factory=list)


class FitRequest(_Run):
    op: Literal["fit"] = "fit"
    family: FamilyRef
    params: dict[str, Any] = Field(default_factory=dict)
    frame: FrameRef


class BacktestRequest(_Run):
    op: Literal["backtest"] = "backtest"
    family: FamilyRef
    params: dict[str, Any] = Field(default_factory=dict)
    frame: FrameRef
    # Steps of the grid predicted from each origin.
    horizon: int = Field(ge=1)
    # Steps of the grid between two origins, and how many origins, the last one `horizon`
    # steps before the end of the frame.
    step: int = Field(ge=1)
    windows: int = Field(ge=1)
    # Fit again at every k-th origin (true: at every origin; false: once).
    refit: int | bool = True
    levels: Levels
    # Names of the manifest's error measures.
    metrics: list[str] = Field(min_length=1)
    # The shipped naive family scored on the same folds.
    baseline: str | None = None


class PredictRequest(_Run):
    op: Literal["predict"] = "predict"
    family: FamilyRef
    params: dict[str, Any] = Field(default_factory=dict)
    # The fitted state, as stored by a fit.
    state: Sha
    history: FrameRef
    horizon: int = Field(ge=1)
    levels: Levels
    # Known future values of features over the horizon, when the family uses any.
    drivers: FrameRef | None = None
    interventions: list[InterventionRef] = Field(default_factory=list)


WorkerRequest = Annotated[
    FunctionRequest | FitRequest | BacktestRequest | PredictRequest, Field(discriminator="op")
]


class _Result(_Model):
    duration_ms: float
    # What the run logged, as the twin's log lines; the API records them under the trace.
    logs: list[dict[str, Any]] = Field(default_factory=list)


class FunctionResult(_Result):
    # The answer's value when it is not a table; a table is the Arrow stream itself.
    value: Any = None
    table: bool = False
    unit: str | None = None
    interval: dict[str, float] | None = None
    notes: list[str] = Field(default_factory=list)
    # Rows the function wants written: the stored table, and what to write it as.
    derived: Sha | None = None
    derived_type: str | None = None
    derived_kind: Literal["inferred", "assumed", "simulated"] | None = None
    derived_confidence: float | None = None


class FitResult(_Result):
    state: Sha
    series: int
    rows: int
    fitted_from: datetime
    fitted_to: datetime
    # Per input column, the range the fit saw.
    validity: dict[str, tuple[float, float]] = Field(default_factory=dict)


class BacktestResult(_Result):
    # Error per measure over every fold and series, and the baseline's on the same folds.
    scores: dict[str, float]
    baseline_scores: dict[str, float] | None = None
    folds: int
    series: int
    refit: int | bool
    # How long the first fit took: what the cost of refitting is estimated from.
    first_fit_ms: float
    # Stored table of the residuals' quantiles per step ahead, for intervals.
    residuals: Sha | None = None


class PredictResult(_Result):
    rows: int
    series: int
    notes: list[str] = Field(default_factory=list)


WorkerResult = FunctionResult | FitResult | BacktestResult | PredictResult


class Handler(Protocol):
    """What runs one kind of request inside the worker.

    The worker gives it the request, a reader of the twin and the twin's artifacts,
    inside a process whose limits and budget the worker enforces; it gives back the
    result, with a table when the result has one. It fills neither `duration_ms` nor
    `logs`: the worker measures and collects those.
    """

    def __call__(
        self, request: Any, twin: Reader, artifacts: Artifacts
    ) -> WorkerResult | tuple[pa.Table, WorkerResult]: ...


# Module and name of the handler of each op. The worker imports a handler in the process
# that runs it, never in the process that listens, so listening costs no model library.
HANDLERS: dict[str, str] = {
    "function": "dta_runtime.functions.registered:run",
    "fit": "dta_runtime.models.ops:fit",
    "backtest": "dta_runtime.models.ops:backtest",
    "predict": "dta_runtime.models.ops:predict",
}
# Ops that may take minutes; they wait in a pool of their own.
BATCH_OPS = frozenset({"fit", "backtest"})


def runs_own_code(request: Any) -> bool:
    """Whether a request runs code the twin brought: a registered function, or a family
    given as an artifact. Such a run gets a fresh process with nothing but what it
    declared; a run of shipped code goes to a process that is kept."""
    family = getattr(request, "family", None)
    return request.op == "function" or (family is not None and family.artifact is not None)
