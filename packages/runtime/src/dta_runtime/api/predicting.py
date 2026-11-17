"""The frame of a model and the one way a prediction is asked of the worker.

A model reads its target on the twin's grid (the sync interval) from where the twin's
data begins up to a version of a branch, with its features beside it. The same frame is
what a backtest walks through, what a fit learns from and what a prediction continues;
over the horizon of a prediction, the features that are known ahead are its drivers.

The predict route, the `forecast` function and the scenarios all predict through
`predict` here, so a model is asked the same way whoever asks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from dta_core.log import Log, current_trace_id, new_trace_id
from dta_core.store import VersionRef

from ..protocol import FrameRef, InterventionRef, PredictRequest
from . import registry
from .problems import ApiError

if TYPE_CHECKING:
    from .services import Services

log = Log("api.models")

PREDICT_BUDGET_S = 120.0
DEFAULT_LEVELS = (80.0, 95.0)


def trace_id() -> str:
    return current_trace_id() or new_trace_id()


def frame_of(
    services: Services,
    model: Mapping[str, Any],
    version: VersionRef,
    *,
    step: timedelta | None = None,
    ahead: int = 0,
    uids: Sequence[str] | None = None,
) -> FrameRef:
    """The frame of a model up to a version of a branch, `ahead` steps beyond it."""
    return FrameRef(
        target=model["target"],
        uids=list(uids) if uids else None,
        start=services.first_data().id,
        end=version.id,
        branch=version.branch,
        step=step if step is not None else services.interval(version),
        features=registry.features_of(model),
        ahead=ahead,
    )


def latest_backtest(
    services: Services, model_uid: str, version: VersionRef
) -> dict[str, Any] | None:
    """The newest evaluation of a model that scored it; None when there is none."""
    found = [
        row
        for row in registry.rows(
            services, registry.BACKTEST_TYPE, version, [("model", "=", model_uid)]
        )
        if row.get("origins")
    ]
    return max(found, key=lambda row: (row["started_at"], row["uid"])) if found else None


def predict(
    services: Services,
    model: Mapping[str, Any],
    version: VersionRef,
    *,
    horizon: int,
    levels: Sequence[float] = (),
    interventions: Sequence[InterventionRef] = (),
    uids: Sequence[str] | None = None,
    step: timedelta | None = None,
    budget_s: float = PREDICT_BUDGET_S,
    wait_until: float | None = None,
    known: VersionRef | None = None,
) -> tuple[dict[str, Any], pa.Table]:
    """What a model predicts for `horizon` steps after a version of a branch: the worker's
    result and the table (`unique_id`, `ds`, `mean`, and `lo_*`, `hi_*` per level).

    The history is read on the version's own branch, so a scenario's assumed facts are
    in it. Intervals a family does not give come from the residuals of the model's
    latest backtest as of `known` (the version the model's record was read at; the
    predicted-from version when absent), when it has one.
    """
    uid = model["uid"]
    if not model.get("fitted"):
        log.info("prediction refused: the model has no fitted state", model=uid)
        raise ApiError(
            f"{uid} has no fitted state; evaluate its specification first",
            code="model.not_fitted",
        )
    if interventions and not model.get("interventional"):
        log.info("prediction refused: interventions for a model that does not hold", model=uid)
        raise ApiError(
            f"{uid} does not say its predictions hold under interventions; "
            f"{len(interventions)} were given",
            code="model.not_interventional",
        )
    history = frame_of(services, model, version, step=step, uids=uids)
    # A model without features needs none of their future values.
    drivers = history.model_copy(update={"ahead": horizon}) if history.features else None
    evaluated = latest_backtest(services, uid, known or version)
    request = PredictRequest(
        budget_s=max(budget_s, 0.001),
        trace_id=trace_id(),
        family=registry.family_ref(model),
        params=registry.from_json(model.get("params"), {}),
        state=registry.bare(model["fitted"]),
        history=history,
        horizon=horizon,
        levels=list(levels),
        drivers=drivers,
        interventions=list(interventions),
        residuals=registry.bare(evaluated.get("residuals")) if evaluated else None,
    )
    result, table = services.compute.run(request, wait_until=wait_until)
    if table is None:
        log.error("a prediction came without its table", model=uid)
        raise ApiError(
            "the worker answered a prediction without a table", code="worker.unreachable"
        )
    log.info(
        "model predicted",
        model=uid,
        version=version.id,
        seq=version.seq,
        branch=version.branch,
        horizon=horizon,
        rows=table.num_rows,
        notes=result.get("notes"),
    )
    return result, table
