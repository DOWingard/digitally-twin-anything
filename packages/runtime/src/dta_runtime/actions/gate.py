"""The interlock: what a proposal and an approval may lead to, given the autonomy of the
control, how the action is bound, whether anything can make the call, and the twin's
fidelity state.

    autonomy        healthy                 correcting or unknown   failed
    advisory        proposed                proposed                proposed
    supervised      proposed; an approval   the same                an approval
                    executes                                        blocks
    autonomous      executes                blocked                 blocked

An action bound `advisory`, or one with nothing to make the call (the executors add-on
is off), is advisory whatever its control says: nothing would execute it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .records import (
    ADVISORY,
    AUTONOMOUS,
    EXECUTOR,
    FAILED_STATE,
    HEALTHY,
    SUPERVISED,
)

# What the gate decides.
PROPOSE = "propose"
EXECUTE = "execute"
BLOCK = "block"


def executable(action: Mapping[str, Any], has_executor: bool) -> bool:
    """Whether the twin could ever make this action's call itself."""
    return (
        has_executor
        and action.get("binding") == EXECUTOR
        and (action.get("autonomy") or ADVISORY) in (SUPERVISED, AUTONOMOUS)
    )


def on_proposal(action: Mapping[str, Any], *, has_executor: bool, state: str) -> str:
    if not executable(action, has_executor) or action.get("autonomy") == SUPERVISED:
        return PROPOSE
    return EXECUTE if state == HEALTHY else BLOCK


def on_approval(state: str) -> str:
    return BLOCK if state == FAILED_STATE else EXECUTE


def blocked_reason(state: str, *, approval: bool) -> dict[str, str]:
    if approval:
        message = f"the twin is {state}: an approved action is not executed while it is failed"
    else:
        message = f"the twin is {state}: an autonomous action executes only while it is healthy"
    return {"member": "fidelity", "message": message}
