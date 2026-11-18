"""`dta sync`: how often the twin reads its sources, and its runs.

`derive` applies the interval rule to what the manifest holds: the sources'
refresh and rate limit periods, the questions' stalenesses and the change
times profiling measured. With `--write` the interval it gives is recorded as
derived, with the inputs it came from. An interval somebody confirmed is never
replaced.

`run` asks the running twin's API for one run and, unless told not to, waits
for its job and prints the run's report. `status` prints what the API says of
the twin's clock, its freshness and its sources. Both reach the twin only
through its API, and print nothing of `.env`: the API never returns where a
source lives, and everything printed is scrubbed of the stack's secrets.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest, Status
from dta_core.manifest.checks.view import fmt_duration
from dta_core.manifest.derive import IntervalDerivation, derive_interval
from dta_core.manifest.models import Sync
from pydantic import TypeAdapter

from ..acceptance import TwinApi
from ..output import command, emit, session
from ..stack import Stack
from ..tasks.state import load_state as load_tasks_state
from ..workspace import Workspace
from .common import fail_check, require_complete_env, scrub_values
from .syncjobs import (
    HEALTH_PATH,
    PARTIAL_CODE,
    SOURCES_PATH,
    read_json,
    run_body,
    start_run,
    verdict,
    wait_job,
)

log = Log("commands.sync")

INTERVAL_PATH = "sync.interval"
DERIVATION_PATH = "sync.derivation"
UNDERIVABLE = "sync.underivable"
INFEASIBLE = "sync.infeasible"
INTERVAL_CONFIRMED = "sync.interval_confirmed"
BATCH_OPEN = "sync.batch_open"

_DURATION = TypeAdapter(timedelta)
_INPUTS = ("source_period", "rate_period", "min_staleness", "freshness_bound", "change_time")


def _refuse_while_batch_open(workspace: Workspace) -> None:
    """The rule's inputs are what the tasks of an open batch are still measuring."""
    batch = load_tasks_state(workspace).current()
    if batch is not None:
        log.warn("interval not written: a task batch is open", batch=batch.id)
        raise CheckFailed(
            f"task batch {batch.id} is open and its tasks may still change what the interval "
            "is derived from; merge or abort the batch first, or run without --write",
            code=BATCH_OPEN,
            detail={"batch": batch.id},
        )


def _iso(value: timedelta | None) -> str | None:
    """A duration as the manifest writes it."""
    return _DURATION.dump_python(value, mode="json") if value is not None else None


def _stated(manifest: Manifest) -> tuple[timedelta | None, bool]:
    """The interval the manifest holds, and whether somebody stands behind it."""
    value = manifest.sync.interval if manifest.sync else None
    status = manifest.field_status.get(INTERVAL_PATH, Status.confirmed)
    return value, value is not None and status == Status.confirmed


def _result(manifest: Manifest, found: IntervalDerivation) -> dict[str, Any]:
    stated, confirmed = _stated(manifest)
    return {
        "interval": _iso(found.interval),
        "binding": found.binding,
        "feasible": found.feasible,
        "problems": list(found.problems),
        "rule": found.inputs.rule,
        "inputs": {name: _iso(getattr(found.inputs, name)) for name in _INPUTS},
        "manifest_interval": _iso(stated),
        "manifest_interval_confirmed": confirmed,
        "agree": stated is not None and stated == found.interval,
        "written": False,
    }


def _text(result: dict[str, Any], found: IntervalDerivation) -> str:
    lines = [f"Rule:     {result['rule']}"]
    for name in _INPUTS:
        value = getattr(found.inputs, name)
        lines.append(f"  {name:<16} {fmt_duration(value) if value is not None else 'not known'}")
    if found.interval is None:
        lines.append("Interval: cannot be derived; nothing the rule needs is known")
    else:
        lines.append(f"Interval: {fmt_duration(found.interval)} (decided by {found.binding})")
    lines += [f"  problem  {problem}" for problem in found.problems]
    stated = result["manifest_interval"]
    if stated is None:
        lines.append("Manifest: sync.interval is not set")
    else:
        kind = "confirmed" if result["manifest_interval_confirmed"] else "not confirmed"
        same = "the same" if result["agree"] else "differs"
        lines.append(f"Manifest: sync.interval is {stated} ({kind}); {same}")
    if result["written"]:
        lines.append(f"Wrote {INTERVAL_PATH} and {DERIVATION_PATH}, marked derived.")
    return "\n".join(lines)


