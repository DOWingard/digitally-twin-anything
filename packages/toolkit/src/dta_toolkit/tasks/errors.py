"""Errors and refusals raised by the task commands.

Two outcomes are kept apart because callers branch on them. A refusal means the
command ran and the answer is no (a held lease, a failed check): exit code 1.
A `TaskError` means the command could not be evaluated at all (an unknown task,
a malformed id, a damaged state file): exit code 2.
"""

from __future__ import annotations

from typing import Any

from dta_core.errors import CheckFailed, DtaError


class TaskError(DtaError):
    """The command could not be evaluated."""

    code = "tasks.error"


def reason(code: str, message: str, **detail: Any) -> dict[str, Any]:
    """One failed check, in the shape every refusal reports."""
    return {"code": code, "message": message, "detail": detail}


def refusal(code: str, message: str, **detail: Any) -> CheckFailed:
    return CheckFailed(message, code=code, detail=detail)
