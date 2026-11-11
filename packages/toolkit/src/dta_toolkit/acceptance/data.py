"""Checks of the loaded data: no row set aside, and every observed or asked-for property filled."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from dta_core.log import Log
from dta_core.manifest import Manifest

from .readiness import API, Context
from .result import CheckResult, described, problem_of

log = Log("acceptance.data")

RUN_TYPE = "TwinSyncRun"


def data_constraints(ctx: Context) -> CheckResult:
    evidence: dict[str, Any] = {}
    params = {"sort": "-started_at", "limit": 1}
    request = described("GET", f"{API}/objects/{RUN_TYPE}", params)
    response = ctx.api.get(f"{API}/objects/{RUN_TYPE}", params=params)
    passed = False
    if response.status_code != 200:
        evidence.update({"failing_request": request, "response": problem_of(response)})
    else:
        rows = response.json().get("data") or []
        if not rows:
            evidence.update({"request": request, "reason": "no load run is recorded in the twin"})
        else:
            run = rows[0]
            evidence.update(
                {
                    "request": request,
                    "run": run.get("uid"),
                    "status": run.get("status"),
                    "rows_read": run.get("rows_read"),
                    "rows_written": run.get("rows_written"),
                    "rows_rejected": run.get("rows_rejected"),
                    "rejects_location": run.get("rejects_location"),
                }
            )
            passed = not run.get("rows_rejected")
    if not passed:
        log.warn("constraint check failed", evidence=evidence)
    return CheckResult(
        id="data.constraints",
        examined="the last load run recorded in the twin set no row aside",
        passed=passed,
        evidence=evidence,
        remedy="Read the rows set aside (rejects_location in the artifacts volume, and the API's "
        "logs), correct the source or the mapping, then dta build --from load.",
    )


def covered_properties(manifest: Manifest) -> list[str]:
    """Every `Type.property` the contract observes or asks about."""
    found: dict[str, None] = {}
    contract = manifest.contract
    if contract is not None:
        if contract.boundary is not None:
            for ref in contract.boundary.observations or []:
                found.setdefault(ref)
        for _qid, question in sorted((contract.questions or {}).items()):
            for ref in question.targets or []:
                found.setdefault(ref)
    return list(found)


def data_coverage(ctx: Context) -> CheckResult:
    evidence: dict[str, Any] = {}
    refs = covered_properties(ctx.manifest)
    filled, empty, failing = [], [], []
    for ref in refs:
        type_name, _, prop = ref.partition(".")
        params = {"where": f"{prop}:notnull", "columns": prop, "limit": 1}
        response = ctx.api.get(f"{API}/objects/{quote(type_name, safe='')}", params=params)
        if response.status_code != 200:
            failing.append(
                {
                    "property": ref,
                    "request": described("GET", f"{API}/objects/{type_name}", params),
                    "response": problem_of(response),
                }
            )
        elif response.json().get("data"):
            filled.append(ref)
        else:
            empty.append(ref)
    evidence.update({"properties": len(refs), "filled": filled, "empty": empty, "failing": failing})
    passed = not empty and not failing
    if not passed:
        log.warn("coverage check failed", empty=empty, failing=len(failing))
    return CheckResult(
        id="data.coverage",
        examined="every property the contract observes or asks about has a value at head",
        passed=passed,
        evidence=evidence,
        remedy="Map a source column to each empty property, or take it out of "
        "contract.boundary.observations and the questions' targets; then build again.",
    )
