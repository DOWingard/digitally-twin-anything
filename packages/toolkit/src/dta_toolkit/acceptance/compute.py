"""Checks of what the twin computes: each question answers in time, no question is
measured outside its tolerance, every model specification has an accepted model (or
its question says why not), and the worker that runs the twin's code is up.

Everything is read from the twin's API; the time a question takes is the time its
answer took to arrive, which is what a caller sees.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from dta_core.log import Log
from dta_core.manifest import Manifest

from .readiness import API, Context
from .result import CheckResult, described, problem_of

log = Log("acceptance.compute")

QUESTIONS = f"{API}/questions"
MODELS = f"{API}/models"
HEALTH = f"{API}/meta/health"
WORKER_SERVICE = "worker"
PAGE = 1_000
VALIDATED, PROVISIONAL, FAILING = "validated", "provisional", "failing"
# The API's refusal to answer a question whose specification has no active model.
NONE_ACTIVE = "model.none_active"

ANSWERED_REMEDY = (
    "Ask the question yourself (dta questions answer <id> --arg name=value) and read the "
    "problem's code and detail; a question answered by the twin's code or by a model needs "
    "the worker (dta logs worker). A slow answer needs a faster function or a wider "
    "latency_budget in the manifest. Build again (dta build) once mended."
)
ERROR_REMEDY = (
    "A failing question was measured outside its tolerance: improve the model or function "
    "that answers it, or agree a wider tolerance in the manifest; then build again (dta build) "
    "or measure again (dta questions measure)."
)
MODELS_REMEDY = (
    "Evaluate again (dta models evaluate) and read each candidate's reasons (dta models list, "
    "GET /api/v1/models/{uid}/backtests); add a candidate that beats the threshold and the "
    "baseline, or load more history."
)
WORKER_REMEDY = (
    "Start the worker (dta up worker, or dta build) and read its logs (dta logs worker)."
)


def _questions(manifest: Manifest) -> dict[str, Any]:
    contract = manifest.contract
    return dict(sorted((contract.questions or {}).items())) if contract else {}


def _needs_worker(manifest: Manifest) -> bool:
    registered = any(f.kind == "registered" for f in (manifest.functions or {}).values())
    return registered or bool(manifest.models)


def _budget_ms(manifest: Manifest, question: Any) -> float | None:
    function = (manifest.functions or {}).get(question.function) if question.function else None
    budget = function.latency_budget if function is not None else None
    return budget.total_seconds() * 1000.0 if budget is not None else None


def _listing(ctx: Context, path: str, evidence: dict[str, Any], params: dict | None = None):
    """Every record of a listing, or None with the failing request in the evidence."""
    found: list[dict[str, Any]] = []
    asked: dict[str, Any] = {**(params or {}), "limit": PAGE}
    while True:
        response = ctx.api.get(path, params=asked)
        if response.status_code != 200:
            evidence["failing_request"] = described("GET", path, asked)
            evidence["response"] = problem_of(response)
            log.warn("a listing could not be read", path=path, status=response.status_code)
            return None
        document = response.json()
        found.extend(document.get("data") or [])
        if not document.get("next_cursor"):
            return found
        asked = {**asked, "cursor": document["next_cursor"]}


def _no_questions(ident: str, examined: str) -> CheckResult:
    return CheckResult(
        ident, examined, True, {"applicable": False, "reason": "the contract has no question"}
    )


# -- questions.answered ---------------------------------------------------------------


def _state_of(ctx: Context, qid: str, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """What a question may claim now, from GET /questions/{id}; None when it cannot be read."""
    path = f"{QUESTIONS}/{quote(qid, safe='')}"
    response = ctx.api.get(path)
    if response.status_code != 200:
        evidence.setdefault("unread", []).append(
            {"request": described("GET", path), "response": problem_of(response)}
        )
        log.warn("a question's state could not be read", question=qid)
        return None
    return response.json().get("data") or {}


def questions_answered(ctx: Context) -> CheckResult:
    examined = (
        "each question of the contract, asked once at the head with its manifest example "
        "arguments, answers 200 within its function's latency budget (a provisional question "
        "with no model to answer it yet is reported with its reason)"
    )
    questions = _questions(ctx.manifest)
    if not questions:
        return _no_questions("questions.answered", examined)
    evidence: dict[str, Any] = {"questions": {}}
    failures = []
    for qid, question in questions.items():
        path = f"{QUESTIONS}/{quote(qid, safe='')}/answer"
        arguments = dict(question.example or {})
        budget = _budget_ms(ctx.manifest, question)
        started = time.perf_counter()
        response = ctx.api.request("POST", path, params={"at": "head"}, json=arguments)
        took = round((time.perf_counter() - started) * 1000.0, 1)
        entry: dict[str, Any] = {
            "request": described("POST", path, {"at": "head"}),
            "arguments": arguments,
            "status": response.status_code,
            "time_ms": took,
            "budget_ms": budget,
        }
        if response.status_code == 200:
            body = response.json()
            entry["provisional"] = body.get("provisional")
            entry["version"] = (body.get("version") or {}).get("seq")
            if budget is not None and took > budget:
                entry["reason"] = f"answered in {took:g} ms, beyond its budget of {budget:g} ms"
                failures.append(f"{qid}: {entry['reason']}")
        else:
            problem = problem_of(response)
            entry["problem"] = problem
            state = _state_of(ctx, qid, evidence) if problem.get("code") == NONE_ACTIVE else None
            if state is not None and state.get("status") == PROVISIONAL and state.get("reason"):
                # No model could be selected yet, and the question says why: it cannot be
                # answered until it can be evaluated, which is not a fault of the answer.
                entry["not_answerable"] = f"provisional: {state['reason']}"
                log.info("a provisional question has no model to answer it yet", question=qid)
            else:
                failures.append(f"{qid}: {problem.get('code')}: {problem.get('detail')}")
        evidence["questions"][qid] = entry
    passed = not failures
    if not passed:
        evidence["failures"] = failures
        log.warn("questions did not all answer in time", failures=failures)
    return CheckResult("questions.answered", examined, passed, evidence, ANSWERED_REMEDY)


# -- questions.error --------------------------------------------------------------------


def questions_error(ctx: Context) -> CheckResult:
    examined = (
        "from GET /questions, each question is validated or provisional with its reason, none "
        "is failing, and a validated question's measured error is within its tolerance"
    )
    questions = _questions(ctx.manifest)
    if not questions:
        return _no_questions("questions.error", examined)
    evidence: dict[str, Any] = {}
    listing = _listing(ctx, QUESTIONS, evidence)
    if listing is None:
        return CheckResult("questions.error", examined, False, evidence, ERROR_REMEDY)
    by_id = {record.get("id"): record for record in listing}
    failures = []
    evidence["questions"] = {}
    for qid in questions:
        record = by_id.get(qid)
        if record is None:
            failures.append(f"{qid}: not registered with the twin")
            evidence["questions"][qid] = None
            continue
        status, error, tolerance = (
            record.get("status"),
            record.get("measured_error"),
            record.get("tolerance"),
        )
        evidence["questions"][qid] = {
            "status": status,
            "basis": record.get("basis"),
            "measured_error": error,
            "tolerance": tolerance,
            "metric": record.get("metric"),
            "reason": record.get("reason"),
        }
        if status == FAILING:
            failures.append(
                f"{qid}: failing ({record.get('reason') or 'measured outside its tolerance'}; "
                f"error {error}, tolerance {tolerance})"
            )
        elif status == PROVISIONAL and not record.get("reason"):
            failures.append(f"{qid}: provisional without a reason")
        elif status == VALIDATED:
            if isinstance(error, int | float) and isinstance(tolerance, int | float):
                if error > tolerance:
                    failures.append(f"{qid}: measured error {error} beyond tolerance {tolerance}")
        elif status not in (PROVISIONAL, VALIDATED):
            failures.append(f"{qid}: unexpected status {status}")
    passed = not failures
    if not passed:
        evidence["failures"] = failures
        log.warn("questions are measured outside their tolerance", failures=failures)
    return CheckResult("questions.error", examined, passed, evidence, ERROR_REMEDY)


# -- models.accepted ---------------------------------------------------------------------


def models_accepted(ctx: Context) -> CheckResult:
    examined = (
        "every model specification has an active model with a fitted state, or its question "
        "is provisional with a reason"
    )
    specs = dict(sorted((ctx.manifest.models or {}).items()))
    if not specs:
        return CheckResult(
            "models.accepted",
            examined,
            True,
            {"applicable": False, "reason": "the manifest declares no model specification"},
        )
    evidence: dict[str, Any] = {"specs": {}}
    models = _listing(ctx, MODELS, evidence)
    questions = _listing(ctx, QUESTIONS, evidence)
    if models is None or questions is None:
        return CheckResult("models.accepted", examined, False, evidence, MODELS_REMEDY)
    by_question = {record.get("id"): record for record in questions}
    failures = []
    for spec_id, spec in specs.items():
        active = next(
            (m for m in models if m.get("spec") == spec_id and m.get("status") == "active"), None
        )
        entry: dict[str, Any] = {"question": spec.question}
        if active is not None and active.get("fitted"):
            entry["active"] = active.get("uid")
            entry["family"] = active.get("family")
            path = f"{MODELS}/{quote(str(active.get('uid')), safe=':/')}/backtests"
            response = ctx.api.get(path, params={"limit": 1})
            backtests = response.json().get("data") if response.status_code == 200 else None
            if backtests:
                latest = backtests[0]
                entry["backtest"] = {
                    "metric": latest.get("metric"),
                    "error": latest.get("error"),
                    "threshold": latest.get("threshold"),
                    "baseline": latest.get("baseline"),
                    "baseline_error": latest.get("baseline_error"),
                    "accepted": latest.get("accepted"),
                }
            else:
                entry["backtest"] = None
                entry["backtest_request"] = problem_of(response)
            evidence["specs"][spec_id] = entry
            continue
        question = by_question.get(spec.question or "") or {}
        entry["active"] = active.get("uid") if active else None
        entry["question_status"] = question.get("status")
        entry["question_reason"] = question.get("reason")
        if question.get("status") == PROVISIONAL and question.get("reason"):
            entry["reason"] = f"question {spec.question} is provisional: {question['reason']}"
        else:
            entry["reason"] = (
                "the active model has no fitted state"
                if active is not None
                else "no active model, and its question is not provisional with a reason"
            )
            failures.append(f"{spec_id}: {entry['reason']}")
        evidence["specs"][spec_id] = entry
    passed = not failures
    if not passed:
        evidence["failures"] = failures
        log.warn("model specifications without an accepted model", failures=failures)
    return CheckResult("models.accepted", examined, passed, evidence, MODELS_REMEDY)


# -- compute.worker -----------------------------------------------------------------------


def compute_worker(ctx: Context, services: Callable[[], list[dict[str, Any]]]) -> CheckResult:
    examined = "compute.worker of GET /meta/health is configured and ok"
    if not _needs_worker(ctx.manifest):
        return CheckResult(
            "compute.worker",
            examined,
            True,
            {
                "applicable": False,
                "reason": "the twin has no registered function and no model: it has no worker",
            },
        )
    evidence: dict[str, Any] = {}
    response = ctx.api.get(HEALTH)
    if response.status_code != 200:
        evidence["failing_request"] = described("GET", HEALTH)
        evidence["response"] = problem_of(response)
        return CheckResult("compute.worker", examined, False, evidence, WORKER_REMEDY)
    worker = (response.json().get("compute") or {}).get("worker") or {}
    evidence["worker"] = {"configured": worker.get("configured"), "ok": worker.get("ok")}
    row = next((r for r in services() if r.get("service") == WORKER_SERVICE), None)
    evidence["service"] = {"state": row.get("state"), "health": row.get("health")} if row else None
    passed = bool(worker.get("configured")) and bool(worker.get("ok"))
    if not passed:
        evidence["reason"] = (
            "the API has no worker configured"
            if not worker.get("configured")
            else "the API's worker does not answer"
        )
        log.warn("worker check failed", evidence=evidence)
    return CheckResult("compute.worker", examined, passed, evidence, WORKER_REMEDY)


__all__ = ["compute_worker", "models_accepted", "questions_answered", "questions_error"]
