"""What the fidelity package is handed and what it hands back."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from dta_core.store import VersionRef

from ..protocol.worker import InterventionRef

if TYPE_CHECKING:
    import pyarrow as pa

RECALIBRATE = "recalibrate"
RESELECT = "reselect"
Rung = Literal["recalibrate", "reselect"]


class Predictor(Protocol):
    """What predicts a question's origin: the worker behind the API, or a test's own."""

    def __call__(
        self,
        model: Mapping[str, Any],
        *,
        origin: VersionRef,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        """`unique_id` (string), `ds` (timestamp us UTC), `mean` (float64), and `lo_80`,
        `hi_80` when the model gives them: `horizon` rows per series at the origin's time
        plus one to `horizon` steps, predicted from the history up to `origin`.

        `model` is the `TwinModel` record as the check reads it. A refusal is a
        `DtaError`: the question is then not scored at that check, with the reason.
        """
        ...


@dataclass(frozen=True)
class Freshness:
    """How old the data of one question is, as the caller measured it at the check."""

    # None: it cannot be measured now (the clock or the sources say nothing).
    fresh: bool | None
    age_s: float | None
    staleness_s: float | None
    source: str | None


@dataclass(frozen=True)
class CorrectionRequest:
    """A correction the caller is to run after a check: one job, under `key`."""

    # Uid of the breach incident; for a warning, `warning-<question>-<check time>`.
    incident: str
    question: str
    # Id of the model specification.
    spec: str
    rung: Rung
    cycle: int
    attempt: int
    # correction:<incident>:<cycle>:<rung>:<attempt>
    key: str
    # A pre-emptive refit after a warning: no incident is open.
    warning: bool

    def document(self) -> dict[str, Any]:
        return {
            "incident": self.incident,
            "question": self.question,
            "spec": self.spec,
            "rung": self.rung,
            "cycle": self.cycle,
            "attempt": self.attempt,
            "key": self.key,
            "warning": self.warning,
        }


def correction_key(incident: str, cycle: int, rung: str, attempt: int) -> str:
    return f"correction:{incident}:{cycle}:{rung}:{attempt}"


@dataclass(frozen=True)
class CorrectionResult:
    """What the caller reports back once a correction has run."""

    accepted: bool
    # Uid of the model now active.
    model: str | None
    # End of the data the model now active was fitted on.
    fitted_to: datetime | None
    # Its error in the recent evaluation.
    error: float | None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckOutcome:
    """Everything one check decided.

    `events` holds one entry per thing to notify, in order: `type` (a CloudEvents type
    of the twin), `subject` (the uid it is about) and `data`. `replayed` is true when
    the check had been made before at the same time and nothing was written.
    """

    check: dict[str, Any]
    state_before: str
    state_after: str
    opened: list[dict[str, Any]]
    changed: list[dict[str, Any]]
    resolved: list[dict[str, Any]]
    flagged: list[dict[str, Any]]
    cleared: list[dict[str, Any]]
    corrections: list[CorrectionRequest]
    events: list[dict[str, Any]]
    version: VersionRef
    replayed: bool = False
