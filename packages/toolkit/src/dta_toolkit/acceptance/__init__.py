"""The checks of `dta accept`: a built twin against its contract, all checks every time.

Every check runs whatever the others found, and the report lists each with
what it examined, whether it passed, its evidence and what to change.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.schema import Ontology

from .client import ApiUnavailable, TwinApi, probe_health, service_url
from .contract import DEFAULT_EXAMPLES, DEFAULT_SEED, api_contract
from .data import data_constraints, data_coverage
from .endpoints import SKILL_API_FILE, skill_endpoints
from .live import sync_freshness, sync_runs, sync_scheduler, sync_sources
from .readiness import (
    Context,
    readiness_r1,
    readiness_r2,
    readiness_r3,
    readiness_r4,
    readiness_r5,
    smoke,
)
from .result import CheckResult

__all__ = [
    "DEFAULT_EXAMPLES",
    "DEFAULT_SEED",
    "SKILL_API_FILE",
    "AcceptanceRun",
    "ApiUnavailable",
    "CheckResult",
    "Context",
    "SkillText",
    "TwinApi",
    "probe_health",
    "run_checks",
    "service_url",
    "smoke",
]

log = Log("acceptance")

_UI_ATTEMPTS = 3


@dataclass(frozen=True)
class SkillText:
    """The generated skill's endpoint list as acceptance reads it."""

    # Text of the skill's API reference; None when no skill was generated.
    api_reference: str | None
    # Where it was read, relative to the workspace, as reports show it.
    where: str


@dataclass
class AcceptanceRun:
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed(self) -> list[str]:
        return [check.id for check in self.checks if not check.passed]


def ui_reachable(manifest: Manifest, values: Mapping[str, str]) -> CheckResult:
    examined = "GET /healthz on the UI port answers 200"
    if not manifest.interfaces.ui:
        return CheckResult(
            "ui.reachable",
            examined,
            True,
            {"applicable": False, "reason": "interfaces.ui is false"},
        )
    try:
        status = probe_health(
            service_url(values, manifest.interfaces.ui_port_env), attempts=_UI_ATTEMPTS
        )
    except ApiUnavailable as exc:
        return CheckResult(
            "ui.reachable",
            examined,
            False,
            {"failing_request": "GET /healthz (UI)", "error": exc.message},
            "Start the UI (dta up ui) and read its logs (dta logs ui).",
        )
    return CheckResult(
        "ui.reachable",
        examined,
        status == 200,
        {"request": "GET /healthz (UI)", "status": status},
        "Start the UI (dta up ui) and read its logs (dta logs ui).",
    )


def skill_current(rendered: str | None, manifest_hash: str) -> CheckResult:
    return CheckResult(
        "skill.current",
        "the generated skill was rendered from the current manifest",
        rendered == manifest_hash,
        {"skill_manifest_hash": rendered, "manifest_hash": manifest_hash},
        "Run dta generate skill (or dta build) for the current manifest.",
    )


def _guarded(ident: str, run: Callable[[], CheckResult]) -> CheckResult:
    """Run one check; an error inside it fails that check and no other."""
    started = time.perf_counter()
    try:
        result = run()
    except DtaError as exc:
        log.error("acceptance check could not run", exc=exc, check=ident, code=exc.code)
        result = CheckResult(
            ident,
            "the check could not run",
            False,
            {"error": exc.as_dict()},
            "Make sure the stack is up (dta up) and read the API's logs (dta logs api).",
        )
    except Exception as exc:  # noqa: BLE001 - a crash inside one check must not hide the others
        log.error("acceptance check crashed", exc=exc, check=ident)
        result = CheckResult(
            ident,
            "the check crashed",
            False,
            {"error": f"{type(exc).__name__}: {exc}"},
            "Read the toolkit's log in .dta/logs/toolkit.log.",
        )
    result.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
    log.info(
        "acceptance check done", check=ident, passed=result.passed, duration_ms=result.duration_ms
    )
    return result


def run_checks(
    manifest: Manifest,
    ontology: Ontology,
    values: Mapping[str, str],
    *,
    manifest_hash: str,
    rendered_skill_hash: str | None,
    skill: SkillText,
    services: Callable[[], list[dict[str, Any]]],
    examples: int = DEFAULT_EXAMPLES,
    seed: int = DEFAULT_SEED,
) -> AcceptanceRun:
    """Every check of acceptance, in a fixed order.

    `services` gives the state of the stack's containers when a check asks for it.
    """
    with TwinApi(manifest, values) as api:
        ctx = Context(api=api, manifest=manifest, ontology=ontology)
        plan: list[tuple[str, Callable[[], CheckResult]]] = [
            ("api.contract", lambda: api_contract(api, examples, seed)),
            ("readiness.R1", lambda: readiness_r1(ctx)),
            ("readiness.R2", lambda: readiness_r2(ctx)),
            ("readiness.R3", lambda: readiness_r3(ctx)),
            ("readiness.R4", lambda: readiness_r4(ctx)),
            ("readiness.R5", lambda: readiness_r5(ctx)),
            ("data.constraints", lambda: data_constraints(ctx)),
            ("data.coverage", lambda: data_coverage(ctx)),
            ("ui.reachable", lambda: ui_reachable(manifest, values)),
            ("skill.current", lambda: skill_current(rendered_skill_hash, manifest_hash)),
            ("skill.endpoints", lambda: skill_endpoints(api, skill.api_reference, skill.where)),
            ("sync.scheduler", lambda: sync_scheduler(ctx, services)),
            ("sync.runs", lambda: sync_runs(ctx)),
            ("sync.sources", lambda: sync_sources(ctx)),
            ("sync.freshness", lambda: sync_freshness(ctx)),
        ]
        checks = [_guarded(ident, run) for ident, run in plan]
    return AcceptanceRun(checks=checks)


def render_markdown(document: dict[str, Any]) -> str:
    """The acceptance or deficit report for a person."""
    title = "Acceptance" if document["passed"] else "Deficit"
    verdict = (
        "every check passed" if document["passed"] else f"failed: {', '.join(document['failed'])}"
    )
    lines = [
        f"# {title} report: {document['twin_id']}",
        "",
        f"- Manifest hash: `{document['manifest_hash']}`",
        f"- Checked at: {document['at']}",
        f"- Result: {verdict}",
        "",
        "| Check | Passed | Examined |",
        "|---|---|---|",
    ]
    for check in document["checks"]:
        lines.append(
            f"| {check['id']} | {'yes' if check['passed'] else 'NO'} | {check['examined']} |"
        )
    for check in document["checks"]:
        lines += ["", f"## {check['id']}: {'passed' if check['passed'] else 'failed'}", ""]
        if not check["passed"] and check.get("remedy"):
            lines += [f"What to change: {check['remedy']}", ""]
        lines.append("Evidence:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(check["evidence"], indent=2, default=str, ensure_ascii=False))
        lines.append("```")
    return "\n".join(lines) + "\n"
