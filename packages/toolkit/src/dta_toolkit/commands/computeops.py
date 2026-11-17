"""What the toolkit asks of a twin's API about what the twin computes.

Shared by `dta build` and the commands that work on functions, models and
questions: uploading the twin's packed code, registering, evaluating the model
specifications, measuring the questions, and reading each back. Evaluating and
measuring are jobs of the API's compute lane; they are followed to their end
as sync jobs are. A refusal is raised with the API's own code and detail.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dta_core.errors import CheckFailed
from dta_core.log import Log
from dta_core.manifest import Manifest

from .. import codepack
from ..acceptance import TwinApi
from ..workspace import Workspace
from .syncjobs import ApiRefused, read_json, refused, wait_job

log = Log("commands.compute")

API = "/api/v1"
ARTIFACTS = f"{API}/artifacts"
REGISTER = f"{API}/lifecycle/register"
MODELS = f"{API}/models"
EVALUATE = f"{API}/models/evaluate"
QUESTIONS = f"{API}/questions"
MEASURE = f"{API}/questions/measure"
SCENARIOS = f"{API}/scenarios"
HEALTH = f"{API}/meta/health"
PAGE = 10_000
ZIP = "application/zip"

EVALUATE_WAIT_S = 7_200.0
MEASURE_WAIT_S = 900.0
KEY_PREFIX = "dta-build"
EVALUATE_TIMEOUT = "models.wait_timeout"
MEASURE_TIMEOUT = "questions.wait_timeout"
EVALUATE_FAILED = "models.evaluate_failed"
MEASURE_FAILED = "questions.measure_failed"
CODE_OUTSIDE = "code.outside_workspace"
# What `register` answers per registry for what the manifest no longer has.
ORPHANED = {"functions": "disabled", "models": "retired", "questions": "orphaned"}
SECTIONS = ("functions", "models", "questions")


# -- reading -----------------------------------------------------------------------------


def _segment(text: str) -> str:
    """A path segment as the API reads it; a model's uid keeps its `/` and `:`."""
    return quote(text, safe=":/")


def read_all(api: TwinApi, path: str, params: Mapping[str, Any] | None = None) -> list[dict]:
    """Every record of a listing, page after page."""
    found: list[dict[str, Any]] = []
    cursor = None
    while True:
        asked: dict[str, Any] = {**(params or {}), "limit": PAGE}
        if cursor:
            asked["cursor"] = cursor
        response = api.get(path, params=asked)
        if response.status_code != 200:
            error = refused(response, f"GET {path}")
            log.warn("the API refused a listing", path=path, code=error.code)
            raise error
        document = response.json()
        found.extend(document.get("data") or [])
        cursor = document.get("next_cursor")
        if not cursor:
            return found


def head_of(api: TwinApi) -> dict[str, Any]:
    """`id`, `seq` and `t` of the live line's head, as /meta/health tells it."""
    return dict(read_json(api, HEALTH).get("head") or {})


# -- the code ------------------------------------------------------------------------------


def code_directory(workspace: Workspace, manifest: Manifest) -> Path | None:
    """The directory of the twin's own code; refused when it lies outside the workspace."""
    relative = manifest.runtime.code
    if not relative:
        return None
    path = (workspace.root / relative).resolve()
    if path != workspace.root and workspace.root not in path.parents:
        log.error("runtime.code leads out of the workspace", code=relative)
        raise CheckFailed(
            f"runtime.code ({relative}) is outside the workspace; name a directory inside it",
            code=CODE_OUTSIDE,
            detail={"code": relative},
        )
    return path


def pack_code(workspace: Workspace, manifest: Manifest) -> codepack.Packed:
    """The twin's code packed, refused when the manifest names a module it lacks."""
    directory = code_directory(workspace, manifest)
    assert directory is not None
    packed = codepack.pack(directory)
    missing = codepack.missing_modules(manifest, packed.files)
    if missing:
        first = missing[0]
        log.error("the code lacks a module the manifest names", missing=missing)
        raise CheckFailed(
            f"{first['path']} names {first['entry']}, but {manifest.runtime.code} has no module "
            f"{first['module']} (looked for {first['looked_for']}); add the module, or correct "
            "the entry in the manifest",
            code=codepack.CODE_NO_MODULE,
            detail={"missing": missing, "files": list(packed.files)},
        )
    return packed


