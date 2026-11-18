"""The part of a twin that acts on the real system.

A twin may be given controls: a property of the real system it may set, within bounds,
at a stated autonomy. An action is the typed step that sets one (or that only advises),
either as a recommendation to a person or as a call to an external system. Action types
and control loops come from the manifest (`rows` gives their records); every proposal
becomes a `TwinActionRecord` on the live line, whatever becomes of it.

    rows, known         the records of the manifest's action types and loops
    types               the action types, with parameters, bounds, autonomy, binding
    propose             judge a proposal at the head, record it, send it if the gate lets it
    approve, reject     decide on a proposed record
    reconcile           compare executed actions with what is observed afterwards
    records, record     read the records
    loops, control_loop, enable, disable, run_loop
                        the control loops and their runs

The caller hands in the twin's fidelity state (`unknown`, `healthy`, `correcting`,
`failed`) and, for a proposal or a loop's run, the `kind` and `uid` of every incident it
has not resolved, both read at one version; the clock's `now`, what makes external
calls (an `Executor`, such as the `HttpExecutor` built from the manifest; None when the
executors add-on is off) and, for a loop, what runs its policy (a `Policy`). Nothing
here reads the wall clock.

The interlock: an autonomous action is executed while the twin is `healthy` or
`correcting` and none of its unresolved incidents is `stale` or `reconcile`, and is
`blocked` otherwise; a supervised one waits for an approval, which is `blocked` only
while the twin is `failed`; an advisory one is only recorded.

Uids: `twin_action:<id>` for an action type, `twin_control_loop:<id>` for a loop and
`twin_action_record:<id>` for a record, whose id is the first 32 hexadecimal characters
of the SHA-256 of the caller's key when one is given, a random one otherwise. The id is
the `Idempotency-Key` of the record's call.

Every refusal and failure is an `ActionError`. The statuses the API gives the codes:

    action.rejected           422   the proposal cannot be carried out as asked; it is
                                    recorded as rejected; `detail.reasons` lists every
                                    reason, `detail.record` names the record (None when
                                    the action type does not exist)
    action.key_reused         409   the key was used before for another proposal
    action.bad_state          500   the fidelity state handed in does not exist, or an
                                    incident handed in is not {kind, uid} of a known kind
    action.record_not_found   404   no record of that uid
    action.not_pending        409   the record is not awaiting a decision
    action.not_executable     409   the twin cannot execute the record: advisory, its
                                    type disabled, or nothing to make the call
    action.executor_invalid   500   an executor's templates use a variable it does not list
    action.loop_not_found     404   no control loop of that id
    action.loop_orphaned      409   the loop is no longer declared and cannot be enabled
    action.loop_disabled      409   the loop is not enabled
    action.policy_failed      502   the loop's policy raised; the run is recorded as failed
    action.bad_policy_answer  502   the policy's answer cannot be used whole; nothing was
                                    proposed and the run is recorded as failed

A variable an executor lists and the environment lacks is a `config.missing_env` from
`HttpExecutor.from_manifest`. Errors of the store (`store.*`) pass through.
"""

from .catalog import types
from .errors import (
    BAD_POLICY_ANSWER,
    BAD_STATE,
    EXECUTOR_INVALID,
    KEY_REUSED,
    LOOP_DISABLED,
    LOOP_NOT_FOUND,
    LOOP_ORPHANED,
    NOT_EXECUTABLE,
    NOT_PENDING,
    POLICY_FAILED,
    RECORD_NOT_FOUND,
    REJECTED,
    ActionError,
)
from .executor import Execution, Executor, HttpExecutor
from .loops import Policy, control_loop, disable, enable, loops, run_loop
from .proposals import approve, propose, record, records, reject
from .reconciliation import Reconciled, reconcile
from .registration import Registration, known, rows

__all__ = [
    "BAD_POLICY_ANSWER",
    "BAD_STATE",
    "EXECUTOR_INVALID",
    "KEY_REUSED",
    "LOOP_DISABLED",
    "LOOP_NOT_FOUND",
    "LOOP_ORPHANED",
    "NOT_EXECUTABLE",
    "NOT_PENDING",
    "POLICY_FAILED",
    "RECORD_NOT_FOUND",
    "REJECTED",
    "ActionError",
    "Execution",
    "Executor",
    "HttpExecutor",
    "Policy",
    "Reconciled",
    "Registration",
    "approve",
    "control_loop",
    "disable",
    "enable",
    "known",
    "loops",
    "propose",
    "reconcile",
    "record",
    "records",
    "reject",
    "rows",
    "run_loop",
    "types",
]
