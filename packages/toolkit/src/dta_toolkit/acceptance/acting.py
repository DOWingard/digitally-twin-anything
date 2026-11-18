"""Checks of what keeps a twin true and lets it act: its fidelity state, what its
scheduler asks for, its action types and control loops, and its notifications.

Each check only reads: it never starts a fidelity check, never proposes an action and
never calls an executor. What is compared comes from the manifest and from the API's
own answers, on the twin's clock; the wall is never read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest

from .live import is_live
from .readiness import API, Context
from .result import CheckResult, described, problem_of

log = Log("acceptance.acting")

FIDELITY = f"{API}/fidelity"
SCHEDULE = f"{API}/lifecycle/schedule"
JOBS = f"{API}/lifecycle/jobs"
ACTIONS = f"{API}/actions"
LOOPS = f"{API}/control/loops"
NOTIFICATIONS = f"{API}/lifecycle/notifications"

HEALTHY, UNKNOWN = "healthy", "unknown"
ACTIVE = "active"
EXECUTOR = "executor"
MANIFEST = "manifest"
LOG_KIND = "log"
JOBS_LISTED = 20
UNDER_WAY = ("queued", "running")

STATE_REMEDY = (
    "Read the incidents (dta fidelity incidents --status unresolved, or GET "
    "/api/v1/fidelity/incidents?status=unresolved) and what each went through (dta fidelity "
    "incident <id>). The twin corrects a breach itself; a request to re-model is answered by "
    "revising the model in the workspace and building again; a signal or reconcile incident is "
    "resolved by hand once its cause is mended (dta fidelity resolve or dta fidelity clear)."
)
SCHEDULE_REMEDY = (
    "Build again (dta build) so the twin's registries follow the manifest; a loop runs on the "
    "schedule only while it is enabled (dta control enable). Compare GET "
    "/api/v1/lifecycle/schedule with the manifest's sync, fidelity, functions and loops."
)
ACTIONS_REMEDY = (
    "Build again (dta build): it registers the manifest's action types and loops and starts "
    "the API anew when its executors changed. Turn the executors add-on off when no action is "
    "bound to an executor."
)
NOTIFICATIONS_REMEDY = (
    "Build again (dta build): the API sets up its notifications when it starts, and the build "
    "starts it anew when the notification changed."
)


def _not_applicable(ident: str, examined: str, reason: str) -> CheckResult:
    return CheckResult(ident, examined, True, {"applicable": False, "reason": reason})


def _read(
    ctx: Context, path: str, evidence: dict[str, Any], params: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The JSON body of a 200 answer, or None with the failing request in the evidence."""
    asked = dict(params) if params else None
    response = ctx.api.get(path, params=asked)
    if response.status_code != 200:
        evidence.setdefault("failing_requests", []).append(
            {"request": described("GET", path, asked), "response": problem_of(response)}
        )
        log.warn("an acceptance read was refused", path=path, status=response.status_code)
        return None
    evidence.setdefault("requests", []).append(described("GET", path, asked))
    body = response.json()
    return body if isinstance(body, dict) else {}


def _checks_fidelity(manifest: Manifest) -> bool:
    fidelity = manifest.fidelity
    return (
        is_live(manifest)
        and fidelity is not None
        and fidelity.interval is not None
        and fidelity.window is not None
    )


# -- fidelity.state -----------------------------------------------------------------


def _incident_text(incident: Mapping[str, Any]) -> str:
    return (
        f"{incident.get('uid')} ({incident.get('kind')} of {incident.get('subject')}, "
        f"{incident.get('status')})"
    )


def unknown_verdict(jobs: list[dict[str, Any]], scheduled: bool) -> tuple[bool, str]:
    """Whether a twin whose fidelity is still `unknown` passes, and what to say.

    `jobs` are the API's fidelity jobs, newest first; `scheduled` whether its schedule
    has the fidelity check. Unknown means no check has completed: that passes, with a
    warning, while the first check is under way or has not come due, and fails when
    the latest check failed or no check will ever be asked for.
    """
    if not scheduled:
        return False, "no fidelity check is on the twin's schedule, so none will ever run"
    latest = jobs[0] if jobs else None
    if latest is None:
        return True, (
            "no fidelity check has run yet; the scheduler asks for one at every boundary of "
            "the fidelity interval, the first once the sync run of its boundary has ended"
        )
    state = latest.get("state")
    if state in UNDER_WAY:
        return True, f"the first fidelity check is {state} (job {latest.get('id')})"
    if state == "failed":
        error = latest.get("error") or {}
        return False, (
            f"the latest fidelity check failed (job {latest.get('id')}): "
            f"{error.get('code')}: {error.get('message')}"
        )
    return True, f"a fidelity check ended {state} and the state is not written yet"


