"""Validation of a manifest: checks, stage gates and the contract summary.

Importing this package registers every check. Findings are sorted by path and
code, so the order in which checks run does not show in the output.
"""

from . import (  # noqa: F401 - registers checks
    consistency,
    failsafe,
    feasibility,
    grid,
    loading,
    references,
    version,
)
from .against_ontology import ontology_findings
from .findings import CHECKS, Check, Finding, Issue, Severity, check, errors_of, run_checks
from .gates import (
    GATES,
    STAGE_NAMES,
    Gate,
    GateInputs,
    GateOutcome,
    Verdict,
    computed_gate,
    current_stage,
    evaluate_gates,
    recorded_gate,
    subject_of,
)
from .summary import OntologyTypes, read_ontology_types, render_summary, summary_hash
from .view import ManifestView

__all__ = [
    "CHECKS",
    "GATES",
    "STAGE_NAMES",
    "Check",
    "Finding",
    "Gate",
    "GateInputs",
    "GateOutcome",
    "Issue",
    "ManifestView",
    "OntologyTypes",
    "Severity",
    "Verdict",
    "check",
    "computed_gate",
    "current_stage",
    "errors_of",
    "evaluate_gates",
    "ontology_findings",
    "read_ontology_types",
    "recorded_gate",
    "render_summary",
    "run_checks",
    "subject_of",
    "summary_hash",
]