def upload_code(api: TwinApi, packed: codepack.Packed) -> dict[str, Any]:
    """Keep the packed code in the API's artifacts unless it already holds that hash."""
    path = f"{ARTIFACTS}/{packed.sha256}"
    status = api.status_of(path)
    summary = {
        "sha256": packed.sha256,
        "size_bytes": packed.size_bytes,
        "files": len(packed.files),
        "unpacked_bytes": packed.unpacked_bytes,
        "skipped_links": list(packed.skipped_links),
    }
    if status == 200:
        log.info("the API holds the code already; nothing is uploaded", sha256=packed.sha256)
        return {**summary, "uploaded": False}
    if status != 404:
        log.warn("whether the API holds the code could not be read", status=status)
    started = time.perf_counter()
    response = api.request("POST", ARTIFACTS, content=packed.content, headers={"Content-Type": ZIP})
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if response.status_code not in (200, 201):
        error = refused(response, f"POST {ARTIFACTS}")
        log.error("the API refused the code", code=error.code, status=response.status_code)
        raise error
    kept = response.json().get("sha256")
    if kept != packed.sha256:
        log.error("the API kept the code under another hash", sent=packed.sha256, kept=kept)
        raise CheckFailed(
            "the API kept the code under another hash than the toolkit computed",
            code="code.hash_mismatch",
            detail={"sent": packed.sha256, "kept": kept},
        )
    log.info("code uploaded", sha256=packed.sha256, size_bytes=packed.size_bytes, ms=elapsed)
    return {**summary, "uploaded": True, "upload_ms": elapsed}


# -- registering -----------------------------------------------------------------------


