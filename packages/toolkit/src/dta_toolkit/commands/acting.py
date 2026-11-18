"""`dta fidelity`, `dta actions`, `dta control` and `dta notifications`: whether the twin
keeps what it promised, what it does to the real system, its control loops and what it
told its owner, asked of its running API.

Each command reads the workspace's manifest and `.env`, talks to the API on the host
with the workspace's token and prints what the API answered, as a table or with
`--json` as one document. A refusal exits 1 with the API's own code and message. A
command that starts a job (a check, a correction, a loop's run) waits for it as the
other commands do, and exits 1 when the job failed. No value of `.env` is printed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log

from ..acceptance import TwinApi
from ..output import command, emit
from . import computeops
from .common import scrub_values
from .compute import _api, _argument, table
from .syncjobs import ApiRefused, read_json, refused, wait_job

log = Log("commands.acting")

API = "/api/v1"
FIDELITY = f"{API}/fidelity"
ACTIONS = f"{API}/actions"
LOOPS = f"{API}/control/loops"
NOTIFICATIONS = f"{API}/lifecycle/notifications"

JOB_WAIT_S = 900.0
JOB_FAILED = "job.failed"
BAD_PARAM = "actions.bad_param"
# The keys of a refusal's problem document worth keeping beside its code and message.
_PROBLEM_EXTRAS = ("errors", "record", "incident", "signal", "loop")
DEFAULT_SOURCE = "dta"

ById = Annotated[str, typer.Option("--by", help="Who takes the step, as it is recorded.")]
Note = Annotated[str | None, typer.Option("--note", help="Why, as it is recorded.")]
Wait = Annotated[
    float, typer.Option("--timeout", min=1.0, help="Seconds to wait for the job to end.")
]


def _segment(text: str) -> str:
    """A uid as one path segment; its `:` is kept as the API writes it."""
    return quote(text, safe=":")


def _refusal(response: httpx.Response, request: str) -> ApiRefused:
    """The API's refusal with what its problem document adds: every reason, the record."""
    error = refused(response, request)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        for key in _PROBLEM_EXTRAS:
            if key in body:
                error.detail[key] = body[key]
    log.warn("the API refused", request=request, code=error.code, status=response.status_code)
    return error


def _call(
    api: TwinApi,
    method: str,
    path: str,
    request: str,
    *,
    ok: Sequence[int] = (200,),
    body: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    key: str | None = None,
) -> tuple[dict[str, Any], int]:
    """The JSON answer of one request and its status; anything else is the API's refusal."""
    started = time.perf_counter()
    headers = {"Idempotency-Key": key} if key else None
    response = api.request(
        method,
        path,
        json=dict(body) if body is not None else None,
        params=dict(params) if params else None,
        headers=headers,
    )
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if response.status_code not in ok:
        raise _refusal(response, request)
    log.debug("the API answered", request=request, status=response.status_code, ms=elapsed)
    document = response.json()
    return (document if isinstance(document, dict) else {}), response.status_code


def _param(text: str) -> tuple[str, Any]:
    if "=" not in text or not text.partition("=")[0].strip():
        log.warn("a parameter is not name=value", given=text)
        raise DtaError(
            f"--param takes name=value, got {text!r}", code=BAD_PARAM, detail={"param": text}
        )
    return _argument(text)


def _step_body(by: str, note: str | None) -> dict[str, Any]:
    return {"by": by, "note": note} if note else {"by": by}


