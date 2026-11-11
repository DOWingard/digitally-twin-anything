"""`dta profile`: measure what each source holds, from inside the twin's stack.

The measurements become `sources.<id>.profile` (and, when measured, the
source's refresh period), marked as derived by the toolkit. While a task batch
is open those keys belong to its tasks, so only `--no-write` may run then: it is
what a delegated profiling task runs to produce its patch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed
from dta_core.log import Log
from dta_core.manifest import Manifest, Status, dump_manifest
from dta_core.manifest.models import SourceProfile
from pydantic import ValidationError

from ..output import command, emit, session
from ..stack import Stack
from ..tasks.state import load_state as load_tasks_state
from ..workspace import Workspace
from .common import fail_check, require_complete_env, scrub_values, timed

log = Log("commands.profile")

PROFILE_FAILED = "profile.failed"
BATCH_OPEN = "profile.batch_open"


def _refuse_while_batch_open(workspace: Workspace) -> None:
    batch = load_tasks_state(workspace).current()
    if batch is not None:
        log.warn("profile refused: a task batch is open", batch=batch.id)
        raise CheckFailed(
            f"task batch {batch.id} is open and its tasks own the profiles; "
            "run with --no-write, or merge or abort the batch first",
            code=BATCH_OPEN,
            detail={"batch": batch.id},
        )


def _confirmed(manifest: Manifest, path: str, value: Any) -> bool:
    """Whether an agent-owned field holds a value its owner stands behind."""
    if value is None:
        return False
    return manifest.field_status.get(path, Status.confirmed) == Status.confirmed


def _parse(
    profiles: dict[str, Any], failed: dict[str, Any]
) -> tuple[dict[str, SourceProfile], dict[str, Any]]:
    parsed: dict[str, SourceProfile] = {}
    failed = dict(failed)
    for source_id, document in sorted(profiles.items()):
        try:
            parsed[source_id] = SourceProfile.model_validate(document)
        except ValidationError as exc:
            log.error("profile returned by the runtime does not fit the manifest", source=source_id)
            failed[source_id] = {
                "code": "profile.invalid",
                "message": f"the profile does not match the manifest schema: {exc.error_count()} "
                "problem(s)",
            }
    return parsed, failed


def _write(workspace: Workspace, parsed: dict[str, SourceProfile]) -> tuple[list[str], list[str]]:
    """One manifest write under the lock. Returns (sources written, paths marked derived)."""
    written: list[str] = []
    derived: list[str] = []
    with workspace.exclusive():
        _refuse_while_batch_open(workspace)
        manifest = workspace.load_manifest()
        statuses = dict(manifest.field_status)
        for source_id, profile in parsed.items():
            source = (manifest.sources or {}).get(source_id)
            if source is None:
                log.warn("source left the manifest while it was profiled", source=source_id)
                continue
            source.profile = profile
            path = f"sources.{source_id}.profile"
            statuses[path] = Status.derived
            derived.append(path)
            written.append(source_id)
            period_path = f"sources.{source_id}.refresh_period"
            if profile.refresh_period is not None and not _confirmed(
                manifest, period_path, source.refresh_period
            ):
                source.refresh_period = profile.refresh_period
                statuses[period_path] = Status.derived
                derived.append(period_path)
        if written:
            manifest.field_status = dict(sorted(statuses.items()))
            workspace.save_manifest(manifest)
    return written, derived


def _text(parsed: dict[str, SourceProfile], failed: dict[str, Any], written: list[str]) -> str:
    lines = []
    for source_id, profile in parsed.items():
        lines.append(f"{source_id}: {len(profile.datasets)} dataset(s)")
        for name, dataset in sorted(profile.datasets.items()):
            span = ""
            if dataset.history_start and dataset.history_end:
                span = f", {dataset.history_start.isoformat()} .. {dataset.history_end.isoformat()}"
            lines.append(f"  {name}: {dataset.rows} row(s), {len(dataset.columns)} column(s){span}")
    for source_id, error in sorted(failed.items()):
        lines.append(f"FAIL {source_id}: {error.get('code')}: {error.get('message')}")
    lines.append(
        f"Written to the manifest: {', '.join(written)}." if written else "Nothing written."
    )
    return "\n".join(lines)


@command
def profile(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Profile only this source; may be repeated."),
    ] = None,
    no_write: Annotated[
        bool, typer.Option("--no-write", help="Print the profiles; change nothing.")
    ] = False,
    row_cap: Annotated[
        int | None,
        typer.Option("--row-cap", min=1, help="Rows of a dataset the statistics are computed on."),
    ] = None,
) -> None:
    """Profile each source from inside the stack and write the profiles into the manifest."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    stack = Stack(workspace, manifest)
    env = require_complete_env(stack)
    if not no_write:
        _refuse_while_batch_open(workspace)
    args = ["profile", "--at", datetime.now(UTC).isoformat()]
    for source_id in source or []:
        args += ["--source", source_id]
    if row_cap is not None:
        args += ["--row-cap", str(row_cap)]
    with timed(log, "profile", sources=source or "all", row_cap=row_cap):
        document = stack.runtime(*args, input=dump_manifest(manifest), store=False, timeout=None)
    parsed, failed = _parse(document.get("profiles") or {}, document.get("failed") or {})
    written: list[str] = []
    derived: list[str] = []
    if not no_write and parsed:
        written, derived = _write(workspace, parsed)
    result = scrub_values(
        {
            "profiles": {
                sid: p.model_dump(mode="json", exclude_none=True) for sid, p in parsed.items()
            },
            "failed": failed,
            "written": written,
            "derived": derived,
        },
        env,
    )
    text = _text(parsed, result["failed"], written)
    if not parsed and not failed:
        log.warn("no source was profiled", sources=source or "all")
        fail_check(ctx, PROFILE_FAILED, "no source was profiled", result, text)
    if failed:
        log.warn("some sources could not be profiled", failed=sorted(failed), written=written)
        fail_check(
            ctx,
            PROFILE_FAILED,
            f"cannot profile {', '.join(sorted(failed))}",
            result,
            text,
        )
    log.info("sources profiled", sources=sorted(parsed), written=written, derived=derived)
    emit(ctx, result, text)
