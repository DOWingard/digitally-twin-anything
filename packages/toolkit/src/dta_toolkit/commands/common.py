"""Helpers shared by the commands: gate inputs, the summary, check failures, the stack's
environment and keeping its values out of everything printed."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import typer
from dta_core.errors import CheckFailed
from dta_core.log import Log
from dta_core.manifest import FieldState, Manifest
from dta_core.manifest.checks import (
    Finding,
    GateInputs,
    OntologyTypes,
    Severity,
    ontology_findings,
    read_ontology_types,
    render_summary,
)
from dta_core.ontology import OntologyError, load_ontology, type_signature

from ..envfile import read_env, scrub
from ..output import session
from ..stack import Stack
from ..workspace import Workspace

log = Log("commands")

ONTOLOGY_UNUSABLE = "ontology.unusable"
ENV_MISSING = "env.missing"


def require_complete_env(stack: Stack) -> dict[str, str]:
    """The workspace's `.env` values, or a check failure naming every required name it lacks.

    Compose interpolates the whole stack file, so even a command that starts
    nothing but a one-off container needs every variable the file names.
    """
    values = read_env(stack.env_file)
    absent = [name for name in stack.plan.required_env() if not values.get(name)]
    if absent:
        log.error("the stack's environment is incomplete", missing=absent)
        raise CheckFailed(
            f"{stack.env_file.name} does not set {', '.join(absent)}",
            code=ENV_MISSING,
            detail={"missing": absent, "env_file": str(stack.env_file)},
        )
    return values


def scrub_values(value: Any, env: Mapping[str, str]) -> Any:
    """`value` with every value of the environment file masked in every string, at any depth.

    Runtime documents may quote a resolved location or a connection error, and
    such text can carry a value that came from `.env`.
    """
    if isinstance(value, str):
        return scrub(value, env)
    if isinstance(value, dict):
        return {key: scrub_values(item, env) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_values(item, env) for item in value]
    return value


@contextlib.contextmanager
def timed(logger: Log, what: str, **context: Any) -> Iterator[dict[str, Any]]:
    """Log the start and the end of a long step with its duration; failures with their context.

    The yielded dictionary may be filled with fields to log at the end.
    """
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    logger.debug(f"{what} started", **context)
    try:
        yield extra
    except Exception as exc:
        logger.error(
            f"{what} failed",
            exc=exc,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            **context,
        )
        raise
    logger.info(
        f"{what} finished",
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        **context,
        **extra,
    )


def ontology_checks(workspace: Workspace, manifest: Manifest) -> list[Finding]:
    """What in the manifest does not fit its ontology file; nothing while the file is unwritten."""
    relative = manifest.ontology.path if manifest.ontology else None
    if not relative:
        return []
    path = workspace.root / relative
    if not path.is_file():
        log.debug("ontology file not written yet; manifest not checked against it", path=relative)
        return []
    try:
        ontology = load_ontology(path)
    except OntologyError as exc:
        log.warn("ontology file cannot be used", path=relative, code=exc.code, detail=exc.detail)
        return [
            Finding(
                code=ONTOLOGY_UNUSABLE,
                severity=Severity.error,
                path="ontology.path",
                message=f"{relative}: {exc}",
            )
        ]
    return ontology_findings(manifest, ontology, type_signature(ontology))


def all_findings(workspace: Workspace, manifest: Manifest) -> list[Finding]:
    """Findings of the manifest's own checks and of those against its ontology, in one order."""
    return gate_inputs(workspace, manifest).findings


def ontology_path(workspace: Workspace, manifest: Manifest) -> Path | None:
    """Where the manifest's ontology file is, or None when the manifest names none."""
    relative = manifest.ontology.path if manifest.ontology else None
    return workspace.root / relative if relative else None


def gate_inputs(workspace: Workspace, manifest: Manifest) -> GateInputs:
    state = workspace.load_state()
    return GateInputs(
        manifest,
        recorded=state.gates,
        signatures=state.signatures,
        extra_findings=ontology_checks(workspace, manifest),
    )


def ontology_types(workspace: Workspace, manifest: Manifest) -> OntologyTypes | None:
    """Type names from the manifest's LinkML file, or None while it is not written."""
    relative = manifest.ontology.path if manifest.ontology else None
    if not relative:
        return None
    path = workspace.root / relative
    if not path.is_file():
        log.info("ontology file not written yet; summary leaves types out", path=relative)
        return None
    return read_ontology_types(path)


def build_summary(workspace: Workspace, manifest: Manifest) -> str:
    return render_summary(manifest, ontology_types(workspace, manifest))


def resolve_in_workspace(workspace: Workspace, path: Path) -> Path:
    """A path given on the command line, taken relative to the workspace root."""
    return path if path.is_absolute() else workspace.root / path


def findings_text(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["No findings."]
    width = max(len(finding.code) for finding in findings)
    return [
        f"{finding.severity.value:<8} {finding.code:<{width}}  {finding.path}: {finding.message}"
        for finding in findings
    ]


def gaps_text(gaps: Sequence[FieldState]) -> list[str]:
    if not gaps:
        return ["No gaps."]
    width = max(len(state.path) for state in gaps)
    return [
        f"{state.owner.value:<6} {state.path:<{width}}  {state.status.value:<9} {state.description}"
        for state in gaps
    ]


def gap_dict(state: FieldState) -> dict[str, Any]:
    return {"path": state.path, "status": state.status.value, "description": state.description}


def fail_check(
    ctx: typer.Context, code: str, message: str, result: dict[str, Any], text: str
) -> NoReturn:
    """Report a check whose answer is no: the result travels as the error's detail.

    In text mode the human-readable result is printed first, so the reader sees
    what failed and not only that it failed.
    """
    if not session(ctx).json_output:
        typer.echo(text)
    raise CheckFailed(message, code=code, detail=result)
