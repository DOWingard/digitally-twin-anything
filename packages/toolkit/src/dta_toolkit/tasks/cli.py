"""The `dta tasks` sub-application.

`plan --open`, `merge` and `abort` are for the orchestrating agent only.
`claim` and `submit` are what a delegated agent runs. `plan` without `--open`
and `status` change nothing.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ..output import command, emit, session
from . import lifecycle
from .errors import TaskError

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _plan_text(result: dict[str, Any]) -> str:
    lines = [f"stage {result['stage']}: {len(result['tasks'])} task(s)"]
    for index, batch in enumerate(result["batches"]):
        marker = "  <- next" if index == result["next_batch"] else ""
        lines.append(f"batch {index + 1}{marker}")
        for task_id in batch:
            task = next(entry for entry in result["tasks"] if entry["id"] == task_id)
            lines.append(f"  {task_id}  [{task['status']}]")
            lines.append(f"    reads  {', '.join(task['reads']['manifest']) or '-'}")
            lines.append(f"    owns   {', '.join(task['owned_keys']) or '-'}")
            lines.append(f"    writes {task['write_zone']}")
            if task["needs"]:
                lines.append(f"    needs  {', '.join(task['needs'])}")
    if result.get("opened"):
        opened = result["opened"]
        lines.append(f"opened batch {opened['batch']}: {', '.join(opened['tasks'])}")
    elif result["open_batch"]:
        lines.append(f"batch {result['open_batch']} is open")
    return "\n".join(lines)


@app.command("plan")
@command
def plan(
    ctx: typer.Context,
    stage: Annotated[
        str | None,
        typer.Option("--stage", help="Stage to plan: 3, 4, 6 or operate."),
    ] = None,
    open_batch: Annotated[
        bool,
        typer.Option("--open", help="Open the next batch (orchestrator only)."),
    ] = False,
    store_version: Annotated[
        str | None,
        typer.Option("--store-version", help="Store version the batch's tasks must read."),
    ] = None,
) -> None:
    """List a stage's tasks and the batches they run in; with --open, open the next batch."""
    if stage is None:
        raise TaskError("pass --stage (3, 4, 6 or operate)", code="tasks.stage.required")
    if store_version is not None and not open_batch:
        raise TaskError("--store-version only applies with --open", code="tasks.usage")
    workspace = session(ctx).workspace
    if open_batch:
        result = lifecycle.open_batch(workspace, stage, store_version)
    else:
        result = lifecycle.plan(workspace, stage)
    emit(ctx, result, _plan_text(result))


@app.command("claim")
@command
def claim(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(metavar="ID", help="Task to claim.")],
    holder: Annotated[str, typer.Option("--holder", help="Name of the agent taking the task.")],
    ttl: Annotated[
        int, typer.Option("--ttl", help="Seconds until the lease expires unless renewed.")
    ] = lifecycle.DEFAULT_TTL_S,
    renew: Annotated[
        bool, typer.Option("--renew", help="Extend a lease already held; needs --token.")
    ] = False,
    token: Annotated[
        str | None, typer.Option("--token", help="Lease token returned by the claim.")
    ] = None,
) -> None:
    """Take the lease on a task of the open batch, or extend one already held."""
    workspace = session(ctx).workspace
    if renew != (token is not None):
        raise TaskError("--renew and --token go together", code="tasks.usage")
    if renew:
        result = lifecycle.renew(workspace, task_id, holder, token, ttl)
        emit(ctx, result, f"{task_id}: lease extended until {result['expires_at']}")
        return
    result = lifecycle.claim(workspace, task_id, holder, ttl)
    emit(
        ctx,
        result,
        f"{task_id}: claimed by {holder} until {result['expires_at']}\n"
        f"token: {result['token']}\n"
        f"read {result['paths']['spec']} and {result['paths']['input']}/, "
        f"write only under {result['paths']['output']}/",
    )


@app.command("submit")
@command
def submit(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(metavar="ID", help="Task to hand over.")],
    token: Annotated[str, typer.Option("--token", help="Lease token returned by the claim.")],
) -> None:
    """Check a finished task and seal its output; exits 1 with every reason if it is refused."""
    result = lifecycle.submit(session(ctx).workspace, task_id, token)
    emit(ctx, result, f"{task_id}: submitted ({len(result['patch_keys'])} manifest key(s))")


@app.command("merge")
@command
def merge(
    ctx: typer.Context,
    task_id: Annotated[
        str | None, typer.Argument(metavar="[ID]", help="Submitted task to merge.")
    ] = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Merge every submitted task of the open batch.")
    ] = False,
) -> None:
    """Apply submitted results to the manifest, in id order (orchestrator only)."""
    result = lifecycle.merge(session(ctx).workspace, task_id, everything)
    text = f"merged: {', '.join(result['merged']) or 'nothing'}"
    if result["already_merged"]:
        text += f"\nalready merged: {', '.join(result['already_merged'])}"
    if result["batch_closed"]:
        text += f"\nbatch {result['batch']} closed"
    emit(ctx, result, text)


@app.command("abort")
@command
def abort(
    ctx: typer.Context,
    task_id: Annotated[str | None, typer.Argument(metavar="[ID]", help="Task to give up.")] = None,
    whole_batch: Annotated[
        bool, typer.Option("--batch", help="Give up every unfinished task and close the batch.")
    ] = False,
) -> None:
    """Abandon a task, or the whole open batch (orchestrator only)."""
    result = lifecycle.abort(session(ctx).workspace, task_id, whole_batch)
    text = f"abandoned: {', '.join(result['abandoned']) or 'nothing'}"
    if result["batch_closed"]:
        text += f"\nbatch {result['batch']} closed"
    for finding in result["findings"]:
        text += f"\n{finding['code']}: {finding['message']}"
    emit(ctx, result, text)


@app.command("status")
@command
def status(
    ctx: typer.Context,
    verify: Annotated[
        bool,
        typer.Option("--verify", help="Also run the shared-state checks a submit would run."),
    ] = False,
) -> None:
    """Show the open batch, its tasks and leases, and the proposals stored so far."""
    result = lifecycle.status(session(ctx).workspace, verify)
    batch = result["open_batch"]
    lines = [f"merged tasks: {len(result['merged'])}, proposals: {len(result['proposals'])}"]
    if batch is None:
        lines.append("no batch is open")
    else:
        lines.append(f"batch {batch['id']} (stage {batch['stage']}) is open")
        for task in batch["tasks"]:
            lease = task["lease"]
            held = ""
            if lease and task["status"] == "claimed":
                state = "expired" if lease["expired"] else f"until {lease['expires_at']}"
                held = f"  held by {lease['holder']} ({state})"
            lines.append(f"  {task['id']}  [{task['status']}]{held}")
    if verify:
        checked = result["verified"]
        lines.append("checks: clean" if checked["clean"] else "checks: problems found")
        for problem in checked["problems"]:
            lines.append(f"  {problem['code']}: {problem['message']}")
    emit(ctx, result, "\n".join(lines))
