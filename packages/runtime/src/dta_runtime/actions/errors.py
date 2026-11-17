"""Why an action was refused or could not be carried out, as stable codes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dta_core.errors import DtaError

REJECTED = "action.rejected"
KEY_REUSED = "action.key_reused"
BAD_STATE = "action.bad_state"
RECORD_NOT_FOUND = "action.record_not_found"
NOT_PENDING = "action.not_pending"
NOT_EXECUTABLE = "action.not_executable"
EXECUTOR_INVALID = "action.executor_invalid"
LOOP_NOT_FOUND = "action.loop_not_found"
LOOP_ORPHANED = "action.loop_orphaned"
LOOP_DISABLED = "action.loop_disabled"
POLICY_FAILED = "action.policy_failed"
BAD_POLICY_ANSWER = "action.bad_policy_answer"


class ActionError(DtaError):
    """An action, a record of one or a control loop was refused or failed.

    An `action.rejected` carries `detail["reasons"]` (every reason found, each with
    the `member` of the proposal it is about and a `message`) and `detail["record"]`,
    the uid of the record that keeps the refusal, or None when none could be kept.
    """

    code = "action.error"


def refused(code: str, message: str, **detail: Any) -> ActionError:
    return ActionError(message, code=code, detail=detail)


def reason(member: str, message: str) -> dict[str, str]:
    return {"member": member, "message": message}


def rejected(
    reasons: Sequence[dict[str, str]], *, record: str | None, **detail: Any
) -> ActionError:
    """One refusal naming every reason found."""
    first = reasons[0]
    more = "" if len(reasons) == 1 else f" (and {len(reasons) - 1} more)"
    return ActionError(
        f"{first['member']}: {first['message']}{more}",
        code=REJECTED,
        detail={"reasons": list(reasons), "record": record, **detail},
    )
