"""`dta generate skill` and `dta install-skill`: the operator skill of the twin.

The skill is rendered into the workspace, then copied to where the agent reads
skills from. A copy is only ever installed from a render of the current
manifest, and the new copy appears in one rename, so the agent never reads a
half-written or outdated skill. Handover follows acceptance: a twin that has
not been accepted is handed over only when that is asked for explicitly, and
is then recorded as not handed over.
"""

from __future__ import annotations

import contextlib
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

from ..acceptance import TwinApi
from ..envfile import read_env
from ..output import command, emit, session
from ..skill import MARKER_FILE, render_skill, skill_name
from ..workspace import Workspace
from .common import gate_inputs, timed
from .computeops import questions

log = Log("commands.skill")

HANDOVER_STAGE = 8
SKILL_DIR = "skill"
HANDOVER_REPORT = "reports/handover.md"


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


def read_question_states(workspace: Workspace, manifest: Manifest) -> list[dict[str, Any]] | None:
    """What each question may claim, from the twin's API when it answers; None otherwise,
    and the skill then says the states were not known when it was written."""
    contract = manifest.contract
    if contract is None or not contract.questions:
        return None
    try:
        with TwinApi(manifest, read_env(workspace.env_path)) as client:
            return questions(client)
    except (DtaError, OSError) as exc:
        log.warn(
            "the questions' states cannot be read from the API; the skill is written without them",
            error=type(exc).__name__,
            code=getattr(exc, "code", None),
        )
        return None


def write_skill(
    workspace: Workspace,
    manifest: Manifest,
    *,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render the skill and bring `<workspace>/skill/` in line with it.

    `questions` are the questions' states as the API reported them, recorded in the skill.
    """
    digest = manifest_hash(manifest)
    rendered = render_skill(manifest, manifest_hash=digest, questions=questions)
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
    manifest = workspace.load_manifest()
    result = write_skill(workspace, manifest, questions=read_question_states(workspace, manifest))
    changed = len(result["written"]) + len(result["removed"])
    text = (
        f"Skill {result['name']} written to {result['path']} ({changed} file(s) changed)."
        if changed
        else f"Skill {result['name']} at {result['path']} is current."
    )
    emit(ctx, result, text)


def install_directory(source: Path, destination: Path) -> None:
    """Copy `source` to `destination`; the new copy appears whole, in one rename.

    An earlier copy is moved aside first and removed afterwards, so a reader
    sees the earlier copy, no copy for an instant, or the new one, and never a
    mixture of the two.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{secrets.token_hex(4)}.tmp"
    shutil.copytree(source, staging)
    try:
        if not destination.exists():
            os.rename(staging, destination)
            return
        aside = destination.parent / f".{destination.name}.{secrets.token_hex(4)}.old"
        os.rename(destination, aside)
        try:
            os.rename(staging, destination)
        except OSError:
            os.rename(aside, destination)  # put the earlier copy back
            raise
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
    provisional: Annotated[
        bool,
        typer.Option(
            "--provisional",
            help="Install although an earlier gate has not passed; the handover gate then fails.",
        ),
    ] = False,
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
    base = _target_base(target, scope)
    outcomes = evaluate_gates(gate_inputs(workspace, manifest))
    unpassed = [o for o in outcomes if o.stage < HANDOVER_STAGE and not o.passed]
    if unpassed and not provisional:
        log.warn("install refused: earlier gates not passed", gates=[o.name for o in unpassed])
        raise CheckFailed(
            f"gate(s) {', '.join(str(o.stage) for o in unpassed)} have not passed; "
            "handover follows acceptance (--provisional installs the skill anyway)",
            code="handover.not_accepted",
            detail={"gates": [o.as_dict() for o in unpassed]},
        )
    destination = base / source.name
    with timed(log, "skill install", destination=str(destination)):
        install_directory(source, destination)
    report = workspace.root / HANDOVER_REPORT
    with workspace.exclusive():
        state = workspace.load_state()
        state.installed_skill = str(destination)
        workspace.save_state(state)
        workspace.record_gate(
            HANDOVER_STAGE,
            passed=not unpassed,
            subject_hash=subject_of(HANDOVER_STAGE, manifest),
            detail={
                "skill": source.name,
                "installed": str(destination),
                "not_passed": [o.name for o in unpassed],
            },
        )
        workspace.atomic_write(
            report, handover_text(workspace, manifest, destination, source.name, digest)
        )
    result = {
        "skill": source.name,
        "installed": str(destination),
        "manifest_hash": digest,
        "handover": str(report),
        "provisional": [o.name for o in unpassed],
    }
    text = f"Skill {source.name} installed at {destination}. Handover: {report}"
    if unpassed:
        text += f"\nProvisional: gate(s) {', '.join(o.name for o in unpassed)} have not passed."
    emit(ctx, result, text)