def fidelity_state(ctx: Context) -> CheckResult:
    ident = "fidelity.state"
    examined = (
        "the twin's fidelity state (GET /api/v1/fidelity) is healthy, or unknown while no check "
        "is due yet; correcting or failed name the incidents not resolved"
    )
    if not is_live(ctx.manifest):
        return _not_applicable(ident, examined, "contract.tier is static: no fidelity check runs")
    if not _checks_fidelity(ctx.manifest):
        return _not_applicable(ident, examined, "the manifest sets no fidelity interval and window")
    evidence: dict[str, Any] = {}
    found = _read(ctx, FIDELITY, evidence)
    if found is None:
        evidence["reason"] = "GET /api/v1/fidelity did not answer 200"
        return CheckResult(ident, examined, False, evidence, STATE_REMEDY)
    state = found.get("state")
    incidents = list(found.get("incidents") or [])
    evidence.update(
        {
            "state": state,
            "since": found.get("since"),
            "check": found.get("check"),
            "incidents": incidents,
            "suspect_signals": list(found.get("suspect_signals") or []),
        }
    )
    if state == HEALTHY:
        return CheckResult(ident, examined, True, evidence, STATE_REMEDY)
    if state == UNKNOWN:
        listing = _read(ctx, JOBS, evidence, {"kind": "fidelity", "limit": JOBS_LISTED})
        schedule = _read(ctx, SCHEDULE, evidence)
        if listing is None or schedule is None:
            evidence["reason"] = (
                "the state is unknown and the API's jobs or schedule cannot be read"
            )
            return CheckResult(ident, examined, False, evidence, STATE_REMEDY)
        jobs = list(listing.get("data") or [])
        scheduled = any(entry.get("kind") == "fidelity" for entry in schedule.get("entries") or [])
        evidence["fidelity_jobs"] = [
            {key: job.get(key) for key in ("id", "state", "idempotency_key", "error")}
            for job in jobs[:3]
        ]
        evidence["now"] = schedule.get("now")
        passed, said = unknown_verdict(jobs, scheduled)
        evidence["warning" if passed else "reason"] = said
        if passed:
            log.warn("the twin's fidelity is not known yet", note=said)
        else:
            log.warn("fidelity state check failed", reason=said)
        return CheckResult(ident, examined, passed, evidence, STATE_REMEDY)
    named = "; ".join(_incident_text(incident) for incident in incidents) or "none listed"
    evidence["reason"] = f"the twin is {state}: incidents not resolved: {named}"
    log.warn("fidelity state check failed", state=state, incidents=len(incidents))
    return CheckResult(ident, examined, False, evidence, STATE_REMEDY)


# -- fidelity.schedule ---------------------------------------------------------------


def expected_entries(
    manifest: Manifest,
    *,
    loops: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    executor_configured: bool,
) -> dict[str, str]:
    """Entry id -> why the schedule must hold it, from the manifest and the twin's records.

    A live twin's sync and fidelity check; the reconciliation of executed actions when
    the API makes calls and an active action is bound to an executor; every enabled,
    active loop; every function the manifest triggers on an interval. A static twin
    has none.
    """
    if not is_live(manifest):
        return {}
    expected: dict[str, str] = {}
    if manifest.sync is not None and manifest.sync.interval is not None:
        expected["sync"] = "sync.interval"
    if _checks_fidelity(manifest):
        expected["fidelity"] = "fidelity.interval"
    if executor_configured and any(
        action.get("binding") == EXECUTOR and action.get("status") == ACTIVE for action in actions
    ):
        expected["reconcile"] = "an active action is bound to an executor"
    for loop in loops:
        if loop.get("enabled") and loop.get("status") == ACTIVE:
            expected[f"loop:{loop.get('id')}"] = "the loop is enabled"
    for function_id, function in sorted((manifest.functions or {}).items()):
        if function.trigger == "interval":
            expected[f"function:{function_id}"] = f"functions.{function_id}.trigger is interval"
    return expected


