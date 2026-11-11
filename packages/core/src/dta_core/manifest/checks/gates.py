"""The gate at the end of each build stage, and the current stage.

The current stage is the lowest stage whose gate has not passed. Some gates are
computed from the manifest and the recorded build state every time they are
asked; others are recorded by the command that evaluates them and count only
while the hash of what they examined (their subject) is unchanged. Gates live in
a registry, so one gate can be added or replaced without touching the others.

Recorded results and signatures are read through small protocols so this module
does not depend on how the caller stores build state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cached_property
from typing import Any, Protocol

from ...log import Log
from ..io import contract_hash, manifest_hash
from ..models import Manifest, Owner
from ..status import OPEN, FieldState, gaps, required_fields
from .findings import Finding, errors_of, run_checks

log = Log("manifest.gates")

STAGE_NAMES: dict[int, str] = {
    0: "intake",
    1: "contract",
    2: "access",
    3: "profiling",
    4: "design",
    5: "gaps",
    6: "build",
    7: "acceptance",
    8: "handover",
}


class Verdict(StrEnum):
    passed = "passed"
    failed = "failed"
    stale = "stale"  # recorded, but what it examined has changed since
    not_evaluated = "not_evaluated"  # nothing recorded yet


class RecordedGate(Protocol):
    passed: bool
    subject_hash: str | None


class SignatureRecord(Protocol):
    contract_hash: str
    mode: str
    approved_by: str | None
    signed_at: datetime


@dataclass(frozen=True)
class GateOutcome:
    stage: int
    name: str
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "result": self.verdict.value,
            "reasons": list(self.reasons),
            "detail": self.detail,
        }


class GateInputs:
    """Everything a gate may look at; derived values are computed once, on first use."""

    def __init__(
        self,
        manifest: Manifest,
        recorded: Mapping[str, RecordedGate] | None = None,
        signatures: Sequence[SignatureRecord] = (),
        extra_findings: Sequence[Finding] = (),
    ) -> None:
        self.manifest = manifest
        self.recorded = dict(recorded or {})
        self.signatures = list(signatures)
        # Findings that need more than the manifest, such as those against its ontology.
        self.extra_findings = list(extra_findings)

    @cached_property
    def findings(self) -> list[Finding]:
        found = [*run_checks(self.manifest), *self.extra_findings]
        found.sort(key=lambda finding: (finding.path, finding.code, finding.message))
        return found

    @cached_property
    def errors(self) -> list[Finding]:
        return errors_of(self.findings)

    @cached_property
    def gaps(self) -> list[FieldState]:
        return gaps(self.manifest)

    @cached_property
    def manifest_hash(self) -> str:
        return manifest_hash(self.manifest)

    @cached_property
    def contract_hash(self) -> str:
        return contract_hash(self.manifest)


Evaluate = Callable[[GateInputs], GateOutcome]
Subject = Callable[[Manifest], str]


@dataclass(frozen=True)
class Gate:
    stage: int
    name: str
    evaluate: Evaluate
    # For a recorded gate: the hash its record must still match. None when no
    # subject has been defined yet; then a recorded pass counts as recorded.
    subject: Subject | None = None
    recorded: bool = False


GATES: dict[int, Gate] = {}


def _outcome(stage: int, reasons: list[str], **detail: Any) -> GateOutcome:
    verdict = Verdict.failed if reasons else Verdict.passed
    return GateOutcome(stage, STAGE_NAMES[stage], verdict, tuple(reasons), detail)


def computed_gate(stage: int) -> Callable[[Evaluate], Evaluate]:
    """Register a gate that is evaluated from the manifest and state every time."""

    def register(func: Evaluate) -> Evaluate:
        GATES[stage] = Gate(stage=stage, name=STAGE_NAMES[stage], evaluate=func)
        return func

    return register


def recorded_gate(stage: int, subject: Subject | None = None) -> Gate:
    """Register a gate whose result is recorded by the command that evaluates it."""
    name = STAGE_NAMES[stage]

    def evaluate(inputs: GateInputs) -> GateOutcome:
        record = inputs.recorded.get(name)
        if record is None:
            return GateOutcome(stage, name, Verdict.not_evaluated, ("not evaluated yet",))
        if subject is not None and record.subject_hash != subject(inputs.manifest):
            return GateOutcome(
                stage, name, Verdict.stale, ("the manifest changed since it was recorded",)
            )
        if record.passed:
            return GateOutcome(stage, name, Verdict.passed)
        return GateOutcome(stage, name, Verdict.failed, ("recorded as failed",))

    gate = Gate(stage=stage, name=name, evaluate=evaluate, subject=subject, recorded=True)
    GATES[stage] = gate
    return gate


@computed_gate(0)
def _intake(inputs: GateInputs) -> GateOutcome:
    manifest = inputs.manifest
    reasons = []
    if manifest.twin is None or not manifest.twin.id:
        reasons.append("twin.id has no value")
    if manifest.contract is None or not manifest.contract.purpose:
        reasons.append("contract.purpose has no value")
    sources = manifest.sources or {}
    if not sources:
        reasons.append("no source is declared")
    for sid, source in sorted(sources.items()):
        for attribute in ("connector", "location"):
            if not getattr(source, attribute):
                reasons.append(f"sources.{sid}.{attribute} has no value")
    return _outcome(0, reasons)


def _waits_for_profiling(path: str) -> bool:
    parts = path.split(".")
    if len(parts) < 4 or parts[:2] != ["contract", "questions"]:
        return False
    return parts[3] in ("tolerance", "staleness")


@computed_gate(1)
def _contract(inputs: GateInputs) -> GateOutcome:
    if inputs.manifest.contract is None:
        return _outcome(1, ["contract has no value"])
    open_paths = [
        state.path
        for state in required_fields(inputs.manifest)
        if state.owner == Owner.user
        and state.path.startswith("contract.")
        and state.status in OPEN
        and not _waits_for_profiling(state.path)
    ]
    return _outcome(1, [f"{path} is not confirmed" for path in open_paths])


recorded_gate(2)


@computed_gate(3)
def _profiling(inputs: GateInputs) -> GateOutcome:
    sources = inputs.manifest.sources or {}
    reasons = [] if sources else ["no source is declared"]
    reasons += [
        f"sources.{sid} has no profile"
        for sid, source in sorted(sources.items())
        if source.profile is None
    ]
    return _outcome(3, reasons)


recorded_gate(4, subject=manifest_hash)


@computed_gate(5)
def _gaps(inputs: GateInputs) -> GateOutcome:
    reasons = []
    if inputs.gaps:
        reasons.append(f"{len(inputs.gaps)} gap(s) remain")
    if inputs.errors:
        reasons.append(f"{len(inputs.errors)} error finding(s)")
    current = inputs.contract_hash
    matching = [sig for sig in inputs.signatures if sig.contract_hash == current]
    detail: dict[str, Any] = {}
    if matching:
        latest = matching[-1]
        detail["signature"] = {
            "mode": latest.mode,
            "approved_by": latest.approved_by,
            "signed_at": latest.signed_at.isoformat(),
        }
    elif inputs.signatures:
        reasons.append("the contract changed since it was signed")
    else:
        reasons.append("the contract is not signed")
    return _outcome(5, reasons, **detail)


for _stage in (6, 7, 8):
    recorded_gate(_stage)


def subject_of(stage: int, manifest: Manifest) -> str | None:
    """The subject hash a command must record for a recorded gate."""
    gate = GATES[stage]
    return gate.subject(manifest) if gate.subject is not None else None


def evaluate_gates(inputs: GateInputs) -> list[GateOutcome]:
    outcomes = [GATES[stage].evaluate(inputs) for stage in sorted(GATES)]
    log.debug(
        "gates evaluated",
        results={outcome.name: outcome.verdict.value for outcome in outcomes},
    )
    return outcomes


def current_stage(outcomes: Sequence[GateOutcome]) -> int | None:
    """Lowest stage whose gate has not passed; None once every gate has passed."""
    for outcome in sorted(outcomes, key=lambda item: item.stage):
        if not outcome.passed:
            return outcome.stage
    return None