def _run_job(
    api: TwinApi,
    path: str,
    body: Mapping[str, Any],
    *,
    what: str,
    follow: str,
    limit_s: float,
    key: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Start a job, wait for its end; a job that failed exits 1 with its own error."""
    job, created = computeops.start_job(api, path, body, key)
    ended = wait_job(
        api,
        str(job.get("id")),
        limit_s=limit_s,
        what=what,
        code=f"{what}.wait_timeout",
        follow=follow,
    )
    if ended.get("state") != "succeeded":
        error = ended.get("error") or {}
        log.error(f"the {what} job failed", job=ended.get("id"), code=error.get("code"))
        raise CheckFailed(
            f"the {what} job failed: {error.get('code')}: {error.get('message') or 'no message'}",
            code=str(error.get("code") or JOB_FAILED),
            detail={"job": ended},
        )
    return ended, created


def _say(ctx: typer.Context, result: dict[str, Any], text: str, secrets: dict[str, str]) -> None:
    emit(ctx, scrub_values(result, secrets), scrub_values(text, secrets))


def _brief(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True) if value not in (None, {}, []) else "-"


# -- dta fidelity ----------------------------------------------------------------------

_QUESTION_COLUMNS = [
    "id",
    "model",
    "error",
    "window_error",
    "window_n",
    "tolerance",
    "metric",
    "breach",
    "reason",
]
_INCIDENT_COLUMNS = ["uid", "kind", "subject", "status", "rung", "attempt", "cycle", "opened_at"]
_SIGNAL_COLUMNS = [
    "uid",
    "property",
    "object",
    "status",
    "reason",
    "since",
    "value_at_flag",
    "reading",
    "reading_at",
]


def _status_text(found: Mapping[str, Any]) -> str:
    check = found.get("check") or {}
    lines = [
        f"State     {found.get('state')} since {found.get('since') or '-'}",
        f"Check     {check.get('uid') or 'none yet'}",
    ]
    if found.get("questions"):
        lines += ["", "Questions", table(found["questions"], _QUESTION_COLUMNS)]
    if found.get("incidents"):
        lines += ["", "Incidents not resolved", table(found["incidents"])]
    if found.get("suspect_signals"):
        lines += ["", "Suspect signals", table(found["suspect_signals"])]
    return "\n".join(lines)


@command
def fidelity_status(ctx: typer.Context) -> None:
    """The twin's fidelity state and what its last check found."""
    with _api(ctx) as (_manifest, api, secrets):
        found = read_json(api, FIDELITY, f"GET {FIDELITY}")
    _say(ctx, found, _status_text(found), secrets)


@command
def fidelity_checks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000, help="Most checks.")] = 20,
) -> None:
    """The fidelity checks, newest first."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api, "GET", f"{FIDELITY}/checks", "GET /fidelity/checks", params={"limit": limit}
        )
    rows = [
        {
            "uid": check.get("uid"),
            "at": check.get("at"),
            "state": check.get("state"),
            "questions": len(check.get("questions") or {}),
            "duration_ms": check.get("duration_ms"),
        }
        for check in found.get("data") or []
    ]
    _say(ctx, {"checks": found.get("data") or []}, table(rows), secrets)


@command
def fidelity_check(ctx: typer.Context, timeout: Wait = JOB_WAIT_S) -> None:
    """Check the twin's fidelity now, for the newest boundary of its interval, and wait."""
    with _api(ctx) as (_manifest, api, secrets):
        job, created = _run_job(
            api,
            f"{FIDELITY}/checks",
            {},
            what="fidelity",
            follow="dta fidelity checks",
            limit_s=timeout,
        )
        found = read_json(api, FIDELITY, f"GET {FIDELITY}")
    result = {"job": job, "created": created, "fidelity": found}
    lead = "Checked." if created else "That boundary was checked already; its record stands."
    _say(ctx, result, f"{lead}\n{_status_text(found)}", secrets)


@command
def fidelity_incidents(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="open, remodel_requested, failed, resolved, or unresolved for every one not "
            "resolved.",
        ),
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="breach, stale, signal or reconcile.")
    ] = None,
) -> None:
    """The incidents, newest first."""
    params = {key: value for key, value in (("status", status), ("kind", kind)) if value}
    with _api(ctx) as (_manifest, api, secrets):
        found = computeops.read_all(api, f"{FIDELITY}/incidents", params)
    _say(ctx, {"incidents": found}, table(found, _INCIDENT_COLUMNS), secrets)