def fidelity_schedule(ctx: Context) -> CheckResult:
    ident = "fidelity.schedule"
    examined = (
        "GET /api/v1/lifecycle/schedule lists the sync, the fidelity check, the reconciliation of "
        "executed actions, one entry per enabled loop and per function triggered on an "
        "interval, and nothing else"
    )
    evidence: dict[str, Any] = {}
    schedule = _read(ctx, SCHEDULE, evidence)
    listing = _read(ctx, ACTIONS, evidence)
    loops = _read(ctx, LOOPS, evidence)
    if schedule is None or listing is None or loops is None:
        evidence["reason"] = "the schedule, the action types or the loops cannot be read"
        return CheckResult(ident, examined, False, evidence, SCHEDULE_REMEDY)
    entries = list(schedule.get("entries") or [])
    expected = expected_entries(
        ctx.manifest,
        loops=list(loops.get("data") or []),
        actions=list(listing.get("data") or []),
        executor_configured=bool(listing.get("executor_configured")),
    )
    listed = [str(entry.get("id")) for entry in entries]
    missing = sorted(set(expected) - set(listed))
    unexpected = sorted(set(listed) - set(expected))
    repeated = sorted({ident for ident in listed if listed.count(ident) > 1})
    evidence.update(
        {
            "now": schedule.get("now"),
            "entries": [
                {key: entry.get(key) for key in ("id", "kind", "interval", "path")}
                for entry in entries
            ],
            "expected": expected,
            "missing": missing,
            "unexpected": unexpected,
        }
    )
    reasons = []
    if missing:
        reasons.append(f"missing: {', '.join(missing)}")
    if unexpected:
        reasons.append(f"not expected: {', '.join(unexpected)}")
    if repeated:
        reasons.append(f"listed more than once: {', '.join(repeated)}")
    if reasons:
        evidence["reason"] = "; ".join(reasons)
        log.warn("schedule check failed", reason=evidence["reason"])
    if not is_live(ctx.manifest):
        evidence["note"] = "a static twin's schedule is empty"
    return CheckResult(ident, examined, not reasons, evidence, SCHEDULE_REMEDY)


# -- actions.registered ----------------------------------------------------------------


def registry_problems(
    manifest: Manifest,
    *,
    actions: list[dict[str, Any]],
    loops: list[dict[str, Any]],
    executor_configured: bool,
) -> dict[str, list[str]]:
    """What differs between the manifest's actions, loops and controls and the twin's.

    A record the manifest no longer declares that a registration has disabled is
    orphaned and listed apart: it stays in the twin's history and cannot act. One
    still active means the registry was not brought in line with this manifest.
    """
    problems: dict[str, list[str]] = {}

    def note(kind: str, text: str) -> None:
        problems.setdefault(kind, []).append(text)

    by_kind = {
        "action": {a.get("id"): a for a in actions},
        "loop": {lp.get("id"): lp for lp in loops},
    }
    declared = {"action": set(manifest.actions or {}), "loop": set(manifest.control_loops or {})}
    for kind in ("action", "loop"):
        for ident in sorted(declared[kind]):
            found = by_kind[kind].get(ident)
            if found is None:
                note("unregistered", f"{kind} {ident} is not registered")
            elif found.get("status") != ACTIVE:
                note("inactive", f"{kind} {ident} is {found.get('status')}")
        for ident, found in sorted(by_kind[kind].items(), key=lambda item: str(item[0])):
            if ident in declared[kind] or found.get("origin") != MANIFEST:
                continue
            if found.get("status") == ACTIVE:
                note("orphaned", f"{kind} {ident} is no longer declared and still active")
    needs = any(action.binding == EXECUTOR for action in (manifest.actions or {}).values())
    addon = bool(manifest.contract is not None and manifest.contract.addons.executors)
    wanted = addon and needs
    if executor_configured != wanted:
        if wanted:
            note("executor", "an action is bound to an executor and the API has none configured")
        elif addon:
            note("executor", "the executors add-on is on and no action is bound to an executor")
        else:
            note("executor", "the API has an executor configured and the add-on is off")
    controls = manifest.contract.controls if manifest.contract is not None else {}
    for control_id, control in sorted((controls or {}).items()):
        if not control.action:
            continue
        found = by_kind["action"].get(control.action)
        if found is None:
            note("control", f"control {control_id}: its action {control.action} is not registered")
        elif found.get("control") != control_id:
            note(
                "control",
                f"control {control_id}: action {control.action} carries out "
                f"{found.get('control')!r}, not {control_id!r}",
            )
        elif control.autonomy is not None and found.get("autonomy") != control.autonomy.value:
            note(
                "control",
                f"control {control_id}: action {control.action} is registered "
                f"{found.get('autonomy')}, the contract says {control.autonomy.value}",
            )
    return problems


