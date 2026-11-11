"""Findings and the registry of manifest checks.

A check is a small function over a `ManifestView` that yields `Issue`s. The
registry attaches the check's code and default severity, so a check cannot
report under someone else's code. New checks are added by decorating a
function with `@check(...)` in a module imported by this package.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...errors import DtaError
from ...log import Log
from ..models import Manifest
from .view import ManifestView

log = Log("manifest.checks")


class Severity(StrEnum):
    error = "error"  # the manifest cannot be built as it stands
    warning = "warning"  # suspicious, or not yet decidable; never blocks


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    path: str  # dotted manifest path the finding concerns
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class Issue:
    """What a check reports; the registry turns it into a `Finding`."""

    path: str
    message: str
    severity: Severity | None = None  # None: the check's default severity


CheckFunction = Callable[[ManifestView], Iterable[Issue]]


@dataclass(frozen=True)
class Check:
    code: str
    severity: Severity
    run: CheckFunction
    live_only: bool = False  # only meaningful for a twin that syncs repeatedly


CHECKS: list[Check] = []


def check(
    code: str, severity: Severity = Severity.error, *, live_only: bool = False
) -> Callable[[CheckFunction], CheckFunction]:
    """Register a check under `code`."""

    def register(func: CheckFunction) -> CheckFunction:
        if any(existing.code == code for existing in CHECKS):
            raise ValueError(f"check code registered twice: {code}")
        CHECKS.append(Check(code=code, severity=severity, run=func, live_only=live_only))
        return func

    return register


def run_checks(manifest: Manifest) -> list[Finding]:
    """Run every registered check; findings sorted by path, then code."""
    started = time.perf_counter()
    view = ManifestView(manifest)
    findings: list[Finding] = []
    for spec in CHECKS:
        if spec.live_only and not view.live:
            log.debug("check skipped: twin is not live", code=spec.code)
            continue
        try:
            for issue in spec.run(view):
                findings.append(
                    Finding(
                        code=spec.code,
                        severity=issue.severity or spec.severity,
                        path=issue.path,
                        message=issue.message,
                    )
                )
        except Exception as exc:
            # A crashing check is a defect, not an answer; never report a partial result.
            log.error("check crashed; validation aborted", exc=exc, code=spec.code)
            raise DtaError(
                f"check {spec.code} crashed: {type(exc).__name__}: {exc}",
                code="check.internal",
                detail={"check": spec.code},
            ) from exc
    findings.sort(key=lambda finding: (finding.path, finding.code, finding.message))
    errors = sum(1 for finding in findings if finding.severity == Severity.error)
    log.debug(
        "checks finished",
        checks=len(CHECKS),
        errors=errors,
        warnings=len(findings) - errors,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return findings


def errors_of(findings: Iterable[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.severity == Severity.error]
