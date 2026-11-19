"""How every command reports: one result on stdout, logs on stderr, a meaningful exit code.

Exit codes: 0 the command succeeded; 1 a gate or check ran and failed; 2 the
command could not run (bad usage, missing workspace, held lock, internal error).
With `--json` stdout carries exactly one JSON document, so an agent can parse it.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import typer
from dta_core.errors import DtaError
from dta_core.log import Log

from .workspace import Workspace

log = Log("cli")


@dataclass
class Session:
    workspace: Workspace
    json_output: bool
    trace_id: str
    # The newest manifest version each runtime image reads, as it answered within this command.
    image_reads: dict[str, int] = field(default_factory=dict)


def session(ctx: typer.Context) -> Session:
    return ctx.obj


def emit(ctx: typer.Context, result: dict[str, Any], text: str | None = None) -> None:
    """Print a successful result: JSON with `--json`, otherwise the given text."""
    current = session(ctx)
    if current.json_output:
        document = {"ok": True, "trace_id": current.trace_id, "result": result}
        typer.echo(json.dumps(document, default=str))
    else:
        typer.echo(text if text is not None else json.dumps(result, indent=2, default=str))


def _report_failure(ctx: typer.Context, error: dict[str, Any], exit_code: int) -> None:
    current = session(ctx)
    if current.json_output:
        typer.echo(
            json.dumps({"ok": False, "trace_id": current.trace_id, "error": error}, default=str)
        )
    else:
        typer.echo(f"{error['code']}: {error['message']}", err=True)
        if error.get("detail"):
            typer.echo(json.dumps(error["detail"], indent=2, default=str), err=True)
    raise typer.Exit(exit_code)


def command(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a command body so every failure is logged and reported the same way.

    The wrapped function must take the Typer context as its first argument.
    """

    @functools.wraps(func)
    def wrapper(ctx: typer.Context, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(ctx, *args, **kwargs)
        except typer.Exit:
            raise
        except DtaError as exc:
            log.warn("command failed", command=ctx.command_path, code=exc.code, detail=exc.detail)
            _report_failure(ctx, exc.as_dict(), exc.exit_code)
        except Exception as exc:  # noqa: BLE001 - last line of defence for the command line
            log.error("command crashed", exc=exc, command=ctx.command_path)
            _report_failure(
                ctx,
                {"code": "internal", "message": f"{type(exc).__name__}: {exc}", "detail": {}},
                2,
            )

    return wrapper
