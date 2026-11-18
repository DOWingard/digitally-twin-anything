"""`dta env template` and `dta env check`: the variables the twin's stack needs.

The values live in the workspace's `.env` only. These commands read and write
names; a value is generated for the secrets the twin itself owns (the store's
password, the API token and, with read-only SQL, the store reader's password) and
never printed, logged or put into a result.
"""

from __future__ import annotations

import secrets
import stat
from dataclasses import dataclass
from typing import Any

import typer
from dta_core.log import Log
from dta_core.manifest import Manifest

from .. import addons
from ..envfile import read_env
from ..output import command, emit, session
from ..stack import STORE_PASSWORD_ENV, Stack
from ..workspace import Workspace
from .common import ENV_MISSING, fail_check

log = Log("commands.env")

ENV_FILE_MODE = 0o600
_SECRET_BYTES = 32
_OPEN_TO_OTHERS = stat.S_IRWXG | stat.S_IRWXO


@dataclass(frozen=True)
class EnvName:
    name: str
    description: str
    secret: bool
    generated: bool  # the toolkit makes up its value; nobody has to choose it


def _builtin(manifest: Manifest, stack: Stack) -> dict[str, tuple[str, bool]]:
    """What the toolkit knows about the variables of the twin's own services."""
    interfaces = manifest.interfaces
    return {
        STORE_PASSWORD_ENV: ("Password of the twin's store", True),
        stack.plan.token_env: ("Bearer token of the twin's API", True),
        stack.plan.api_port_env: ("Host port of the twin's API, bound to 127.0.0.1", False),
        stack.plan.ui_port_env: ("Host port of the twin's UI, bound to 127.0.0.1", False),
        interfaces.base_url_env: (
            "Address of the twin's API for agents and clients: http://127.0.0.1:<API port>",
            False,
        ),
        **addons.ENV_DESCRIPTIONS,
    }


def planned_names(manifest: Manifest, stack: Stack) -> list[EnvName]:
    """Every variable `.env` should hold: what the stack requires, then the API address."""
    builtin = _builtin(manifest, stack)
    generated = {STORE_PASSWORD_ENV, stack.plan.token_env}
    generated |= set(addons.selected(manifest).generated_env())
    names = list(stack.plan.required_env())
    if manifest.interfaces.base_url_env not in names:
        names.append(manifest.interfaces.base_url_env)
    planned = []
    for name in names:
        declared = manifest.env.get(name)
        known = builtin.get(name)
        if declared is not None and declared.description:
            description = declared.description
        elif known is not None:
            description = known[0]
        else:
            description = "Not described in the manifest's env section"
        if name in (STORE_PASSWORD_ENV, addons.SQL_READER_PASSWORD_ENV):
            secret = True
        elif declared is not None:
            secret = declared.secret
        elif known is not None:
            secret = known[1]
        else:
            secret = True  # what nobody described is treated as the more dangerous kind
        planned.append(EnvName(name, description, secret, name in generated))
    return planned


def render_example(manifest: Manifest, names: list[EnvName]) -> str:
    twin_id = manifest.twin.id if manifest.twin else "?"
    lines = [
        f'# Environment of the twin "{twin_id}", written by `dta env template`.',
        "# The real values go into .env next to this file: keep it private (mode 0600)",
        "# and never commit it. Values marked generated are filled in by the toolkit.",
    ]
    for entry in names:
        kind = "secret" if entry.secret else "not secret"
        if entry.generated:
            kind += ", generated"
        lines += ["", f"# {entry.description} ({kind})", f"{entry.name}="]
    return "\n".join(lines) + "\n"


def write_example(workspace: Workspace, manifest: Manifest, stack: Stack) -> bool:
    """Bring `.env.example` in line with the manifest. True when it changed."""
    text = render_example(manifest, planned_names(manifest, stack))
    with workspace.exclusive():
        path = workspace.env_example_path
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
        workspace.atomic_write(path, text)
    log.info("environment example written", path=str(workspace.env_example_path))
    return True


