"""The models of a twin: families that are fitted, walked forward through history and
asked to predict, and the rules by which one of them is selected.

This package and `selection` import no model library: the API imports them to
plan and judge. What fits and predicts (`ops`, `shipped`, `families`, `state`,
`series`, `scores`) imports the libraries and is imported only by the worker.
"""

from .errors import (
    BAD_FAMILY,
    BAD_FRAME,
    BAD_PARAMS,
    BAD_STATE,
    FAILED,
    NOT_INTERVENTIONAL,
    UNKNOWN_FAMILY,
    ModelError,
)
from .selection import Plan, Verdict, choose_refit, horizon_steps, judge, plan, select

__all__ = [
    "BAD_FAMILY",
    "BAD_FRAME",
    "BAD_PARAMS",
    "BAD_STATE",
    "FAILED",
    "NOT_INTERVENTIONAL",
    "UNKNOWN_FAMILY",
    "ModelError",
    "Plan",
    "Verdict",
    "choose_refit",
    "horizon_steps",
    "judge",
    "plan",
    "select",
]