@command
def fidelity_incident(
    ctx: typer.Context, incident: Annotated[str, typer.Argument(help="The incident's uid.")]
) -> None:
    """One incident and every step it went through."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "GET",
            f"{FIDELITY}/incidents/{_segment(incident)}",
            "GET /fidelity/incidents/{id}",
        )
    data = found.get("data") or {}
    lines = [f"{name:<16} {_brief(data.get(name))}" for name in _INCIDENT_COLUMNS[1:]]
    for name in ("resolved_at", "awaiting", "detail"):
        lines.append(f"{name:<16} {_brief(data.get(name))}")
    steps = data.get("steps") or []
    if steps:
        lines += ["", "Steps", table(steps)]
    _say(ctx, found, "\n".join([f"Incident {data.get('uid')}", *lines]), secrets)


def _stepped_text(found: Mapping[str, Any]) -> str:
    data = found.get("data") or {}
    events = [event.get("type") for event in found.get("events") or []]
    version = (found.get("version") or {}).get("seq")
    return (
        f"{data.get('uid')}: {data.get('status')}"
        + (f"; told: {', '.join(str(e) for e in events)}" if events else "")
        + (f"; version {version}" if version is not None else "")
    )


def _incident_step(ctx: typer.Context, incident: str, step: str, by: str, note: str | None):
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "POST",
            f"{FIDELITY}/incidents/{_segment(incident)}/{step}",
            f"POST /fidelity/incidents/{{id}}/{step}",
            body=_step_body(by, note),
        )
    log.info(f"incident {step} by hand", incident=incident, by=by)
    _say(ctx, found, _stepped_text(found), secrets)


@command
def fidelity_retry(
    ctx: typer.Context,
    incident: Annotated[str, typer.Argument(help="The incident's uid.")],
    by: ById,
    note: Note = None,
) -> None:
    """Put a breach or stale incident back to its start; the next check takes it on."""
    _incident_step(ctx, incident, "retry", by, note)


@command
def fidelity_resolve(
    ctx: typer.Context,
    incident: Annotated[str, typer.Argument(help="The incident's uid.")],
    by: ById,
    note: Note = None,
) -> None:
    """Resolve a signal or reconcile incident by hand, once its cause is mended."""
    _incident_step(ctx, incident, "resolve", by, note)


@command
def fidelity_correct(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="The question whose model is corrected.")],
    rung: Annotated[
        str,
        typer.Option(
            "--rung",
            help="recalibrate (re-fit the active model) or reselect (every candidate).",
        ),
    ],
    timeout: Wait = JOB_WAIT_S,
) -> None:
    """Run one correction by hand on recent data, and wait for it."""
    with _api(ctx) as (_manifest, api, secrets):
        job, created = _run_job(
            api,
            f"{FIDELITY}/corrections",
            {"question": question, "rung": rung},
            what="correction",
            follow="dta fidelity incidents",
            limit_s=timeout,
        )
    outcome = job.get("result")
    text = f"Correction of {question} ({rung}): job {job.get('id')} {job.get('state')}\n" + (
        json.dumps(outcome, indent=2, default=str) if outcome is not None else ""
    )
    _say(ctx, {"job": job, "created": created}, text.rstrip(), secrets)


@command
def fidelity_signals(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option("--status", help="suspect or ok.")] = None,
) -> None:
    """The observed signals found suspect, and those cleared since."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "GET",
            f"{FIDELITY}/signals",
            "GET /fidelity/signals",
            params={"status": status} if status else None,
        )
    rows = found.get("data") or []
    _say(ctx, {"signals": rows}, table(rows, _SIGNAL_COLUMNS), secrets)


@command
def fidelity_clear(
    ctx: typer.Context,
    signal: Annotated[str, typer.Argument(help="The signal's uid.")],
    by: ById,
    note: Note = None,
) -> None:
    """Take a signal off the suspect list; sync writes its readings again."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "POST",
            f"{FIDELITY}/signals/{_segment(signal)}/clear",
            "POST /fidelity/signals/{uid}/clear",
            body=_step_body(by, note),
        )
    log.info("signal cleared by hand", signal=signal, by=by)
    _say(ctx, found, _stepped_text(found), secrets)


@command
def fidelity_errors(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="The question's id.")],
) -> None:
    """The error measured of one question at each origin a check scored, oldest first."""
    path = f"{FIDELITY}/questions/{_segment(question)}/errors"
    with _api(ctx) as (_manifest, api, secrets):
        rows = computeops.read_all(api, path, {"format": "json"})
    columns = ["origin", "scored_at", "error", "n", "model", "in_window", "check"]
    _say(ctx, {"question": question, "errors": rows}, table(rows, columns), secrets)


# -- dta actions -------------------------------------------------------------------------

_RECORD_COLUMNS = [
    "uid",
    "action",
    "object",
    "status",
    "source",
    "proposed_at",
    "reconciliation",
]


def _record_text(record: Mapping[str, Any]) -> str:
    lines = [f"Record   {record.get('uid')}"]
    for name in ("action", "object", "params", "source", "status", "fidelity", "reason"):
        lines.append(f"{name:<15} {_brief(record.get(name))}")
    for name in ("reasons", "approval", "execution", "reconciliation", "observed"):
        if record.get(name):
            lines.append(f"{name:<15} {_brief(record.get(name))}")
    return "\n".join(lines)


@command
def actions_list(ctx: typer.Context) -> None:
    """The action types: what each acts on, its control's bounds and autonomy, its binding."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(api, "GET", ACTIONS, "GET /actions")
    rows = [
        {
            "id": action.get("id"),
            "control": action.get("control"),
            "target": action.get("target"),
            "binding": action.get("binding"),
            "autonomy": action.get("autonomy"),
            "bounds": action.get("bounds"),
            "settle": action.get("settle"),
            "status": action.get("status"),
            "executable": action.get("executable"),
        }
        for action in found.get("data") or []
    ]
    configured = found.get("executor_configured")
    text = table(rows) + f"\nExecutor configured: {'yes' if configured else 'no'}"
    _say(ctx, found, text, secrets)