def extend_env(workspace: Workspace, names: list[EnvName]) -> tuple[bool, list[str]]:
    """Add the names `.env` lacks, never changing a line it has. (written, generated names)."""
    with workspace.exclusive():
        path = workspace.env_path
        existed = path.exists()
        present = read_env(path)
        text = path.read_text(encoding="utf-8") if existed else ""
        added: list[str] = []
        generated: list[str] = []
        for entry in names:
            if entry.name in present:
                continue
            value = ""
            if entry.generated:
                value = secrets.token_urlsafe(_SECRET_BYTES)
                generated.append(entry.name)
            added.append(f"{entry.name}={value}")
        if not added:
            return False, []
        if not existed:
            text = "# Values of the twin's environment. Keep this file private; never commit it.\n"
        elif text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(added) + "\n"
        workspace.atomic_write(path, text, mode=None if existed else ENV_FILE_MODE)
    log.info(
        "environment file extended" if existed else "environment file created",
        added=[line.split("=", 1)[0] for line in added],
        generated=generated,
    )
    return True, generated


def _to_fill(workspace: Workspace, names: list[EnvName]) -> list[str]:
    values = read_env(workspace.env_path)
    return [entry.name for entry in names if not values.get(entry.name)]


@command
def template(ctx: typer.Context) -> None:
    """Write `.env.example` and create or extend `.env`; values are generated or left to fill."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    stack = Stack(workspace, manifest)
    names = planned_names(manifest, stack)
    written = []
    if write_example(workspace, manifest, stack):
        written.append(workspace.env_example_path.name)
    env_written, generated = extend_env(workspace, names)
    if env_written:
        written.append(workspace.env_path.name)
    to_fill = _to_fill(workspace, names)
    result: dict[str, Any] = {"written": written, "generated": generated, "to_fill": to_fill}
    lines = [f"Wrote {', '.join(written)}." if written else "Nothing to write; files are current."]
    if generated:
        lines.append(f"Generated: {', '.join(generated)}")
    lines.append(
        f"To fill in {workspace.env_path.name}: {', '.join(to_fill)}"
        if to_fill
        else f"{workspace.env_path.name} sets every name."
    )
    log.info("environment template done", written=written, to_fill=to_fill)
    emit(ctx, result, "\n".join(lines))


@command
def check(ctx: typer.Context) -> None:
    """Exit 0 when `.env` sets every variable `dta env template` asks for; 1 naming those it
    lacks."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    stack = Stack(workspace, manifest)
    # The same names `template` lists to fill: the stack's, and the API's address that the
    # twin's clients and its operator skill read.
    required = [entry.name for entry in planned_names(manifest, stack)]
    values = read_env(workspace.env_path)
    absent = [name for name in required if not values.get(name)]
    warnings: list[dict[str, str]] = []
    if workspace.env_path.exists():
        mode = workspace.env_path.stat().st_mode
        if mode & _OPEN_TO_OTHERS:
            log.warn("environment file is readable by others", mode=oct(mode & 0o777))
            warnings.append(
                {
                    "code": "env.permissions",
                    "message": f"{workspace.env_path.name} can be read by group or others "
                    f"(mode {oct(mode & 0o777)}); run chmod 600 on it",
                }
            )
    result = {
        "env_file": str(workspace.env_path),
        "required": required,
        "missing": absent,
        "warnings": warnings,
    }
    lines = [f"warning  {entry['message']}" for entry in warnings]
    if absent:
        lines.append(f"{workspace.env_path.name} does not set {', '.join(absent)}")
        fail_check(
            ctx,
            ENV_MISSING,
            f"{workspace.env_path.name} does not set {', '.join(absent)}",
            result,
            "\n".join(lines),
        )
    lines.append(f"{workspace.env_path.name} sets all {len(required)} required name(s).")
    log.info("environment complete", required=len(required), warnings=len(warnings))
    emit(ctx, result, "\n".join(lines))
