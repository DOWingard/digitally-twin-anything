"""Whether the twin keeps its promises, and what it does when it does not.

A check (`check`) measures the twin at a time of its clock: the error of each
predictive question's forecasts against what was then observed, over a window; how old
each question's data is; whether observed signals are stuck or biased. It opens,
steps and resolves incidents and keeps the twin's state, all in one version of the live
line at the check time. It never runs a model itself: it is handed a `Predictor`, and
what it decides to correct comes back as `CorrectionRequest`s for the caller to run,
whose results are recorded with `corrected`.

The twin's state (`state`): `unknown` before a check has completed, `healthy`,
`correcting` while an incident is open or waits for a re-model, `failed` while one has
failed. Incident kinds: `breach`, `stale`, `signal`, `reconcile`; statuses: `open`,
`remodel_requested`, `failed`, `resolved`. Signals: `suspect` (reason `stuck` or
`biased`) or `ok`. `standing` reads the state and the incidents not resolved at one
version: the interlock of the twin's actions judges by both. `unhealthy_since` names the
stretch the twin has not been healthy, which a control's fail-safe is sent once in.

Refusals are `FidelityError`s with a code: `fidelity.not_live` (a twin with no
recurring sync or no fidelity interval and window), `fidelity.unsupported` (an ontology
without the fidelity records), `fidelity.out_of_order` (a check before the latest one),
`fidelity.not_found`, `fidelity.bad_request`, `fidelity.conflict` (a record not in a
status the call applies to).
"""

from __future__ import annotations

from .check import check
from .errors import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    NOT_LIVE,
    OUT_OF_ORDER,
    UNSUPPORTED,
    FidelityError,
)
from .incidents import clear_signal, close_incident, corrected, open_incident, resolve, retry
from .outcome import INCIDENT_EVENT, REMODEL_EVENT, STATE_EVENT, UNKNOWN_EVENT
from .reads import (
    checks,
    errors,
    incident,
    incidents,
    observed_reading,
    signal,
    signals,
    standing,
    state,
    suspects,
    unhealthy_since,
    withhold,
)
from .records import incident_uid, signal_uid
from .values import CheckOutcome, CorrectionRequest, CorrectionResult, Freshness, Predictor

__all__ = [
    "BAD_REQUEST",
    "CONFLICT",
    "INCIDENT_EVENT",
    "NOT_FOUND",
    "NOT_LIVE",
    "OUT_OF_ORDER",
    "REMODEL_EVENT",
    "STATE_EVENT",
    "UNKNOWN_EVENT",
    "UNSUPPORTED",
    "CheckOutcome",
    "CorrectionRequest",
    "CorrectionResult",
    "FidelityError",
    "Freshness",
    "Predictor",
    "check",
    "checks",
    "clear_signal",
    "close_incident",
    "corrected",
    "errors",
    "incident",
    "incident_uid",
    "incidents",
    "observed_reading",
    "open_incident",
    "resolve",
    "retry",
    "signal",
    "signal_uid",
    "signals",
    "standing",
    "state",
    "suspects",
    "unhealthy_since",
    "withhold",
]
