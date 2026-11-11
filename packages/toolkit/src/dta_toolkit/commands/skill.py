"""`dta generate skill` and `dta install-skill`: the operator skill of the twin.

The skill is rendered into the workspace, then copied to where the agent reads
skills from. A copy is only ever installed from a render of the current
manifest, and it replaces an earlier copy in one rename, so the agent never
reads a half-written or outdated skill.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import secrets
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest, manifest_hash
from dta_core.manifest.checks import evaluate_gates, subject_of

from ..output import command, emit, session
from ..skill import MARKER_FILE, render_skill, skill_name
from ..workspace import Workspace
from .common import gate_inputs, timed

log = Log("commands.skill")

HANDOVER_STAGE = 8
SKILL_DIR = "skill"
HANDOVER_REPORT = "reports/handover.md"
_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


class Scope(StrEnum):
    personal = "personal"
    project = "project"


def skill_root(workspace: Workspace) -> Path:
    return workspace.root / SKILL_DIR


def skill_dir(workspace: Workspace, manifest: Manifest) -> Path:
    twin_id = manifest.twin.id if manifest.twin and manifest.twin.id else ""
    return skill_root(workspace) / skill_name(twin_id)


def rendered_hash(directory: Path) -> str | None:
    """The manifest hash a generated skill records, or None when there is no readable one."""
    marker = directory / MARKER_FILE
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8")).get("manifest_hash")
    except (OSError, ValueError, AttributeError) as exc:
        log.warn("skill marker cannot be read", path=str(marker), error=type(exc).__name__)
        return None
    return value if isinstance(value, str) else None


def write_skill(workspace: Workspace, manifest: Manifest) -> dict[str, Any]:
    """Render the skill and bring `<workspace>/skill/` in line with it."""
    digest = manifest_hash(manifest)
    rendered = render_skill(manifest, manifest_hash=digest)
    root = skill_root(workspace)
    directory = root / rendered.name
    written: list[str] = []
    removed: list[str] = []
    with workspace.exclusive():
        for relative, text in sorted(rendered.files.items()):
            target = directory / relative
            if target.is_file() and target.read_text(encoding="utf-8") == text:
                continue
            workspace.atomic_write(target, text)
            written.append(relative)
        if root.is_dir():
            produced = {directory / relative for relative in rendered.files}
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_dir() and not path.is_symlink():
                    with contextlib.suppress(OSError):
                        path.rmdir()  # only succeeds once the directory is empty
                elif path not in produced:
                    workspace.remove(path)
                    removed.append(str(path.relative_to(root)))
    log.info(
        "skill generated",
        skill=rendered.name,
        written=written,
        removed=removed,
        manifest_hash=digest,
    )
    return {
        "name": rendered.name,
        "path": str(directory),
        "files": sorted(rendered.files),
        "written": written,
        "removed": removed,
        "manifest_hash": digest,
    }


@command
def generate_skill(ctx: typer.Context) -> None:
    """Render the operator skill from the manifest into `<workspace>/skill/<name>/`."""
    workspace = session(ctx).workspace
    result = write_skill(workspace, workspace.load_manifest())
    changed = len(result["written"]) + len(result["removed"])
    text = (
        f"Skill {result['name']} written to {result['path']} ({changed} file(s) changed)."
        if changed
        else f"Skill {result['name']} at {result['path']} is current."
    )
    emit(ctx, result, text)


def _exchange(first: Path, second: Path) -> bool:
    """Swap two directories in one step (Linux renameat2); False when that is not available."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    done = renameat2(
        _AT_FDCWD, os.fsencode(first), _AT_FDCWD, os.fsencode(second), _RENAME_EXCHANGE
    )
    if done != 0:
        log.warn("directories cannot be exchanged in one step", errno=ctypes.get_errno())
        return False
    return True


