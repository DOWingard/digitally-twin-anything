"""Entry point of the `dta` command.

Options before the subcommand apply to every command:
`dta --workspace PATH --json <command> ...`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from dta_core.log import Log, bind_trace, configure, new_trace_id

from .commands import register as register_commands
from .output import Session
from .tasks.cli import app as tasks_app
from .workspace import Workspace

log = Log("cli")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Build and manage a digital twin from its manifest.",
)


@app.callback()
def _root(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", envvar="DTA_WORKSPACE", help="Twin workspace directory."),
    ] = Path("."),
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON document on stdout instead of text.")
    ] = False,
) -> None:
    current = Workspace(workspace)
    configure(log_file=current.log_path if current.exists() else None)
    trace_id = bind_trace(new_trace_id())
    ctx.obj = Session(workspace=current, json_output=json_output, trace_id=trace_id)
    log.debug("command started", command=ctx.invoked_subcommand, workspace=str(current.root))


register_commands(app)
app.add_typer(tasks_app, name="tasks", help="Delegated tasks: plan, claim, submit, merge.")