@command
def derive(
    ctx: typer.Context,
    write: Annotated[
        bool,
        typer.Option("--write", help="Record the interval and its inputs in the manifest."),
    ] = False,
) -> None:
    """Apply the interval rule to the manifest. Exit 1 when no interval meets the contract."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    found = derive_interval(manifest)
    result = _result(manifest, found)
    if found.interval is None:
        log.warn("interval cannot be derived: no input of the rule is known")
        fail_check(
            ctx,
            UNDERIVABLE,
            "the interval cannot be derived: profile the sources, or state their refresh "
            "periods and the questions' stalenesses",
            result,
            _text(result, found),
        )
    if not found.feasible:
        log.warn("no interval meets the contract", problems=list(found.problems))
        fail_check(
            ctx,
            INFEASIBLE,
            f"no interval meets the contract: {found.problems[0]}",
            result,
            _text(result, found),
        )
    if write and not result["agree"]:
        with workspace.exclusive():
            _refuse_while_batch_open(workspace)
            current = workspace.load_manifest()
            stated, confirmed = _stated(current)
            if confirmed:
                log.warn(
                    "derived interval not written: the manifest's interval is confirmed",
                    stated=_iso(stated),
                    derived=_iso(found.interval),
                )
                fail_check(
                    ctx,
                    INTERVAL_CONFIRMED,
                    f"sync.interval is confirmed as {_iso(stated)}; it is not replaced by the "
                    f"derived {_iso(found.interval)}. To use the rule, delete sync.interval and "
                    "its field_status line from the manifest and run this command again.",
                    result,
                    _text(result, found),
                )
            sync = current.sync or Sync()
            sync.interval = found.interval
            sync.derivation = found.inputs
            current.sync = sync
            statuses = dict(current.field_status)
            statuses[INTERVAL_PATH] = Status.derived
            statuses[DERIVATION_PATH] = Status.derived
            current.field_status = dict(sorted(statuses.items()))
            workspace.save_manifest(current)
        result["written"] = True
        result["manifest_interval"] = result["interval"]
        result["manifest_interval_confirmed"] = False
        result["agree"] = True
        log.info(
            "sync interval derived and written",
            interval=result["interval"],
            binding=found.binding,
        )
    else:
        log.info("sync interval derived", interval=result["interval"], binding=found.binding)
    emit(ctx, result, _text(result, found))


# -- runs through the API -----------------------------------------------------------

BAD_REPLAY = "sync.bad_replay"


def _replay(text: str | None) -> tuple[str, str] | None:
    if text is None:
        return None
    source_id, _, dataset = text.partition(".")
    if not source_id or not dataset:
        log.error("--replay is not SOURCE.DATASET")
        raise DtaError(
            "--replay takes SOURCE.DATASET, such as plant.readings",
            code=BAD_REPLAY,
            detail={"given": text},
        )
    return source_id, dataset


def _api_stack(workspace: Workspace) -> tuple[Manifest, Stack, dict[str, str], dict[str, str]]:
    """The manifest, its stack, the values of `.env` the client needs and those never shown."""
    manifest = workspace.load_manifest()
    stack = Stack(workspace, manifest)
    values = require_complete_env(stack)
    return manifest, stack, values, stack.secret_values()


def _scrub_error(error: DtaError, secrets: dict[str, str]) -> None:
    """Mask every value of the stack's secrets in what an error says, before it is printed."""
    error.message = scrub_values(error.message, secrets)
    error.detail = scrub_values(error.detail, secrets)
    error.args = (error.message,)


def _count(value: Any) -> str:
    return "-" if value is None else str(value)


def _age(seconds: Any) -> str:
    return "-" if seconds is None else f"{float(seconds):.0f} s"


def _report_text(job: dict[str, Any], report: dict[str, Any] | None) -> str:
    key = job.get("idempotency_key")
    lines = [f"Job      {job.get('id')} {job.get('state')}" + (f" (key {key})" if key else "")]
    if not report:
        error = job.get("error") or {}
        lines.append(f"Error    {error.get('code')}: {error.get('message')}")
        return "\n".join(lines)
    lines += [
        f"Run      {report.get('run')} {report.get('kind')} {report.get('status')}",
        f"Clock    now {report.get('now')}; latest data {report.get('watermark') or '-'}",
        f"Written  {_count(report.get('versions_written'))} version(s), "
        f"{_count(report.get('rows_written'))} row(s) of {_count(report.get('rows_read'))} read; "
        f"{_count(report.get('rows_rejected'))} set aside, {_count(report.get('rows_late'))} late",
    ]
    error = report.get("error") or {}
    if error:
        lines.append(f"Error    {error.get('code')}: {error.get('message')}")
    for source_id, entry in sorted((report.get("sources") or {}).items()):
        entry = entry or {}
        failure = entry.get("error") or {}
        line = (
            f"  {source_id:<14} {entry.get('status')!s:<8} read {_count(entry.get('rows_read'))}, "
            f"written {_count(entry.get('rows_written'))}, "
            f"set aside {_count(entry.get('rows_rejected'))}, late {_count(entry.get('rows_late'))}"
        )
        if failure:
            line += f"; {failure.get('code')}: {failure.get('message')}"
        lines.append(line)
    return "\n".join(lines)