def install_directory(source: Path, destination: Path) -> None:
    """Copy `source` to `destination`, replacing an earlier copy in one rename."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{secrets.token_hex(4)}.tmp"
    shutil.copytree(source, staging)
    try:
        if not destination.exists():
            os.rename(staging, destination)
            return
        if _exchange(staging, destination):
            shutil.rmtree(staging)  # now holds the earlier copy
            return
        # Without an atomic exchange the earlier copy is moved aside first; the new
        # one still appears in one rename.
        log.warn("falling back to two renames to replace the installed skill")
        aside = destination.parent / f".{destination.name}.{secrets.token_hex(4)}.old"
        os.rename(destination, aside)
        os.rename(staging, destination)
        shutil.rmtree(aside)
    except BaseException as exc:
        log.error("skill could not be put in place", exc=exc, destination=str(destination))
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _target_base(target: Path | None, scope: Scope | None) -> Path:
    if target is not None and scope is not None:
        log.error("install-skill given both --target and --scope")
        raise DtaError("pass either --target or --scope, not both", code="install.options")
    if target is not None:
        return target.expanduser().resolve()
    if (scope or Scope.personal) == Scope.personal:
        return Path.home() / ".claude" / "skills"
    return Path.cwd() / ".claude" / "skills"


def handover_text(
    workspace: Workspace, manifest: Manifest, installed: Path, name: str, digest: str
) -> str:
    twin, contract, interfaces = manifest.twin, manifest.contract, manifest.interfaces
    outcomes = evaluate_gates(gate_inputs(workspace, manifest))
    ui = f"served, port in `{interfaces.ui_port_env}`" if interfaces.ui else "not served"
    lines = [
        f"# Handover: {twin.name or twin.id} ({twin.id})",
        "",
        f"- Tier: {contract.tier.value if contract and contract.tier else 'not set'}",
        f"- Manifest hash: `{digest}`",
        f"- Skill `{name}` installed at `{installed}`",
        f"- API address: the variable `{interfaces.base_url_env}` "
        f"(http://127.0.0.1 and the port in `{interfaces.api_port_env}`)",
        f"- API token: the variable `{interfaces.token_env}`",
        f"- Browser UI: {ui}",
        "",
        "## Questions",
        "",
        "| Question | Text | Tolerance | Provisional |",
        "|---|---|---|---|",
    ]
    provisional = []
    for qid, question in sorted((contract.questions or {}).items() if contract else []):
        tolerance = question.tolerance
        if tolerance is not None and tolerance.value is not None:
            metric = tolerance.metric.value if tolerance.metric is not None else "error"
            unit = f" {tolerance.unit}" if tolerance.unit else ""
            shown = f"{metric} ≤ {tolerance.value:g}{unit}"
        else:
            shown = "not set"
        text = " ".join((question.text or "").split()).replace("|", "\\|")
        lines.append(f"| {qid} | {text} | {shown} | {'yes' if question.provisional else 'no'} |")
        if question.provisional:
            provisional.append(f"- Question {qid}: history is too short to validate its answer.")
    for outcome in outcomes:
        if outcome.stage < HANDOVER_STAGE and not outcome.passed:
            reasons = "; ".join(outcome.reasons) or outcome.verdict.value
            provisional.append(f"- Gate {outcome.stage} ({outcome.name}) has not passed: {reasons}")
    lines += ["", "## Provisional", ""]
    lines += provisional or ["Nothing is provisional."]
    return "\n".join(lines) + "\n"


@command
def install_skill(
    ctx: typer.Context,
    target: Annotated[
        Path | None,
        typer.Option("--target", help="Directory to install into, instead of a scope."),
    ] = None,
    scope: Annotated[
        Scope | None,
        typer.Option(
            "--scope",
            help="personal: ~/.claude/skills (the default); project: ./.claude/skills.",
        ),
    ] = None,
) -> None:
    """Copy the generated skill to where the agent reads it. Records the handover gate."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    workspace.load_state()
    digest = manifest_hash(manifest)
    source = skill_dir(workspace, manifest)
    found = rendered_hash(source)
    if found is None:
        log.warn("install refused: no generated skill", path=str(source))
        raise CheckFailed(
            "no skill has been generated; run `dta generate skill`",
            code="skill.missing",
            detail={"path": str(source)},
        )
    if found != digest:
        log.warn("install refused: skill is from another manifest", found=found, current=digest)
        raise CheckFailed(
            "the generated skill is from another manifest; run `dta generate skill` again",
            code="skill.stale",
            detail={"skill_manifest_hash": found, "manifest_hash": digest},
        )
    destination = _target_base(target, scope) / source.name
    with timed(log, "skill install", destination=str(destination)):
        install_directory(source, destination)
    report = workspace.root / HANDOVER_REPORT
    with workspace.exclusive():
        state = workspace.load_state()
        state.installed_skill = str(destination)
        workspace.save_state(state)
        workspace.record_gate(
            HANDOVER_STAGE,
            passed=True,
            subject_hash=subject_of(HANDOVER_STAGE, manifest),
            detail={"skill": source.name, "installed": str(destination)},
        )
        workspace.atomic_write(
            report, handover_text(workspace, manifest, destination, source.name, digest)
        )
    result = {
        "skill": source.name,
        "installed": str(destination),
        "manifest_hash": digest,
        "handover": str(report),
    }
    emit(ctx, result, f"Skill {source.name} installed at {destination}. Handover: {report}")
