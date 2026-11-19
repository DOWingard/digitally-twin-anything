"""The interlock: what a proposal and an approval may lead to, given the autonomy of the
control, how the action is bound, whether anything can make the call, the twin's
fidelity state and the incidents it has not resolved.

    autonomy     healthy     correcting, only    correcting, a stale   unknown    failed
                             breach or signal    or reconcile
                             incidents open      incident open, or
                                                 the target unknown
    advisory     proposed    proposed            proposed              proposed   proposed
    supervised   proposed; an approval executes                                   an approval
                                                                                  blocks
    autonomous   executes    executes            blocked               blocked    blocked

An autonomous action is executed iff the twin is healthy or correcting, no incident
it has not resolved is of kind `stale` (the data is too old to decide on) or
`reconcile` (an earlier command did not take effect), and the value of its control's
target on its object is not unknown. A breach or a suspect signal does not stop it
otherwise: the twin corrects those itself, and a suspect signal's value is then the
model's forecast; once that forecast runs out the value is unknown, and an action on it
is blocked like one in the `stale` column. `unknown` blocks because no check has
vouched for the twin yet.

An action bound `advisory`, or one with nothing to make the call (the executors add-on
is off), is advisory whatever its control says: nothing would execute it.

The incidents are handed in as plain data: the `kind` and `uid` of each one not
resolved, and for a signal incident whose value is unknown its `withheld` cell
(`{signal, property, object}`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .records import (
    ADVISORY,
    AUTONOMOUS,
    CORRECTING,
    EXECUTOR,
    FAILED_STATE,
    HEALTHY,
    RECONCILE_KIND,
    STALE_KIND,
    SUPERVISED,
    UNKNOWN,
)

# What the gate decides.
PROPOSE = "propose"
EXECUTE = "execute"
BLOCK = "block"

# The states an autonomous action may be executed in, and the kinds of incident that
# block it in them, with why.
ACTING_STATES = (HEALTHY, CORRECTING)
BLOCKING = {
    STALE_KIND: "the data is too old to decide on",
    RECONCILE_KIND: "an earlier command did not take effect",
}
_STATE_REASONS = {
    UNKNOWN: "no check has vouched for it yet",
    FAILED_STATE: "it could not put itself right",
}


def executable(action: Mapping[str, Any], has_executor: bool) -> bool:
    """Whether the twin could ever make this action's call itself."""
    return (
        has_executor
        and action.get("binding") == EXECUTOR
        and (action.get("autonomy") or ADVISORY) in (SUPERVISED, AUTONOMOUS)
    )


def withheld_target(
    action: Mapping[str, Any] | None,
    object_uid: str | None,
    incidents: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """The signal incidents whose unknown value is the action's control's target on
    `object_uid`. The target may name an abstract type; the object decides which of its
    types holds the value, so the property's name and the object are compared."""
    target = (action or {}).get("target")
    if not target or not object_uid:
        return []
    name = str(target).partition(".")[2]
    found = []
    for incident in incidents:
        cell = incident.get("withheld")
        if not isinstance(cell, Mapping) or cell.get("object") != object_uid:
            continue
        if str(cell.get("property") or "").partition(".")[2] == name:
            found.append(incident)
    return found


def blocking(
    incidents: Iterable[Mapping[str, Any]],
    action: Mapping[str, Any] | None = None,
    object_uid: str | None = None,
) -> list[Mapping[str, Any]]:
    """The incidents, of those not resolved, that block an autonomous action, in the
    order given: those of a blocking kind and, given the action and its object, the
    signal incidents that leave its target unknown."""
    incidents = list(incidents)
    withheld = {id(found) for found in withheld_target(action, object_uid, incidents)}
    return [found for found in incidents if found.get("kind") in BLOCKING or id(found) in withheld]


def on_proposal(
    action: Mapping[str, Any],
    *,
    has_executor: bool,
    state: str,
    incidents: Iterable[Mapping[str, Any]],
    object_uid: str | None = None,
) -> str:
    if not executable(action, has_executor) or action.get("autonomy") == SUPERVISED:
        return PROPOSE
    acting = state in ACTING_STATES and not blocking(incidents, action, object_uid)
    return EXECUTE if acting else BLOCK


def on_approval(state: str) -> str:
    return BLOCK if state == FAILED_STATE else EXECUTE


def blocked_reasons(
    state: str,
    *,
    approval: bool,
    incidents: Iterable[Mapping[str, Any]] = (),
    action: Mapping[str, Any] | None = None,
    object_uid: str | None = None,
) -> list[dict[str, str]]:
    """Why a proposal or an approval was blocked: the state when it blocks, otherwise
    one reason per incident that does, naming its kind and uid, and for an unknown
    target the signal."""
    if approval:
        message = f"the twin is {state}: an approved action is not executed while it is failed"
        return [{"member": "fidelity", "message": message}]
    if state not in ACTING_STATES:
        why = _STATE_REASONS.get(state, "it is in no state an autonomous action executes in")
        message = (
            f"the twin is {state}: {why}; an autonomous action executes only while it is "
            "healthy or correcting"
        )
        return [{"member": "fidelity", "message": message}]
    reasons = []
    for found in blocking(incidents, action, object_uid):
        if found.get("kind") in BLOCKING:
            why = BLOCKING[found["kind"]]
        else:
            cell = found["withheld"]
            why = (
                f"the value of {cell['property']} of {cell['object']}, the control's target, "
                f"is unknown (signal {cell['signal']})"
            )
        reasons.append(
            {
                "member": "fidelity",
                "message": f"the twin is {state} and its {found['kind']} incident "
                f"{found['uid']} is not resolved: {why}",
            }
        )
    return reasons
