"""What is asked for when a scenario is made, what predicts it, and the name it gets.

A scenario is named by what it is: the same request at the same base version
is the same scenario, whoever asks and whenever. Nothing random and nothing of
the wall clock goes into the name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from dta_core.store import VersionRef
from dta_core.store.model import iso_utc
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ..protocol.worker import InterventionRef

if TYPE_CHECKING:
    import pyarrow as pa

ID_LENGTH = 12
# The longest horizon, in steps of the grid: one version is written per step.
MAX_STEPS = 2000
_DURATION = TypeAdapter(timedelta)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    # The assumed changes. A `start` is always given: the caller fills an absent one.
    interventions: list[InterventionRef] = Field(default_factory=list)
    # How far the scenario runs beyond its base.
    horizon: timedelta = Field(gt=timedelta(0))
    # Uids of `TwinModel` records, one per predicted property.
    models: list[str] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _no_repeats(cls, value: list[str]) -> list[str]:
        repeated = sorted({uid for uid in value if value.count(uid) > 1})
        if repeated:
            raise ValueError(f"a model is named once: {', '.join(repeated)} repeated")
        return value


class Predictor(Protocol):
    """What predicts for a scenario: the worker behind the API, or a test's own."""

    def __call__(
        self,
        model: Mapping[str, Any],
        *,
        base: VersionRef,
        branch: str,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        """`unique_id` (string), `ds` (timestamp us UTC) and `mean` (float64): `horizon`
        rows per series, at the base time plus one to `horizon` steps.

        `model` is the `TwinModel` record as read at the base version, `branch` the
        scenario's branch (it holds the assumed facts), `deadline_s` the seconds left
        for this call. A refusal is a `DtaError`; it becomes the run's failure.
        """
        ...


def duration_text(value: timedelta) -> str:
    """ISO 8601, as the record keeps the horizon."""
    return _DURATION.dump_python(value, mode="json")


def duration_of(text: str) -> timedelta:
    return _DURATION.validate_python(text)


def _plain(value: Any) -> Any:
    """Values JSON has no form for, as text: times in ISO 8601 UTC."""
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no JSON form")


def canonical(document: Any) -> str:
    """Sorted keys, no spaces, times as ISO 8601 UTC. Raises `ValueError` or `TypeError`
    for what JSON cannot hold (a number that is not finite, an object of the caller's)."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_plain
    )


def intervention_documents(interventions: Sequence[InterventionRef]) -> list[dict[str, Any]]:
    """The interventions in one order, whatever order they were given in."""
    documents = [
        {
            "target": found.target,
            "uid": found.uid,
            "value": found.value,
            "start": iso_utc(found.start),
            "end": None if found.end is None else iso_utc(found.end),
        }
        for found in interventions
    ]
    return sorted(documents, key=lambda found: (found["target"], found["uid"], found["start"]))


def interventions_of(text: str) -> list[InterventionRef]:
    """The interventions a record keeps, as they are handed to a predictor."""
    return [InterventionRef(**found) for found in json.loads(text)]


def scenario_id(request: ScenarioRequest, at: VersionRef, step: timedelta) -> str:
    """The first characters of the SHA-256 of what defines the scenario."""
    document = {
        "name": request.name,
        "base": at.id,
        "interventions": intervention_documents(request.interventions),
        "horizon": request.horizon.total_seconds(),
        "models": sorted(request.models),
        "step": step.total_seconds(),
    }
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()[:ID_LENGTH]
