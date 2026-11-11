"""Checks of a live twin: its scheduler runs, its latest run succeeded, no source is
failing, and every question that states a staleness has data that fresh.

For a static twin each check does not apply. Freshness is measured by the API on
the twin's own clock; these checks read it and never look at the wall.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import Tier

from .readiness import API, Context
from .result import CheckResult, described, problem_of

log = Log("acceptance.live")

SCHEDULER_SERVICE = "scheduler"
SCHEDULER_KEY_PREFIX = "sync:"
JOBS_LISTED = 200
JOBS_SHOWN = 5
SUCCEEDED = "succeeded"
FAILING = "failing"
RUNNING = "running"
NOT_LIVE = "contract.tier is static: the twin does not sync"

HEALTH = f"{API}/meta/health"
SOURCES = f"{API}/sync/sources"
JOBS = f"{API}/lifecycle/jobs"

SCHEDULER_REMEDY = (
    "Start the scheduler (dta up scheduler, or dta build) and read its logs (dta logs scheduler)."
)
RUNS_REMEDY = (
    "Read the run's error (GET /api/v1/sync/runs/{uid}) and the failing sources "
    "(GET /api/v1/sync/sources), mend them, then start a run (dta sync run)."
)
SOURCES_REMEDY = (
    "Read last_error of each failing source (dta sync status, or GET /api/v1/sync/sources), "
    "mend the source or the variables it needs in .env, then start a run (dta sync run)."
)
FRESHNESS_REMEDY = (
    "Look at GET /api/v1/sync/sources (or dta sync status) for the source that is failing or "
    "behind; start a run (dta sync run) and check that the scheduler runs; compare sync.interval "
    "with each question's staleness: the interval and the source's refresh period together "
    "must stay within it."
)


def is_live(manifest: Manifest) -> bool:
    return manifest.contract is not None and manifest.contract.tier == Tier.live


def _not_applicable(ident: str, examined: str) -> CheckResult:
    return CheckResult(ident, examined, True, {"applicable": False, "reason": NOT_LIVE})


def _read(ctx: Context, path: str, evidence: dict[str, Any], params: dict | None = None):
    """The JSON body of a 200 answer, or None with the failing request in the evidence."""
    response = ctx.api.get(path, params=params)
    if response.status_code != 200:
        evidence["failing_request"] = described("GET", path, params)
        evidence["response"] = problem_of(response)
        return None
    evidence.setdefault("requests", []).append(described("GET", path, params))
    return response.json()


def _sync_of(ctx: Context, evidence: dict[str, Any]) -> tuple[dict | None, dict | None]:
    """(health, its `sync` section); None where it could not be read."""
    health = _read(ctx, HEALTH, evidence)
    if health is None:
        return None, None
    state = health.get("sync")
    if state is None:
        evidence["reason"] = "health has no sync section: the manifest sets no sync interval"
    return health, state


# -- sync.scheduler ---------------------------------------------------------------


def sync_scheduler(ctx: Context, services: Callable[[], list[dict[str, Any]]]) -> CheckResult:
    examined = "the stack's scheduler service is running"
    if not is_live(ctx.manifest):
        return _not_applicable("sync.scheduler", examined)
    evidence: dict[str, Any] = {}
    row = next((r for r in services() if r.get("service") == SCHEDULER_SERVICE), None)
    evidence["service"] = (
        {"state": row.get("state"), "health": row.get("health"), "container": row.get("container")}
        if row
        else None
    )
    listing = _read(ctx, JOBS, evidence, {"kind": "sync", "limit": JOBS_LISTED})
    if listing is not None:
        jobs = [
            job
            for job in listing.get("data") or []
            if str(job.get("idempotency_key") or "").startswith(SCHEDULER_KEY_PREFIX)
        ]
        evidence["scheduler_jobs"] = len(jobs)
        evidence["latest_scheduler_jobs"] = [
            {
                "id": job.get("id"),
                "state": job.get("state"),
                "idempotency_key": job.get("idempotency_key"),
                "sequence": job.get("sequence"),
                "run_status": (job.get("result") or {}).get("status"),
            }
            for job in jobs[:JOBS_SHOWN]
        ]
    passed = row is not None and row.get("state") == RUNNING
    if not passed:
        evidence["reason"] = (
            "the stack has no scheduler container"
            if row is None
            else f"the scheduler container is {row.get('state')}"
        )
        log.warn("scheduler check failed", evidence=evidence)
    return CheckResult("sync.scheduler", examined, passed, evidence, SCHEDULER_REMEDY)


# -- sync.runs ----------------------------------------------------------------------


def sync_runs(ctx: Context) -> CheckResult:
    examined = "the latest sync run recorded in the twin (sync.last_run of /meta/health) succeeded"
    if not is_live(ctx.manifest):
        return _not_applicable("sync.runs", examined)
    evidence: dict[str, Any] = {}
    _health, state = _sync_of(ctx, evidence)
    passed = False
    if state is not None:
        last = state.get("last_run")
        evidence["last_run"] = last
        if last is None:
            evidence["reason"] = "no sync run is recorded in the twin"
        else:
            passed = last.get("status") == SUCCEEDED
            if not passed:
                evidence["reason"] = f"the latest run {last.get('uid')} is {last.get('status')}"
    if not passed:
        log.warn("sync runs check failed", evidence=evidence)
    return CheckResult("sync.runs", examined, passed, evidence, RUNS_REMEDY)


# -- sync.sources ---------------------------------------------------------------------


def sync_sources(ctx: Context) -> CheckResult:
    examined = "no source of /sync/sources is failing"
    if not is_live(ctx.manifest):
        return _not_applicable("sync.sources", examined)
    evidence: dict[str, Any] = {}
    listing = _read(ctx, SOURCES, evidence)
    passed = False
    if listing is not None:
        sources = [
            {
                "id": source.get("id"),
                "status": source.get("status"),
                "watermark": source.get("watermark"),
                "freshness_s": source.get("freshness_s"),
                "last_error": source.get("last_error"),
            }
            for source in listing.get("data") or []
        ]
        failing = [source["id"] for source in sources if source["status"] == FAILING]
        evidence.update({"sources": sources, "failing": failing, "clock": listing.get("clock")})
        passed = not failing
        if failing:
            evidence["reason"] = f"failing: {', '.join(str(sid) for sid in failing)}"
    if not passed:
        log.warn("sync sources check failed", evidence=evidence)
    return CheckResult("sync.sources", examined, passed, evidence, SOURCES_REMEDY)


# -- sync.freshness -------------------------------------------------------------------


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    whole = int(round(seconds))
    if abs(whole) < 120:
        return f"{whole} s"
    minutes, _ = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{whole} s ({hours} h {minutes} min)"
    days, hours = divmod(hours, 24)
    return f"{whole} s ({days} d {hours} h)"


def _why_unmeasured(clock: dict[str, Any], state: dict[str, Any]) -> str:
    if not clock.get("available"):
        return f"the twin's clock ({clock.get('kind')}) cannot be read, so freshness is not known"
    unread = [
        question["id"] for question in state.get("questions") or [] if question.get("fresh") is None
    ]
    behind = sorted(
        str(source.get("id"))
        for source in state.get("sources") or []
        if source.get("freshness_s") is None
    )
    if behind:
        return (
            f"nothing has been read yet from {', '.join(behind)}, which "
            f"{', '.join(unread) or 'the questions'} are answered from"
        )
    return "the API could not measure freshness"


def sync_freshness(ctx: Context) -> CheckResult:
    examined = (
        "every question that states a staleness has data no older than it, from the sources it "
        "is answered from, on the twin's clock (sync.fresh of /meta/health)"
    )
    if not is_live(ctx.manifest):
        return _not_applicable("sync.freshness", examined)
    evidence: dict[str, Any] = {}
    health, state = _sync_of(ctx, evidence)
    if health is None or state is None:
        log.warn("freshness check failed", evidence=evidence)
        return CheckResult("sync.freshness", examined, False, evidence, FRESHNESS_REMEDY)
    questions = state.get("questions") or []
    clock = health.get("clock") or {}
    evidence.update(
        {
            "clock": clock,
            "interval": state.get("interval"),
            "staleness": state.get("staleness"),
            "freshness_s": state.get("freshness_s"),
            "fresh": state.get("fresh"),
            "questions": questions,
            "sources": [
                {
                    "id": source.get("id"),
                    "status": source.get("status"),
                    "watermark": source.get("watermark"),
                    "freshness_s": source.get("freshness_s"),
                }
                for source in state.get("sources") or []
            ],
        }
    )
    if not questions:
        evidence.update({"applicable": False, "reason": "no question states a staleness"})
        return CheckResult("sync.freshness", examined, True, evidence)
    fresh = state.get("fresh")
    passed = fresh is True
    if fresh is None:
        evidence["reason"] = _why_unmeasured(clock, state)
    elif not passed:
        stale = [question for question in questions if question.get("fresh") is False]
        evidence["reason"] = "; ".join(
            f"question {question.get('id')}: data {_age(question.get('freshness_s'))} old, "
            f"limit {question.get('staleness')}, behind: source {question.get('source')}"
            for question in stale
        )
    if not passed:
        log.warn("freshness check failed", reason=evidence.get("reason"), fresh=fresh)
    return CheckResult("sync.freshness", examined, passed, evidence, FRESHNESS_REMEDY)
