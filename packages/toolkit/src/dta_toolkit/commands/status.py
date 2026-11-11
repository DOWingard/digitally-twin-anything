"""`dta status`: where the build stands. Read-only."""

from __future__ import annotations

from typing import Any

import typer
from dta_core.log import Log
from dta_core.manifest.checks import STAGE_NAMES, current_stage, evaluate_gates

from ..output import command, emit, session
from .common import gate_inputs

log = Log("commands.status")

_COMPLETE = "complete"


@command
def status(ctx: typer.Context) -> None:
    """Current stage, each gate's result and the number of gaps."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    inputs = gate_inputs(workspace, manifest)
    outcomes = evaluate_gates(inputs)
    stage = current_stage(outcomes)

    signature: dict[str, Any] | None = None
    if inputs.signatures:
        latest = inputs.signatures[-1]
        signature = {
            "mode": latest.mode,
            "approved_by": latest.approved_by,
            "signed_at": latest.signed_at.isoformat(),
            "current": latest.contract_hash == inputs.contract_hash,
        }
    twin = manifest.twin
    result = {
        "twin": {"id": twin.id if twin else None, "name": twin.name if twin else None},
        "stage": {"number": stage, "name": STAGE_NAMES.get(stage, _COMPLETE)},
        "gates": [outcome.as_dict() for outcome in outcomes],
        "gaps": len(inputs.gaps),
        "signature": signature,
    }
    log.info("status read", stage=result["stage"]["name"], gaps=result["gaps"])

    lines = [
        f"Twin: {result['twin']['id'] or 'not set'} ({result['twin']['name'] or 'no name'})",
        f"Stage: {stage if stage is not None else '-'} {result['stage']['name']}",
        f"Gaps: {result['gaps']}",
        "Gates:",
    ]
    for outcome in outcomes:
        reasons = "; ".join(outcome.reasons)
        lines.append(
            f"  {outcome.stage} {outcome.name:<10} {outcome.verdict.value:<13} {reasons}".rstrip()
        )
    if signature is not None:
        by = f" by {signature['approved_by']}" if signature["approved_by"] else ""
        state = "current" if signature["current"] else "outdated: the contract changed"
        lines.append(f"Signature: {signature['mode']}{by} at {signature['signed_at']} ({state})")
    else:
        lines.append("Signature: none")
    emit(ctx, result, "\n".join(lines))