@command
def run_sync(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Keep the run to this source; may be repeated."),
    ] = None,
    replay: Annotated[
        str | None,
        typer.Option("--replay", metavar="SOURCE.DATASET", help="Read one dataset again, whole."),
    ] = None,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="Print the job and return; do not wait for it.")
    ] = False,
) -> None:
    """Start one sync run of the running twin. Exit 0 only when every source was synced."""
    workspace = session(ctx).workspace
    body = run_body(source, _replay(replay))
    manifest, _stack, values, secrets = _api_stack(workspace)
    begun = time.perf_counter()
    try:
        with TwinApi(manifest, values) as api:
            started = start_run(api, body)
            if no_wait:
                result = scrub_values(
                    {"job": started.job, "created": started.created, "asked": body}, secrets
                )
                text = _report_text(result["job"], None).splitlines()[0]
                emit(ctx, result, f"{text}\nFollow it with dta sync status.")
                return
            job = wait_job(api, str(started.job.get("id")))
    except DtaError as exc:
        _scrub_error(exc, secrets)
        raise
    elapsed = round((time.perf_counter() - begun) * 1000.0, 1)
    found = verdict(job)
    report = job.get("result")
    result = scrub_values(
        {
            "job": {name: value for name, value in job.items() if name != "result"},
            "created": started.created,
            "asked": body,
            "report": report,
            "duration_ms": elapsed,
        },
        secrets,
    )
    text = _report_text(result["job"], result["report"])
    if not found.ok:
        log.warn("the sync run did not sync every source", code=found.code, duration_ms=elapsed)
        message = scrub_values(str(found.message), secrets)
        fail_check(ctx, found.code or PARTIAL_CODE, message, result, text)
    log.info("sync run succeeded", run=(report or {}).get("run"), duration_ms=elapsed)
    emit(ctx, result, text)


def _status_text(result: dict[str, Any]) -> str:
    clock = result.get("clock") or {}
    state = result.get("sync")
    lines = [
        f"Twin      {result.get('twin_id')} ({result.get('tier')})",
        f"Clock     {clock.get('kind')}, "
        f"{'available' if clock.get('available') else 'NOT available'}, now {clock.get('now')}",
    ]
    if state is None:
        lines.append("Sync      none: the manifest sets no sync interval")
    else:
        fresh = {True: "yes", False: "NO", None: "not known"}[state.get("fresh")]
        lines.append(
            f"Sync      every {state.get('interval')}; fresh: {fresh} "
            f"({_age(state.get('freshness_s'))} old, limit {state.get('staleness') or '-'})"
        )
        last = state.get("last_run")
        if last:
            lines.append(
                f"Last run  {last.get('uid')} {last.get('kind')} {last.get('status')}: "
                f"{_count(last.get('versions_written'))} version(s), "
                f"{_count(last.get('rows_written'))} row(s) written, "
                f"{_count(last.get('rows_rejected'))} set aside, "
                f"{_count(last.get('rows_late'))} late"
            )
        else:
            lines.append("Last run  none")
        job = state.get("job")
        lines.append(f"Job       {job.get('id')} {job.get('state')}" if job else "Job       none")
        if state.get("questions"):
            lines.append("Questions")
            for question in state["questions"]:
                fresh = {True: "fresh", False: "STALE", None: "unknown"}[question.get("fresh")]
                lines.append(
                    f"  {question.get('id')!s:<14} limit {question.get('staleness')!s:<8} "
                    f"{_age(question.get('freshness_s')):>9} old  {fresh:<7} "
                    f"behind: {question.get('source') or '-'}"
                )
    lines.append("Sources")
    for entry in result.get("sources") or []:
        lines.append(
            f"  {entry.get('id')!s:<14} {entry.get('status') or '-':<8} "
            f"latest data {entry.get('watermark') or '-'}, {_age(entry.get('freshness_s'))} old"
            + (f"; {entry.get('last_error')}" if entry.get("last_error") else "")
        )
    return "\n".join(lines)


@command
def sync_status(ctx: typer.Context) -> None:
    """The twin's clock, freshness and sources, as its API tells them. Exit 0 if it answers."""
    workspace = session(ctx).workspace
    manifest, stack, values, secrets = _api_stack(workspace)
    try:
        with TwinApi(manifest, values) as api:
            health = read_json(api, HEALTH_PATH)
            listing = read_json(api, SOURCES_PATH)
    except DtaError as exc:
        _scrub_error(exc, secrets)
        raise
    result = scrub_values(
        {
            "twin_id": health.get("twin_id"),
            "tier": health.get("tier"),
            "head": health.get("head"),
            "clock": health.get("clock"),
            "sync": health.get("sync"),
            "sources": listing.get("data") or [],
        },
        secrets,
    )
    state = result["sync"] or {}
    log.info(
        "sync status read",
        project=stack.plan.project,
        fresh=state.get("fresh"),
        job=(state.get("job") or {}).get("id"),
        sources=len(result["sources"]),
    )
    emit(ctx, result, _status_text(result))
