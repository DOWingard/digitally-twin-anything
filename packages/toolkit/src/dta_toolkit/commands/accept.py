"""`dta accept`: check the built twin against its contract and write the report.

All checks run every time. Exactly one report is left: the acceptance report
when every check passed, otherwise the deficit report.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import manifest_hash
from dta_core.manifest.checks import evaluate_gates, subject_of
from dta_core.ontology import OntologyError, load_ontology

from ..acceptance import DEFAULT_EXAMPLES, DEFAULT_SEED, render_markdown, run_checks
from ..output import command, emit, session
from ..stack import Stack
from ..workspace import now
from .common import (
    fail_check,
    gate_inputs,
    ontology_path,
    require_complete_env,
    scrub_values,
    timed,
)
from .skill import rendered_hash, skill_dir

log = Log("commands.accept")

BUILD_STAGE = 6
ACCEPTANCE_STAGE = 7
REPORTS = "reports"
_PASSED = ("acceptance.json", "acceptance.md")
_FAILED = ("deficit.json", "deficit.md")


def _text(document: dict[str, Any], report: str) -> str:
    lines = []
    for check in document["checks"]:
        lines.append(
            f"{'ok  ' if check['passed'] else 'FAIL'} {check['id']:<17} {check['examined']}"
        )
        if not check["passed"] and check.get("remedy"):
            lines.append(f"     {check['remedy']}")
    lines.append(f"Report: {report}")
    return "\n".join(lines)


@command
def accept(
    ctx: typer.Context,
    examples: Annotated[
        int,
        typer.Option("--examples", min=1, help="Generated requests per API operation."),
    ] = DEFAULT_EXAMPLES,
    seed: Annotated[
        int, typer.Option("--seed", help="Seed of the generated requests.")
    ] = DEFAULT_SEED,
) -> None:
    """Run every acceptance check against the running twin. Records the acceptance gate."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    digest = manifest_hash(manifest)
    built = next(
        outcome
        for outcome in evaluate_gates(gate_inputs(workspace, manifest))
        if outcome.stage == BUILD_STAGE
    )
    if not built.passed:
        log.warn(
            "accept refused: the twin is not built for this manifest", verdict=built.verdict.value
        )
        raise CheckFailed(
            "the twin is not built for the current manifest; run `dta build`",
            code="accept.not_built",
            detail={"gate": built.as_dict()},
        )
    path = ontology_path(workspace, manifest)
    if path is None or not path.is_file():
        raise DtaError("the manifest's ontology file is missing", code="ontology.missing")
    try:
        ontology = load_ontology(path)
    except OntologyError as exc:
        raise CheckFailed(
            f"the ontology file cannot be used: {exc.message}", code="ontology.unusable"
        ) from exc
    stack = Stack(workspace, manifest)
    values = require_complete_env(stack)
    with timed(log, "acceptance", examples=examples, seed=seed):
        run = run_checks(
            manifest,
            ontology,
            values,
            manifest_hash=digest,
            rendered_skill_hash=rendered_hash(skill_dir(workspace, manifest)),
            examples=examples,
            seed=seed,
        )
    document = scrub_values(
        {
            "twin_id": manifest.twin.id,
            "manifest_hash": digest,
            "at": now().isoformat(),
            "passed": run.passed,
            "failed": run.failed,
            "checks": [check.as_dict() for check in run.checks],
        },
        stack.secret_values(),
    )
    keep, drop = (_PASSED, _FAILED) if run.passed else (_FAILED, _PASSED)
    reports = workspace.root / REPORTS
    with workspace.exclusive():
        for name in drop:
            workspace.remove(reports / name)
        workspace.atomic_write(
            reports / keep[0], json.dumps(document, indent=2, default=str) + "\n"
        )
        workspace.atomic_write(reports / keep[1], render_markdown(document))
        workspace.record_gate(
            ACCEPTANCE_STAGE,
            passed=run.passed,
            subject_hash=subject_of(ACCEPTANCE_STAGE, manifest),
            detail={"failed": run.failed, "report": f"{REPORTS}/{keep[0]}"},
        )
    result = {**document, "report": str(reports / keep[0])}
    text = _text(document, str(reports / keep[1]))
    if not run.passed:
        log.warn("acceptance failed", failed=run.failed)
        fail_check(
            ctx,
            "accept.failed",
            f"{len(run.failed)} check(s) failed: {', '.join(run.failed)}",
            result,
            text,
        )
    log.info("twin accepted", checks=len(run.checks))
    emit(ctx, result, text)
