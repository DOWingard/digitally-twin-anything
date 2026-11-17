"""Why a function could not be run, or what it gave back could not be used."""

from __future__ import annotations

from typing import Any

from dta_core.errors import DtaError

PARAM_UNKNOWN = "function.param_unknown"
PARAM_FIXED = "function.param_fixed"
PARAM_MISSING = "function.param_missing"
BAD_PARAM = "function.bad_param"
BAD_UNIT = "function.bad_unit"
TIMEOUT = "function.timeout"
UNAVAILABLE = "function.unavailable"
UNKNOWN_ENTRY = "function.unknown_entry"
BAD_ENTRY = "function.bad_entry"
BAD_RESULT = "function.bad_result"


class FunctionError(DtaError):
    """A function was refused or failed; `detail` names the function and, when one is
    at fault, the parameter."""

    code = "function.error"


def refused(
    code: str, message: str, *, function: str, parameter: str | None = None, **detail: Any
) -> FunctionError:
    found: dict[str, Any] = {"function": function}
    if parameter is not None:
        found["parameter"] = parameter
    found.update(detail)
    return FunctionError(message, code=code, detail=found)