def register(api: TwinApi, code: str | None) -> dict[str, Any]:
    """Bring the twin's registries in line with its manifest; what changed, counted."""
    started = time.perf_counter()
    response = api.request("POST", REGISTER, json={"code": code})
    if response.status_code != 200:
        error = refused(response, f"POST {REGISTER}")
        log.error("the API refused to register", code=error.code, status=response.status_code)
        raise error
    answer = response.json()
    counts: dict[str, dict[str, int]] = {}
    lists: dict[str, dict[str, list[str]]] = {}
    for section in SECTIONS:
        found = answer.get(section) or {}
        parts = {
            "created": list(found.get("created") or []),
            "changed": list(found.get("changed") or []),
            "unchanged": list(found.get("unchanged") or []),
            "orphaned": list(found.get(ORPHANED[section]) or []),
        }
        counts[section] = {name: len(uids) for name, uids in parts.items()}
        lists[section] = {name: uids for name, uids in parts.items() if name != "unchanged"}
    written = any(
        counts[section][name] for section in SECTIONS for name in ("created", "changed")
    ) or any(counts[section]["orphaned"] for section in ("functions", "models"))
    result = {
        "code": code,
        "counts": counts,
        "changes": lists,
        "changed_anything": written,
        "version": answer.get("version"),
    }
    log.info(
        "registries in line with the manifest",
        counts=counts,
        changed_anything=written,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return result


def touched_models(registered: Mapping[str, Any] | None) -> set[str]:
    """uids of the models a registration created or changed."""
    changes = ((registered or {}).get("changes") or {}).get("models") or {}
    return set(changes.get("created") or []) | set(changes.get("changed") or [])


def touched_questions(registered: Mapping[str, Any] | None) -> set[str]:
    """ids of the questions a registration created or changed."""
    changes = ((registered or {}).get("changes") or {}).get("questions") or {}
    uids = list(changes.get("created") or []) + list(changes.get("changed") or [])
    return {uid.split(":", 1)[-1] for uid in uids}


# -- which specifications need evaluating ----------------------------------------------


@dataclass(frozen=True)
class SpecState:
    """What the twin holds of one model specification."""

    spec: str
    # uid -> status of each candidate that is not retired.
    candidates: dict[str, str]
    # uids of the candidates that have at least one backtest.
    backtested: frozenset[str]

    @property
    def active(self) -> str | None:
        return next((uid for uid, status in self.candidates.items() if status == "active"), None)


def spec_basis(manifest: Manifest, spec: str) -> str:
    """A hash of what an evaluation of a specification rests on in the manifest: its own
    definition and the horizon of each question it answers, which the backtest takes."""
    definition = manifest.models[spec].model_dump(mode="json")
    contract = manifest.contract
    horizons = sorted(
        str(question.horizon)
        for question in ((contract.questions or {}) if contract else {}).values()
        if question.model == spec
    )
    text = json.dumps({"spec": definition, "horizons": horizons}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def needs_evaluating(
    state: SpecState, touched: Iterable[str], every: bool, *, rebased: bool = False
) -> str | None:
    """Why a specification is evaluated in this build, or None when it is not.

    It is when the build was asked to evaluate every specification, when this
    build's registration created or changed one of its candidates, when what its
    last evaluation rested on has changed since (`rebased`: its definition, or the
    horizon of its question), or when it has no active model and a candidate of it
    was never backtested.
    """
    if every:
        return "asked to evaluate every specification"
    changed = sorted(set(touched) & set(state.candidates))
    if changed:
        return f"registered anew or changed: {', '.join(changed)}"
    if rebased:
        return "its definition or its question's horizon changed since it was last evaluated"
    if state.active is None:
        never = sorted(set(state.candidates) - set(state.backtested))
        if never:
            return f"no active model, and never backtested: {', '.join(never)}"
    return None


def spec_states(api: TwinApi, specs: Sequence[str]) -> dict[str, SpecState]:
    """What the twin holds of each specification; the backtests are only asked for where
    no model is active, as only there they decide anything."""
    by_spec: dict[str, dict[str, str]] = {spec: {} for spec in specs}
    for model in read_all(api, MODELS):
        spec = model.get("spec")
        if spec in by_spec and model.get("status") != "retired":
            by_spec[spec][model["uid"]] = model.get("status")
    found = {}
    for spec, candidates in by_spec.items():
        backtested: set[str] = set()
        if not any(status == "active" for status in candidates.values()):
            for uid in candidates:
                response = api.get(f"{MODELS}/{_segment(uid)}/backtests", params={"limit": 1})
                if response.status_code != 200:
                    error = refused(response, f"GET {MODELS}/{{uid}}/backtests")
                    log.warn("the backtests of a model could not be read", model=uid)
                    raise error
                if response.json().get("data"):
                    backtested.add(uid)
        found[spec] = SpecState(spec=spec, candidates=candidates, backtested=frozenset(backtested))
    return found


# -- jobs of the compute lane ----------------------------------------------------------


def job_key(kind: str, *parts: str) -> str:
    """An Idempotency-Key for work the build asks for: the same parts, the same key."""
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}:{kind}:{digest}"


def start_job(
    api: TwinApi, path: str, body: Mapping[str, Any], key: str | None
) -> tuple[dict[str, Any], bool]:
    """The job made for the request (True) or the one that exists for it (False)."""
    headers = {"Idempotency-Key": key} if key else None
    started = time.perf_counter()
    response = api.request("POST", path, json=dict(body), headers=headers)
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if response.status_code not in (200, 202):
        error = refused(response, f"POST {path}")
        log.warn(
            "the API refused to start a job",
            path=path,
            code=error.code,
            status=response.status_code,
            duration_ms=elapsed,
        )
        raise error
    job = (response.json() or {}).get("job") or {}
    created = response.status_code == 202
    log.info(
        "job asked for" if created else "the job for this work exists already",
        path=path,
        job=job.get("id"),
        state=job.get("state"),
        key=key,
        duration_ms=elapsed,
    )
    return job, created


def _job_failed(job: Mapping[str, Any], code: str, what: str) -> CheckFailed:
    error = job.get("error") or {}
    found = str(error.get("code") or code)
    log.error(f"the {what} job failed", job=job.get("id"), code=found)
    return CheckFailed(
        f"the {what} job failed: {found}: {error.get('message') or 'no message'}",
        code=found,
        detail={"job": job.get("id"), "error": error, "trace_id": job.get("trace_id")},
    )


def _candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    scores = item.get("scores") or {}
    baseline = item.get("baseline_scores") or {}
    entry = {
        "uid": item.get("uid"),
        "candidate": item.get("candidate"),
        "family": item.get("family"),
        "status": item.get("status"),
        "note": item.get("note"),
        "accepted": item.get("accepted"),
        "error": item.get("error"),
        "skill": item.get("skill"),
        "reasons": list(item.get("reasons") or []),
        "scores": scores,
        "baseline_scores": baseline,
    }
    if item.get("failed"):
        entry["failed"] = item["failed"]
    return entry


def spec_summary(found: Mapping[str, Any]) -> dict[str, Any]:
    """One specification of an evaluation's result as the build reports it."""
    return {
        "spec": found.get("spec"),
        "question": found.get("question"),
        "selected": found.get("selected"),
        "question_status": found.get("question_status"),
        "measured_error": found.get("measured_error"),
        "skipped": found.get("skipped"),
        "fit_error": found.get("fit_error"),
        "candidates": [_candidate(item) for item in found.get("candidates") or []],
    }


def evaluate(
    api: TwinApi,
    specs: Sequence[str | None],
    *,
    keys: Mapping[str, str] | None = None,
    limit_s: float = EVALUATE_WAIT_S,
    wait: bool = True,
) -> list[dict[str, Any]]:
    """Evaluate each specification in a job of its own, one after the other (None: every
    specification in one job); with `wait`, the summary of each specification once its
    job has ended, else the jobs as started."""
    results = []
    for spec in specs:
        key = (keys or {}).get(spec) if spec else None
        job, created = start_job(api, EVALUATE, {"spec": spec} if spec else {}, key)
        if not wait:
            results.append({"spec": spec, "job": job, "created": created})
            continue
        started = time.perf_counter()
        ended = wait_job(
            api,
            str(job.get("id")),
            limit_s=limit_s,
            what="evaluate",
            code=EVALUATE_TIMEOUT,
            follow="dta models list, or GET /api/v1/lifecycle/jobs",
        )
        if ended.get("state") != "succeeded":
            raise _job_failed(ended, EVALUATE_FAILED, "evaluate")
        for found in (ended.get("result") or {}).get("specs") or []:
            summary = spec_summary(found)
            summary.update(
                job=ended.get("id"),
                job_created=created,
                wrote=((ended.get("result") or {}).get("version") or {}).get("seq"),
                duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )
            results.append(summary)
            log.info(
                "specification evaluated",
                spec=summary["spec"],
                selected=summary["selected"],
                question_status=summary["question_status"],
                duration_ms=summary["duration_ms"],
            )
    return results


def unaccepted(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The evaluated specifications that ended without a selected candidate, and why."""
    found = []
    for summary in results:
        if summary.get("selected") is not None:
            continue
        reasons = summary.get("skipped") or summary.get("fit_error") or "no candidate is accepted"
        found.append(
            {
                "spec": summary.get("spec"),
                "question": summary.get("question"),
                "question_status": summary.get("question_status"),
                "reason": reasons if isinstance(reasons, str) else str(reasons),
            }
        )
    return found


def measure(
    api: TwinApi,
    questions: Sequence[str] | None = None,
    *,
    key: str | None = None,
    limit_s: float = MEASURE_WAIT_S,
    wait: bool = True,
) -> dict[str, Any]:
    """Measure the questions (all when none is named) and, with `wait`, the job's end."""
    body: dict[str, Any] = {"questions": list(questions)} if questions else {}
    job, created = start_job(api, MEASURE, body, key)
    if not wait:
        return {"job": job, "created": created}
    ended = wait_job(
        api,
        str(job.get("id")),
        limit_s=limit_s,
        what="measure",
        code=MEASURE_TIMEOUT,
        follow="dta questions list, or GET /api/v1/lifecycle/jobs",
    )
    if ended.get("state") != "succeeded":
        raise _job_failed(ended, MEASURE_FAILED, "measure")
    result = ended.get("result") or {}
    return {
        "job": ended.get("id"),
        "created": created,
        "measured": [found.get("id") for found in result.get("questions") or []],
        "wrote": (result.get("version") or {}).get("seq"),
    }


def question_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "basis": record.get("basis"),
        "measured_error": record.get("measured_error"),
        "tolerance": record.get("tolerance"),
        "metric": record.get("metric"),
        "unit": record.get("unit"),
        "reason": record.get("reason"),
        "model": record.get("model"),
        "measured_at": record.get("measured_at"),
    }


def questions(api: TwinApi) -> list[dict[str, Any]]:
    """What each registered question may claim now."""
    return [question_summary(record) for record in read_all(api, QUESTIONS)]


def model_path(uid: str) -> str:
    return f"{MODELS}/{_segment(uid)}"


__all__ = [
    "ApiRefused",
    "SpecState",
    "code_directory",
    "evaluate",
    "head_of",
    "job_key",
    "measure",
    "needs_evaluating",
    "pack_code",
    "question_summary",
    "questions",
    "read_all",
    "register",
    "spec_states",
    "touched_models",
    "unaccepted",
    "upload_code",
]
