"""Scenarios as the API runs them: the predictor that asks the worker, the job that
fills a scenario's branch, and what is done about a run that never ended.

A scenario is marked `running` in the graph while it is run. A process that dies in the
middle leaves that mark behind, and such a scenario could be neither run nor deleted;
so a scenario that is `running` while no job is running it is marked failed.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.store import VersionRef

from .. import scenarios
from ..protocol import DS, MEAN, UNIQUE_ID, InterventionRef
from ..scenarios import records
from . import predicting, registry
from .jobs import Outcome, Work

if TYPE_CHECKING:
    from .services import Services

log = Log("api.scenarios")

SCENARIO_KIND = "scenario"
RUN_BUDGET_S = 600.0
INTERRUPTED = "scenario.interrupted"


def scenario_uid(text: str) -> str:
    """The uid of a scenario named by its uid or by its id."""
    return text if text.startswith(f"{records.SCOPE}:") else records.scenario_uid(text)


def document(record: Mapping[str, Any]) -> dict[str, Any]:
    """A scenario's record as it is handed out: its interventions as JSON values."""
    found = dict(record)
    found["interventions"] = registry.from_json(record.get("interventions"), [])
    found["models"] = list(record.get("models") or [])
    return found


def sweep_interrupted(services: Services) -> list[str]:
    """Mark failed every scenario that is `running` while no job runs it; their uids."""
    head = services.store.head()
    running = registry.rows(
        services, registry.SCENARIO_TYPE, head, [("status", "=", records.RUNNING)]
    )
    swept = []
    for row in running:
        uid = row["uid"]
        if uid in services.running_scenarios:
            continue
        log.warn(
            "a scenario was left running by a process that ended; it is marked failed",
            scenario=uid,
            branch=row.get("branch"),
        )
        records.set_status(
            services.twin,
            uid,
            records.FAILED,
            error=f"{INTERRUPTED}: the run was interrupted before it finished; run it again",
        )
        swept.append(uid)
    return swept


def _predictor(services: Services):
    """What predicts for a scenario: the worker, asked as every prediction is."""

    def predict(
        model: Mapping[str, Any],
        *,
        base: VersionRef,
        branch: str,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        # The scenario's branch holds what is assumed from the base time on; its head is
        # still at the base time, since a run writes nothing before every model answered.
        head = services.store.head(branch)
        _, table = predicting.predict(
            services,
            model,
            head,
            horizon=horizon,
            # A family that does not hold under interventions is never given any; the
            # scenario was refused already if one of them changes a control.
            interventions=list(interventions) if model.get("interventional") else [],
            step=step,
            budget_s=deadline_s,
            wait_until=time.monotonic() + deadline_s,
            known=services.store.head(),
        )
        return table.select([UNIQUE_ID, DS, MEAN])

    return predict


def run_work(services: Services, uid: str, budget_s: float) -> Work:
    def work() -> Outcome:
        sweep_interrupted(services)
        services.running_scenarios.add(uid)
        try:
            record = scenarios.run(
                services.twin, uid, predict=_predictor(services), budget_s=budget_s
            )
        finally:
            services.running_scenarios.discard(uid)
        return Outcome(result=registry.finite(document(record)))

    return work