@command
def actions_propose(
    ctx: typer.Context,
    action: Annotated[str, typer.Argument(help="The action type's id.")],
    object_uid: Annotated[str, typer.Option("--object", help="Uid of the object acted on.")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", help="A parameter as name=value; a JSON value is read as such."),
    ] = None,
    reason: Annotated[str | None, typer.Option("--reason", help="Why it is proposed.")] = None,
    source: Annotated[
        str, typer.Option("--source", help="Who proposes: an agent's or a person's name.")
    ] = DEFAULT_SOURCE,
    key: Annotated[
        str | None,
        typer.Option("--key", help="Idempotency-Key: the same key and proposal, one record."),
    ] = None,
) -> None:
    """Propose an action on one object; the twin judges it, records it and, if its control
    allows and its fidelity is healthy, executes it. A refusal exits 1 with every reason."""
    params = dict(_param(text) for text in param or [])
    body = {"object": object_uid, "params": params, "reason": reason, "source": source}
    with _api(ctx) as (_manifest, api, secrets):
        found, status = _call(
            api,
            "POST",
            f"{ACTIONS}/{_segment(action)}/propose",
            "POST /actions/{id}/propose",
            ok=(200, 201),
            body=body,
            key=key,
        )
    record = found.get("data") or {}
    log.info(
        "action proposed",
        action=action,
        record=record.get("uid"),
        status=record.get("status"),
        created=status == 201,
    )
    lead = "" if status == 201 else "The key named this proposal before; its record stands.\n"
    _say(ctx, found, lead + _record_text(record), secrets)


@command
def actions_records(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status", help="proposed, rejected, approved, blocked, executed or failed."),
    ] = None,
    action: Annotated[str | None, typer.Option("--action", help="Only this action type.")] = None,
) -> None:
    """What became of the proposals, newest first."""
    params = {key: value for key, value in (("status", status), ("action", action)) if value}
    with _api(ctx) as (_manifest, api, secrets):
        found = computeops.read_all(api, f"{ACTIONS}/records", params)
    _say(ctx, {"records": found}, table(found, _RECORD_COLUMNS), secrets)


@command
def actions_record(
    ctx: typer.Context, uid: Annotated[str, typer.Argument(help="The record's uid.")]
) -> None:
    """One proposal: what was judged and decided, the call and its answer, the effect."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api, "GET", f"{ACTIONS}/records/{_segment(uid)}", "GET /actions/records/{uid}"
        )
    _say(ctx, found, _record_text(found.get("data") or {}), secrets)


def _decide(ctx: typer.Context, uid: str, step: str, by: str, note: str | None) -> None:
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "POST",
            f"{ACTIONS}/records/{_segment(uid)}/{step}",
            f"POST /actions/records/{{uid}}/{step}",
            body=_step_body(by, note),
        )
    record = found.get("data") or {}
    log.info(f"action {step} answered", record=uid, by=by, status=record.get("status"))
    _say(ctx, found, _record_text(record), secrets)


@command
def actions_approve(
    ctx: typer.Context,
    uid: Annotated[str, typer.Argument(help="The record's uid.")],
    by: ById,
    note: Note = None,
) -> None:
    """Approve a proposed supervised action: it is executed now, unless the twin failed."""
    _decide(ctx, uid, "approve", by, note)


@command
def actions_reject(
    ctx: typer.Context,
    uid: Annotated[str, typer.Argument(help="The record's uid.")],
    by: ById,
    note: Note = None,
) -> None:
    """Turn a proposed action down."""
    _decide(ctx, uid, "reject", by, note)


@command
def actions_reconcile(ctx: typer.Context) -> None:
    """Compare every executed action whose settle time has passed with what is observed."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(api, "POST", f"{ACTIONS}/reconcile", "POST /actions/reconcile")
    rows = [
        {
            "record": entry.get("record"),
            "object": entry.get("object"),
            "value": entry.get("value"),
            "observed": entry.get("observed"),
            "outcome": entry.get("outcome"),
        }
        for entry in found.get("data") or []
    ]
    lines = [table(rows)]
    if found.get("opened"):
        lines.append(f"Incidents opened: {', '.join(found['opened'])}")
    if found.get("closed"):
        lines.append(f"Incidents closed: {', '.join(found['closed'])}")
    _say(ctx, found, "\n".join(lines), secrets)