def actions_registered(ctx: Context) -> CheckResult:
    ident = "actions.registered"
    examined = (
        "every action type and control loop of the manifest is registered and active and none "
        "is orphaned; an executor is configured exactly when the executors add-on is on and an "
        "action is bound to one; every control's action is registered and carries it"
    )
    evidence: dict[str, Any] = {}
    listing = _read(ctx, ACTIONS, evidence)
    loops = _read(ctx, LOOPS, evidence)
    if listing is None or loops is None:
        evidence["reason"] = "the action types or the loops cannot be read"
        return CheckResult(ident, examined, False, evidence, ACTIONS_REMEDY)
    actions = list(listing.get("data") or [])
    loop_rows = list(loops.get("data") or [])
    configured = bool(listing.get("executor_configured"))
    problems = registry_problems(
        ctx.manifest, actions=actions, loops=loop_rows, executor_configured=configured
    )
    left = {
        "actions": sorted(
            str(a.get("id"))
            for a in actions
            if a.get("id") not in (ctx.manifest.actions or {}) and a.get("origin") == MANIFEST
        ),
        "loops": sorted(
            str(lp.get("id"))
            for lp in loop_rows
            if lp.get("id") not in (ctx.manifest.control_loops or {})
            and lp.get("origin") == MANIFEST
        ),
    }
    evidence.update(
        {
            "actions": [
                {key: a.get(key) for key in ("id", "control", "binding", "autonomy", "status")}
                for a in actions
            ],
            "loops": [
                {key: lp.get(key) for key in ("id", "controls", "status", "enabled")}
                for lp in loop_rows
            ],
            "executor_configured": configured,
            "no_longer_declared": left,
            "problems": problems,
        }
    )
    if not ctx.manifest.actions and not ctx.manifest.control_loops and not actions:
        evidence["note"] = "the manifest declares no action and no loop"
    passed = not problems
    if not passed:
        evidence["reason"] = "; ".join(text for found in problems.values() for text in found)
        log.warn("actions check failed", reason=evidence["reason"])
    return CheckResult(ident, examined, passed, evidence, ACTIONS_REMEDY)


# -- notifications.configured ----------------------------------------------------------


def expected_kind(manifest: Manifest) -> str:
    """The kind the API notifies by: the manifest's, `log` when it names none."""
    contract = manifest.contract
    notification = contract.notification if contract is not None else None
    if notification is None or notification.kind is None:
        return LOG_KIND
    return notification.kind


def notifications_configured(ctx: Context) -> CheckResult:
    ident = "notifications.configured"
    examined = (
        "GET /api/v1/lifecycle/notifications answers and the API notifies by the kind the "
        "manifest's contract.notification names"
    )
    evidence: dict[str, Any] = {}
    listing = _read(ctx, NOTIFICATIONS, evidence)
    if listing is None:
        evidence["reason"] = "the notifications cannot be listed"
        return CheckResult(ident, examined, False, evidence, NOTIFICATIONS_REMEDY)
    kind = listing.get("kind")
    wanted = expected_kind(ctx.manifest)
    sent = list(listing.get("data") or [])
    statuses: dict[str, int] = {}
    for notice in sent:
        status = str(notice.get("status"))
        statuses[status] = statuses.get(status, 0) + 1
    evidence.update({"kind": kind, "expected": wanted, "listed": len(sent), "statuses": statuses})
    passed = kind == wanted
    if not passed:
        evidence["reason"] = f"the API notifies by {kind}, the manifest asks for {wanted}"
        log.warn("notifications check failed", reason=evidence["reason"])
    elif statuses.get("failed"):
        evidence["warning"] = f"{statuses['failed']} notification(s) could not be delivered"
    return CheckResult(ident, examined, passed, evidence, NOTIFICATIONS_REMEDY)
