"""What a fit, a backtest or a prediction is refused for, as stable codes.

The worker passes a handler's code through; the API answers with the status
each code stands for (stated in `ops`).
"""

from __future__ import annotations

from typing import Any

from dta_core.errors import DtaError

BAD_PARAMS = "model.bad_params"
BAD_FRAME = "model.bad_frame"
UNKNOWN_FAMILY = "model.unknown_family"
BAD_FAMILY = "model.bad_family"
BAD_STATE = "model.bad_state"
NOT_INTERVENTIONAL = "model.not_interventional"
FAILED = "model.failed"


class ModelError(DtaError):
    code = FAILED


def bad_params(message: str, **detail: Any) -> ModelError:
    return ModelError(message, code=BAD_PARAMS, detail=detail)


def bad_frame(message: str, **detail: Any) -> ModelError:
    return ModelError(message, code=BAD_FRAME, detail=detail)


def bad_state(message: str, **detail: Any) -> ModelError:
    return ModelError(message, code=BAD_STATE, detail=detail)


def bad_family(message: str, **detail: Any) -> ModelError:
    return ModelError(message, code=BAD_FAMILY, detail=detail)