# -- dta control -------------------------------------------------------------------------


@command
def control_list(ctx: typer.Context) -> None:
    """The control loops: what each drives, its policy and interval, whether it is enabled."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(api, "GET", LOOPS, "GET /control/loops")
    rows = [
        {
            "id": loop.get("id"),
            "controls": ", ".join(loop.get("controls") or []),
            "policy": loop.get("policy"),
            "interval": loop.get("interval"),
            "enabled": loop.get("enabled"),
            "status": loop.get("status"),
            "last_run_at": loop.get("last_run_at"),
        }
        for loop in found.get("data") or []
    ]
    _say(ctx, found, table(rows), secrets)


def _switch(ctx: typer.Context, loop: str, step: str, by: str) -> None:
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(
            api,
            "POST",
            f"{LOOPS}/{_segment(loop)}/{step}",
            f"POST /control/loops/{{id}}/{step}",
            body={"by": by},
        )
    data = found.get("data") or {}
    log.info(f"loop {step}d", loop=loop, by=by)
    text = f"{data.get('id')}: {'enabled' if data.get('enabled') else 'disabled'}"
    _say(ctx, found, text, secrets)


@command
def control_enable(
    ctx: typer.Context, loop: Annotated[str, typer.Argument(help="The loop's id.")], by: ById
) -> None:
    """Let a loop run: the scheduler asks for it once per interval. The user's decision."""
    _switch(ctx, loop, "enable", by)


@command
def control_disable(
    ctx: typer.Context, loop: Annotated[str, typer.Argument(help="The loop's id.")], by: ById
) -> None:
    """Stop a loop."""
    _switch(ctx, loop, "disable", by)


@command
def control_run(
    ctx: typer.Context,
    loop: Annotated[str, typer.Argument(help="The loop's id.")],
    timeout: Wait = JOB_WAIT_S,
) -> None:
    """Run an enabled loop now and wait: its policy is asked, and each action it names is
    proposed with the loop as its source."""
    with _api(ctx) as (_manifest, api, secrets):
        job, created = _run_job(
            api,
            f"{LOOPS}/{_segment(loop)}/run",
            {},
            what="loop",
            follow="dta control list",
            limit_s=timeout,
        )
    run = job.get("result") or {}
    counts = ", ".join(
        f"{name} {run.get(name, 0)}"
        for name in ("proposed", "executed", "blocked", "rejected", "failed", "awaiting")
    )
    lines = [
        f"Loop {loop}: job {job.get('id')} {job.get('state')}; run {run.get('status')}"
        + ("" if not run.get("repeated") else " (the key named its last run)"),
        counts,
    ]
    if run.get("records"):
        lines.append(table(run["records"], ["uid", "action", "object", "status"]))
    for note in run.get("notes") or []:
        lines.append(f"note: {note}")
    _say(ctx, {"job": job, "created": created, "run": run}, "\n".join(lines), secrets)


# -- dta notifications -------------------------------------------------------------------


@command
def notifications(ctx: typer.Context) -> None:
    """The twin's last notifications since its API started, newest first, and their fate."""
    with _api(ctx) as (_manifest, api, secrets):
        found, _ = _call(api, "GET", NOTIFICATIONS, "GET /lifecycle/notifications")
    rows = [
        {
            "time": notice.get("time"),
            "type": notice.get("type"),
            "subject": notice.get("subject"),
            "status": notice.get("status"),
            "tries": notice.get("tries"),
            "http_status": notice.get("http_status"),
        }
        for notice in found.get("data") or []
    ]
    _say(ctx, found, f"Kind: {found.get('kind')}\n{table(rows)}", secrets)


__all__ = [
    "actions_approve",
    "actions_list",
    "actions_propose",
    "actions_reconcile",
    "actions_record",
    "actions_records",
    "actions_reject",
    "control_disable",
    "control_enable",
    "control_list",
    "control_run",
    "fidelity_check",
    "fidelity_checks",
    "fidelity_clear",
    "fidelity_correct",
    "fidelity_errors",
    "fidelity_incident",
    "fidelity_incidents",
    "fidelity_resolve",
    "fidelity_retry",
    "fidelity_signals",
    "fidelity_status",
    "notifications",
]
