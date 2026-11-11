"""Helpers shared by the commands: gate inputs, the summary, and check failures."""

from __future__ import annotations

from collections.abc import Sequence
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

from ..output import session
from ..workspace import Workspace

log = Log("commands")

ONTOLOGY_UNUSABLE = "ontology.unusable"


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
