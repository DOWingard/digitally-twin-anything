"""`dta up`, `dta down`, `dta logs`: the twin's stack on this machine."""

from __future__ import annotations

from typing import Annotated

import typer
from dta_core.log import Log
from dta_core.manifest.checks import subject_of

from ..output import command, emit, session
from ..stack import Stack
from ..workspace import Workspace
from .common import require_complete_env, timed

log = Log("commands.lifecycle")

DEFAULT_TAIL = 200
# What removing the twin's data takes back: there is nothing built, and what was
# accepted no longer exists. The handed-over skill still describes the manifest.
GATES_OF_THE_DATA = (6, 7)


def _stack(workspace: Workspace) -> Stack:
    # Compose refuses to read the stack file at all, even to stop it, while a variable it
    # requires is unset; naming the variables here says what to fix.
    stack = Stack(workspace, workspace.load_manifest())
    require_complete_env(stack)
    return stack


def _services_text(services: list[dict]) -> str:
    if not services:
        return "No container of the stack exists."
    return "\n".join(
        f"{row['service']:<8} {row['state'] or '-':<10} {row['health'] or '-'}" for row in services
    )


@command
def up(
    ctx: typer.Context,
    services: Annotated[
        list[str] | None, typer.Argument(help="Services to start; all of them when none is named.")
    ] = None,
) -> None:
    """Start the stack's services and wait until they are healthy."""
    stack = _stack(session(ctx).workspace)
    with timed(log, "stack up", services=services or "all"):
        stack.up(*(services or []))
    state = stack.services()
    emit(ctx, {"project": stack.plan.project, "services": state}, _services_text(state))


@command
def down(
    ctx: typer.Context,
    destroy_data: Annotated[
        bool,
        typer.Option("--destroy-data", help="Also remove the volumes: the twin's data is lost."),
    ] = False,
) -> None:
    """Remove the stack's containers; its volumes are kept unless --destroy-data."""
    workspace = session(ctx).workspace
    stack = _stack(workspace)
    with timed(log, "stack down", volumes=destroy_data):
        stack.down(volumes=destroy_data)
    gates_reset: list[int] = []
    if destroy_data:
        manifest = workspace.load_manifest()
        state = workspace.load_state()
        for stage in GATES_OF_THE_DATA:
            if any(result.stage == stage and result.passed for result in state.gates.values()):
                workspace.record_gate(
                    stage,
                    passed=False,
                    subject_hash=subject_of(stage, manifest),
                    detail={"reason": "the twin's data was removed; build and accept again"},
                )
                gates_reset.append(stage)
        log.warn("the twin's data was removed", project=stack.plan.project, gates=gates_reset)
    result = {
        "project": stack.plan.project,
        "containers_removed": True,
        "volumes_removed": destroy_data,
        "gates_reset": gates_reset,
    }
    text = (
        f"Stack {stack.plan.project} removed with its data; build and accept again to use it."
        if destroy_data
        else f"Stack {stack.plan.project} removed; its volumes are kept."
    )
    emit(ctx, result, text)


@command
def logs(
    ctx: typer.Context,
    service: Annotated[
        str | None, typer.Argument(help="Service whose logs to show; all when none is named.")
    ] = None,
    tail: Annotated[int, typer.Option("--tail", min=1, help="Lines per container.")] = DEFAULT_TAIL,
) -> None:
    """The last lines the stack's containers wrote, with the values of `.env` masked."""
    stack = _stack(session(ctx).workspace)
    text = stack.logs(service, tail=tail)
    log.debug("stack logs read", service=service, tail=tail, characters=len(text))
    emit(ctx, {"project": stack.plan.project, "service": service, "tail": tail, "logs": text}, text)
