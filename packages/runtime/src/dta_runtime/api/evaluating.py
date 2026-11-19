"""The jobs that decide what the twin may claim: evaluating the candidates of a model
specification, and measuring each question against its tolerance.

Both read the twin at the head pinned when they start and write what they found as one
version at the end. Every backtest and fit is a request to the worker; what is decided
here (the layout of the folds, acceptance, selection) is the selection package's
arithmetic on what the worker measured.

An evaluation has a scope. The build's looks at the whole history and every candidate.
A recent one, what a correction of the live twin runs, is the same evaluation narrowed:
to the frame that ends at the head and reaches back the initial window plus the fidelity
window, to the origins of the fidelity window (at least one), and to the candidates of
the correction's rung. It changes the models only when it selects one, and records its
backtests with `scores.scope` = `recent`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dta_core.errors import DtaError
from dta_core.families import BASELINES, ORDER
from dta_core.functions import BUILTINS
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks import ManifestView
from dta_core.manifest.models import ModelSpec
from dta_core.store import Selector, UnknownVersion, VersionRef

from ..models import Plan, judge, plan, select
from ..models.state import MEDIA_TYPE as STATE_MEDIA
from ..protocol import BacktestRequest, FitRequest
from . import predicting, registry, running
from .jobs import Outcome, Work
from .problems import ApiError
from .tables import version_json

if TYPE_CHECKING:
    from .services import Services

log = Log("api.evaluate")

EVALUATE_KIND = "evaluate"
MEASURE_KIND = "measure"
BACKTEST_BUDGET_S = 600.0
FIT_BUDGET_S = 300.0
JOB_BUDGET_S = 3_600.0
RESIDUALS_MEDIA = "application/vnd.apache.arrow.file"
NOT_EVALUATED = "not_evaluated"
# Error measures in the unit of what is measured: only these compare with a noise floor.
_IN_TARGET_UNIT = ("mae", "rmse", "max_abs")

VALIDATED = "validated"
PROVISIONAL = "provisional"
FAILING = "failing"

BUILD = "build"
RECENT = "recent"
RECALIBRATE = "recalibrate"
RESELECT = "reselect"


@dataclass(frozen=True)
class Scope:
    """What an evaluation of one specification looks at, and what it may change."""

    name: str = BUILD
    # Where the frame begins; None where the twin's data begins.
    start: VersionRef | None = None
    # Uids of the candidates to evaluate; None for every one given.
    only: frozenset[str] | None = None
    # The newest origins only, at most this many; None for all the history has room for.
    max_windows: int | None = None
    # Fewest origins of a valid evaluation; None for the specification's own.
    min_folds: int | None = None
    # Nothing accepted, or too short a history, is settled in the records (the build:
    # candidates rejected, the question failing or provisional), or nothing changes.
    settles: bool = True
    # Each request to the worker gets no more time than the job has left.
    bounded: bool = False
    # Written into the `scores` of every backtest record, beside `scope`.
    detail: Mapping[str, Any] | None = None


BUILD_SCOPE = Scope()


def _error(exc: DtaError) -> dict[str, str]:
    return {"code": exc.code, "message": exc.message}


def _is_the_workers(exc: Exception) -> bool:
    """Whether a failure says something of the worker and nothing of what it was asked."""
    return isinstance(exc, DtaError) and exc.code.startswith("worker.")


def _simplicity(model: Mapping[str, Any]) -> tuple[int, str]:
    """Simplest shipped family first, code of the twin's own last."""
    family = model["family"]
    return (ORDER.index(family) if family in ORDER else len(ORDER), model["candidate"])


def _questions(manifest: Manifest) -> dict[str, Any]:
    return dict((manifest.contract.questions if manifest.contract else None) or {})


def _layout(
    services: Services,
    manifest: Manifest,
    spec: ModelSpec,
    head: VersionRef,
    scope: Scope = BUILD_SCOPE,
) -> tuple[Plan | None, str | None]:
    """The folds the history up to `head` has room for, or why none can be laid out."""
    question = _questions(manifest).get(spec.question or "")
    backtest = spec.backtest
    if question is None or question.horizon is None:
        return None, "the question of the specification states no horizon"
    if backtest is None or backtest.initial_window is None or backtest.step is None:
        return None, "the specification states no backtest settings"
    step = services.interval(head)
    first = scope.start or services.first_data()
    rows = max(0, (head.t - first.t) // step + 1) if head.t >= first.t else 0
    fewest = scope.min_folds if scope.min_folds is not None else backtest.min_folds
    found = plan(
        rows,
        initial_window=backtest.initial_window,
        origin_step=backtest.step,
        horizon=question.horizon,
        step=step,
        min_folds=fewest,
    )
    if scope.max_windows is not None and found.windows > scope.max_windows:
        # The folds are laid out from the end of the frame: the newest are kept.
        found = dataclasses.replace(
            found, windows=scope.max_windows, enough=scope.max_windows >= fewest
        )
    return found, None


def _too_few(found: Plan, spec: ModelSpec, scope: Scope = BUILD_SCOPE) -> str:
    wanted = scope.min_folds if scope.min_folds is not None else None
    if wanted is None:
        wanted = spec.backtest.min_folds if spec.backtest else 0
    return (
        f"the history has room for {found.windows} forecast origins; a valid backtest "
        f"needs {wanted}"
    )


def _backtest_uid(model: Mapping[str, Any], head: VersionRef, trace_id: str) -> str:
    digest = hashlib.sha256(f"{model['uid']}:{head.id}:{trace_id}".encode()).hexdigest()[:8]
    return (
        f"{registry.BACKTEST_SCOPE}:{model['spec']}/{model['candidate']}@"
        f"{head.t:%Y%m%dT%H%M%S}Z-{digest}"
    )


def _refit_every(refit: Any) -> int:
    if refit is True:
        return 1
    return 0 if refit is False else int(refit)


def judge_question(
    record: Mapping[str, Any],
    *,
    active: Mapping[str, Any] | None,
    scores: Mapping[str, float] | None,
    backtest_uid: str | None,
    enough: bool,
    few: str,
    head: VersionRef,
) -> dict[str, Any]:
    """What a question answered by a model may claim: from the scores of its active
    model's latest backtest, in the question's own measure, against its tolerance."""
    metric, tolerance = record.get("metric"), record.get("tolerance")
    row: dict[str, Any] = {
        "uid": record["uid"],
        "basis": "backtest",
        "measured_error": None,
        "measured_at": head.t,
        "model": None if active is None else active["uid"],
        "backtest": backtest_uid if active is not None else None,
    }
    if active is None:
        if not enough:
            return {**row, "status": PROVISIONAL, "reason": few, "basis": None}
        return {**row, "status": FAILING, "reason": "no candidate of its model is accepted"}
    error = None if scores is None or metric is None else registry.finite(scores.get(metric))
    if error is None:
        reason = (
            "its active model has no backtest"
            if scores is None
            else f"the backtest of its active model did not score {metric}"
        )
        return {**row, "status": FAILING, "reason": reason}
    row["measured_error"] = float(error)
    if tolerance is None:
        return {**row, "status": PROVISIONAL, "reason": "the question states no tolerance"}
    if float(error) <= tolerance:
        return {**row, "status": VALIDATED, "reason": None}
    return {
        **row,
        "status": FAILING,
        "reason": f"the {metric} of its model's backtest, {float(error):g}, is above the "
        f"tolerance of {tolerance:g}",
    }


def _moment(text: Any) -> datetime:
    return text if isinstance(text, datetime) else datetime.fromisoformat(str(text))


def _budget(scope: Scope, most: float, deadline: float) -> float:
    """The time one request to the worker is given."""
    if not scope.bounded:
        return most
    return max(0.001, min(most, deadline - time.monotonic()))


def _scored_as(scope: Scope, scores: Mapping[str, Any]) -> dict[str, Any]:
    """The `scores` of a backtest record; a narrowed evaluation says which it was."""
    if scope.name == BUILD:
        return dict(scores)
    return {**scores, "scope": scope.name, **dict(scope.detail or {})}


# -- evaluate ----------------------------------------------------------------------------


def _evaluate_spec(
    services: Services,
    manifest: Manifest,
    spec_id: str,
    models: Sequence[dict[str, Any]],
    head: VersionRef,
    deadline: float,
    upserts: dict[str, list[dict[str, Any]]],
    scope: Scope = BUILD_SCOPE,
) -> dict[str, Any]:
    """Backtest the candidates of one specification the scope takes, select, fit the
    selected one; the rows to record go into `upserts`, the summary is returned."""
    declared = manifest.models.get(spec_id)
    everyone = sorted(models, key=_simplicity)
    candidates = [model for model in everyone if scope.only is None or model["uid"] in scope.only]
    items = {
        model["uid"]: {
            "uid": model["uid"],
            "candidate": model["candidate"],
            "family": model["family"],
            "status": model["status"],
            "evaluated": False,
            "note": NOT_EVALUATED,
        }
        for model in candidates
    }
    summary: dict[str, Any] = {
        "spec": spec_id,
        "question": declared.question if declared else None,
        "selected": None,
        "question_status": None,
        "candidates": list(items.values()),
    }
    if declared is None or declared.acceptance is None or declared.acceptance.metric is None:
        summary["skipped"] = "the manifest states no acceptance for this specification"
        log.warn("specification not evaluated", spec=spec_id, reason=summary["skipped"])
        return summary
    try:
        layout, why = _layout(services, manifest, declared, head, scope)
    except DtaError as exc:
        layout, why = None, f"{exc.code}: {exc.message}"
    if layout is None:
        summary["skipped"] = why
        log.warn("specification not evaluated", spec=spec_id, reason=why)
        return summary
    summary["plan"] = {
        "horizon": layout.horizon,
        "step": layout.step,
        "windows": layout.windows,
        "enough": layout.enough,
    }
    question = (
        registry.one(
            services, registry.QUESTION_TYPE, registry.question_uid(declared.question), head
        )
        if declared.question
        else None
    )
    if not layout.enough:
        reason = _too_few(layout, declared, scope)
        summary["skipped"] = reason
        if question is not None and scope.settles:
            upserts.setdefault(registry.QUESTION_TYPE, []).append(
                {
                    "uid": question["uid"],
                    "status": PROVISIONAL,
                    "reason": reason,
                    "basis": None,
                    "measured_error": None,
                    "measured_at": head.t,
                }
            )
            summary["question_status"] = PROVISIONAL
        log.info("specification not evaluated: too few origins", spec=spec_id, reason=reason)
        return summary

    acceptance = declared.acceptance
    metrics = list(
        dict.fromkeys(
            name for name in (acceptance.metric.value, (question or {}).get("metric")) if name
        )
    )
    baseline = BASELINES[acceptance.baseline] if acceptance.baseline else None
    step = services.interval(head)
    first = scope.start or services.first_data()
    verdicts: dict[str, Any] = {}
    scored: dict[str, tuple[dict[str, float], str]] = {}
    for model in candidates:
        item = items[model["uid"]]
        if time.monotonic() >= deadline:
            log.warn("out of budget: candidate not evaluated", spec=spec_id, model=model["uid"])
            continue
        trace_id = predicting.trace_id()
        uid = _backtest_uid(model, head, trace_id)
        row: dict[str, Any] = {
            "uid": uid,
            "model": model["uid"],
            "started_at": head.t,
            "at_version": head.id,
            "range_from": first.t,
            "range_to": head.t,
            "origins": 0,
            "horizon_steps": layout.horizon,
            "step": registry.duration_text(step),
            "metric": acceptance.metric.value,
            "baseline": baseline,
            "threshold": acceptance.threshold,
            "accepted": False,
            "trace_id": trace_id,
        }
        started = time.perf_counter()
        try:
            request = BacktestRequest(
                budget_s=_budget(scope, BACKTEST_BUDGET_S, deadline),
                trace_id=trace_id,
                family=registry.family_ref(model),
                params=registry.from_json(model.get("params"), {}),
                frame=predicting.frame_of(services, model, head, step=step, start=scope.start),
                horizon=layout.horizon,
                step=layout.step,
                windows=layout.windows,
                refit=None,
                levels=list(predicting.DEFAULT_LEVELS),
                metrics=metrics,
                baseline=baseline,
            )
            result, _ = services.compute.run(request, wait_until=deadline)
            verdict = judge(result["scores"], result.get("baseline_scores"), acceptance)
        except (DtaError, ValueError) as exc:
            if _is_the_workers(exc):
                # Not the candidate's failure: with no worker to ask, nothing is known.
                log.error("evaluation stopped: the worker cannot be asked", exc=exc, spec=spec_id)
                raise
            # One candidate that cannot be evaluated does not stop the others.
            failure = (
                _error(exc)
                if isinstance(exc, DtaError)
                else {"code": "model.bad_params", "message": type(exc).__name__}
            )
            log.warn(
                "candidate failed its backtest",
                spec=spec_id,
                model=model["uid"],
                code=failure["code"],
                reason=failure["message"],
            )
            item.update(evaluated=True, note="failed", failed=failure)
            row.update(
                scores=registry.as_json(_scored_as(scope, {"error": failure})),
                duration_ms=round((time.perf_counter() - started) * 1000.0),
            )
            upserts.setdefault(registry.BACKTEST_TYPE, []).append(row)
            continue
        residuals = result.get("residuals")
        if residuals:
            upserts.setdefault(registry.ARTIFACT_TYPE, []).append(
                registry.artifact_row(
                    services,
                    residuals,
                    RESIDUALS_MEDIA,
                    f"residuals of a backtest of {model['uid']}",
                )
            )
        row.update(
            origins=result["folds"],
            refit_every=_refit_every(result["refit"]),
            error=registry.finite(verdict.error),
            baseline_error=registry.finite(
                (result.get("baseline_scores") or {}).get(acceptance.metric.value)
            ),
            accepted=verdict.accepted,
            scores=registry.as_json(
                _scored_as(
                    scope,
                    {
                        "model": result["scores"],
                        "baseline": result.get("baseline_scores"),
                        "skill": verdict.skill,
                        "reasons": list(verdict.reasons),
                        "notes": result.get("notes") or [],
                        "series": result.get("series"),
                    },
                )
            ),
            residuals=registry.artifact_uid(residuals) if residuals else None,
            duration_ms=round(float(result.get("duration_ms") or 0.0)),
        )
        upserts.setdefault(registry.BACKTEST_TYPE, []).append(row)
        verdicts[model["uid"]] = verdict
        scored[model["uid"]] = (dict(result["scores"]), uid)
        item.update(
            evaluated=True,
            note="evaluated",
            backtest=uid,
            scores=result["scores"],
            baseline_scores=result.get("baseline_scores"),
            error=verdict.error,
            skill=verdict.skill,
            accepted=verdict.accepted,
            reasons=list(verdict.reasons),
            folds=result["folds"],
            refit=result["refit"],
        )

    by_uid = {model["uid"]: model for model in candidates}
    selected = select(verdicts, {uid: by_uid[uid]["family"] for uid in verdicts})
    summary["selected"] = selected
    fitted: dict[str, Any] | None = None
    if selected is not None:
        model = by_uid[selected]
        try:
            if time.monotonic() >= deadline:
                raise ApiError("the job's budget ran out before the fit", code="job.out_of_budget")
            fitted, _ = services.compute.run(
                FitRequest(
                    budget_s=_budget(scope, FIT_BUDGET_S, deadline),
                    trace_id=predicting.trace_id(),
                    family=registry.family_ref(model),
                    params=registry.from_json(model.get("params"), {}),
                    frame=predicting.frame_of(services, model, head, step=step, start=scope.start),
                    horizon=layout.horizon,
                ),
                wait_until=deadline,
            )
        except DtaError as exc:
            log.error("the selected candidate could not be fitted", exc=exc, model=selected)
            if _is_the_workers(exc):
                raise
            summary["fit_error"] = _error(exc)
            fitted = None

    was_active = next((model for model in everyone if model["status"] == "active"), None)
    active = was_active
    if selected is not None and fitted is not None:
        active = by_uid[selected]
        upserts.setdefault(registry.ARTIFACT_TYPE, []).append(
            registry.artifact_row(
                services, fitted["state"], STATE_MEDIA, f"fitted state of {selected}"
            )
        )
        # Over every model of the specification: one the scope left out may be the one
        # active until now.
        for model in everyone:
            uid = model["uid"]
            verdict = verdicts.get(uid)
            item = items.get(uid)
            if uid == selected:
                changed: dict[str, Any] = {
                    "status": "active",
                    "fitted": registry.artifact_uid(fitted["state"]),
                    "fitted_from": _moment(fitted["fitted_from"]),
                    "fitted_to": _moment(fitted["fitted_to"]),
                    "fitted_at_version": head.id,
                    "validity": registry.as_json(fitted.get("validity") or {}),
                }
            elif verdict is not None and not verdict.accepted:
                changed = {"status": "rejected"}
            elif (item is not None and item["evaluated"]) or model["status"] == "active":
                # Evaluated and not chosen, or the one that was active until now.
                changed = {"status": "candidate"}
            else:
                continue
            if item is not None:
                item["status"] = changed["status"]
            if len(changed) > 1 or changed["status"] != model["status"]:
                upserts.setdefault(registry.MODEL_TYPE, []).append({"uid": uid, **changed})
        summary["fitted"] = {
            "state": fitted["state"],
            "series": fitted.get("series"),
            "rows": fitted.get("rows"),
            "fitted_from": fitted["fitted_from"],
            "fitted_to": fitted["fitted_to"],
            "notes": fitted.get("notes") or [],
        }
    elif selected is None and scope.settles:
        # Nothing is within what was agreed: what was evaluated and refused says so, and
        # no model of the specification stays active.
        for model in candidates:
            uid = model["uid"]
            verdict = verdicts.get(uid)
            if verdict is not None and not verdict.accepted and model["status"] != "rejected":
                items[uid]["status"] = "rejected"
                upserts.setdefault(registry.MODEL_TYPE, []).append(
                    {"uid": uid, "status": "rejected"}
                )
                if active is not None and active["uid"] == uid:
                    active = None

    changes_models = scope.settles or (selected is not None and fitted is not None)
    if question is not None and changes_models:
        scores, backtest_uid = None, None
        if active is not None:
            if active["uid"] in scored:
                scores, backtest_uid = scored[active["uid"]]
            else:
                stored = predicting.latest_backtest(services, active["uid"], head)
                if stored is not None:
                    scores = registry.from_json(stored.get("scores"), {}).get("model")
                    backtest_uid = stored["uid"]
        row = judge_question(
            question,
            active=active,
            scores=scores,
            backtest_uid=backtest_uid,
            enough=True,
            few="",
            head=head,
        )
        upserts.setdefault(registry.QUESTION_TYPE, []).append(row)
        summary["question_status"] = row["status"]
        summary["measured_error"] = row["measured_error"]
    log.info(
        "specification evaluated",
        spec=spec_id,
        scope=scope.name,
        candidates=len(candidates),
        evaluated=sum(1 for item in items.values() if item["evaluated"]),
        accepted=sorted(uid for uid, verdict in verdicts.items() if verdict.accepted),
        selected=selected,
        fitted=fitted is not None,
        question_status=summary["question_status"],
    )
    return summary


def evaluate_work(services: Services, spec: str | None, budget_s: float) -> Work:
    """Evaluate one specification, or all, at the head in force when the job starts."""

    def work() -> Outcome:
        started = time.monotonic()
        services.compute.require()
        head = services.store.head()
        manifest = services.twin.manifest(at=head)
        by_spec: dict[str, list[dict[str, Any]]] = {}
        for model in registry.rows(services, registry.MODEL_TYPE, head):
            if model["status"] != "retired":
                by_spec.setdefault(model["spec"], []).append(model)
        if spec is not None and spec not in by_spec:
            raise ApiError(
                f"no model of a specification {spec!r} is registered", code="model.unknown_spec"
            )
        upserts: dict[str, list[dict[str, Any]]] = {}
        specs = [
            _evaluate_spec(
                services, manifest, spec_id, by_spec[spec_id], head, started + budget_s, upserts
            )
            for spec_id in ([spec] if spec is not None else sorted(by_spec))
        ]
        version = None
        if upserts:
            # One record per artifact, however many rows name it.
            artifacts = {row["uid"]: row for row in upserts.get(registry.ARTIFACT_TYPE, [])}
            if artifacts:
                upserts[registry.ARTIFACT_TYPE] = list(artifacts.values())
            version = registry.commit(
                services,
                upserts,
                note="models evaluated",
                extra={"evaluated": [found["spec"] for found in specs], "at": head.id},
                t_valid=head.t,
            )
        log.info(
            "evaluation finished",
            specs=len(specs),
            at_seq=head.seq,
            wrote=None if version is None else version.seq,
            duration_ms=round((time.monotonic() - started) * 1000.0),
        )
        return Outcome(
            result=registry.finite(
                {
                    "at": version_json(head),
                    "version": None if version is None else version_json(version),
                    "specs": specs,
                }
            )
        )

    return work


# -- the recent evaluation of a correction ---------------------------------------------------

TIMED_OUT = "timeout"
TOO_LITTLE = "too_little_history"
NOT_ACCEPTED = "not_accepted"
FIT_FAILED = "fit_failed"
NOTHING_TO_EVALUATE = "nothing_to_evaluate"


def _start_of(services: Services, moment: datetime) -> VersionRef:
    """The version in force at a time, never before the twin's data begins."""
    first = services.first_data()
    if moment <= first.t:
        return first
    try:
        found = services.store.resolve(Selector("time", time=moment))[0]
    except UnknownVersion:
        return first
    return found if found.seq >= first.seq else first


def _why_not(summary: Mapping[str, Any], deadline: float) -> tuple[str, str]:
    """Why a recent evaluation selected and fitted nothing: a reason code and its words."""
    if summary.get("skipped"):
        return TOO_LITTLE, str(summary["skipped"])
    items = list(summary.get("candidates") or [])
    timed_out = [
        item["uid"]
        for item in items
        if (item.get("failed") or {}).get("code") == "function.timeout"
        or (item.get("note") == NOT_EVALUATED and time.monotonic() >= deadline)
    ]
    if timed_out:
        return TIMED_OUT, f"the correction's time ran out evaluating {', '.join(timed_out)}"
    if summary.get("fit_error"):
        failure = summary["fit_error"]
        if failure.get("code") in ("function.timeout", "job.out_of_budget"):
            return TIMED_OUT, f"the correction's time ran out fitting: {failure['message']}"
        return FIT_FAILED, f"the selected candidate could not be fitted: {failure['message']}"
    refused = [
        f"{item['uid']} ({'; '.join(item.get('reasons') or []) or item.get('note')})"
        for item in items
    ]
    return NOT_ACCEPTED, "no candidate is accepted over the recent history: " + ", ".join(refused)


def recent_evaluation(
    services: Services,
    *,
    spec: str,
    rung: str,
    budget_s: float,
    note: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The build's evaluation of one specification, narrowed to the recent history and to
    the candidates of a correction's rung: `recalibrate` the active model, `reselect`
    every one not retired. At the head in force when it starts.

    Returns `accepted`, `model` (the one now active), `fitted_to`, `error` (its error in
    this evaluation), `reason` and `reason_code` when nothing was accepted, `version` (what
    was written, None when nothing was), `frame`, the specification's `acceptance`
    (`metric`, `threshold`) and the evaluation's `summary`. A worker
    that cannot be asked raises; the caller reports that as not accepted."""
    if rung not in (RECALIBRATE, RESELECT):
        raise ValueError(f"a recent evaluation runs for recalibrate or reselect, not {rung!r}")
    started = time.monotonic()
    deadline = started + budget_s
    services.compute.require()
    head = services.store.head()
    manifest = services.twin.manifest(at=head)
    result: dict[str, Any] = {
        "accepted": False,
        "model": None,
        "fitted_to": None,
        "error": None,
        "reason": None,
        "reason_code": None,
        "version": None,
        "at": version_json(head),
        "rung": rung,
        "spec": spec,
    }

    def refuse(code: str, reason: str) -> dict[str, Any]:
        log.info(
            "recent evaluation accepted nothing", spec=spec, rung=rung, why=code, reason=reason
        )
        return {**result, "reason": reason, "reason_code": code}

    declared = manifest.models.get(spec)
    fidelity = manifest.fidelity
    question = _questions(manifest).get((declared.question or "") if declared else "")
    backtest = declared.backtest if declared else None
    if declared is None:
        return refuse(NOTHING_TO_EVALUATE, f"the manifest has no model specification {spec}")
    if fidelity is None or fidelity.window is None:
        return refuse(NOTHING_TO_EVALUATE, "the manifest states no fidelity window")
    if backtest is None or backtest.initial_window is None or backtest.step is None:
        return refuse(NOTHING_TO_EVALUATE, "the specification states no backtest settings")
    if question is None or question.horizon is None:
        return refuse(NOTHING_TO_EVALUATE, "the question of the specification states no horizon")
    acceptance = declared.acceptance
    if acceptance is not None and acceptance.metric is not None:
        result["acceptance"] = {
            "metric": acceptance.metric.value,
            "threshold": acceptance.threshold,
        }
    models = [
        model
        for model in registry.rows(services, registry.MODEL_TYPE, head)
        if model["spec"] == spec and model["status"] != "retired"
    ]
    if rung == RECALIBRATE:
        only = frozenset(model["uid"] for model in models if model["status"] == "active")
    else:
        only = frozenset(model["uid"] for model in models)
    if not only:
        what = "no active model" if rung == RECALIBRATE else "no candidate that is not retired"
        return refuse(NOTHING_TO_EVALUATE, f"{spec} has {what} to {rung}")

    window = fidelity.window
    start = _start_of(services, head.t - (backtest.initial_window + window))
    # The origins of the fidelity window whose horizon has been observed.
    windows = max(1, (window - question.horizon) // backtest.step + 1)
    frame = {"from": start.t, "to": head.t, "window": registry.duration_text(window)}
    result["frame"] = {**frame, "windows": windows}
    scope = Scope(
        name=RECENT,
        start=start,
        only=only,
        max_windows=windows,
        min_folds=1,
        settles=False,
        bounded=True,
        detail={
            "rung": rung,
            "frame": {"from": start.t.isoformat(), "to": head.t.isoformat()},
            "window": registry.duration_text(window),
            **dict(note or {}),
        },
    )
    upserts: dict[str, list[dict[str, Any]]] = {}
    summary = _evaluate_spec(services, manifest, spec, models, head, deadline, upserts, scope)
    result["summary"] = summary
    if upserts:
        artifacts = {row["uid"]: row for row in upserts.get(registry.ARTIFACT_TYPE, [])}
        if artifacts:
            upserts[registry.ARTIFACT_TYPE] = list(artifacts.values())
        version = registry.commit(
            services,
            upserts,
            note="models evaluated over recent history",
            extra={"evaluated": [spec], "scope": RECENT, "rung": rung, "at": head.id},
            t_valid=head.t,
        )
        result["version"] = version_json(version)
    selected, fitted = summary.get("selected"), summary.get("fitted")
    elapsed_ms = round((time.monotonic() - started) * 1000.0)
    if selected is None or fitted is None:
        code, reason = _why_not(summary, deadline)
        found = refuse(code, reason)
        found["duration_ms"] = elapsed_ms
        return registry.finite(found)
    item = next(item for item in summary["candidates"] if item["uid"] == selected)
    log.info(
        "recent evaluation accepted a model",
        spec=spec,
        rung=rung,
        model=selected,
        error=item.get("error"),
        fitted_to=fitted["fitted_to"],
        windows=windows,
        duration_ms=elapsed_ms,
    )
    return registry.finite(
        {
            **result,
            "accepted": True,
            "model": selected,
            "fitted_to": _moment(fitted["fitted_to"]),
            "error": item.get("error"),
            "duration_ms": elapsed_ms,
        }
    )


# -- measure -----------------------------------------------------------------------------


def _noise_floor(
    manifest: Manifest, function: Mapping[str, Any]
) -> tuple[float | None, str | None]:
    """The largest noise floor of the source columns a built-in's property is read
    from, when the profiles hold one for every such column; and that property."""
    if function["kind"] != "builtin" or function["entry"] not in BUILTINS:
        return None, None
    target = registry.from_json(function.get("params"), {}).get("property")
    if not isinstance(target, str) or "." not in target:
        return None, None
    floors = [
        mapped.column_profile.noise_sigma if mapped.column_profile is not None else None
        for mapped in ManifestView(manifest).mapped_columns(target)
    ]
    if not floors or any(floor is None for floor in floors):
        return None, target
    return max(floors), target  # type: ignore[type-var]


def _measure_function(
    services: Services,
    manifest: Manifest,
    record: Mapping[str, Any],
    head: VersionRef,
    example: Mapping[str, Any],
) -> dict[str, Any]:
    """What a question answered by a function alone may claim: that the function answers
    inside its latency budget, and how noisy what it reads is, where that is known."""
    row: dict[str, Any] = {
        "uid": record["uid"],
        "basis": "latency",
        "measured_error": None,
        "measured_at": head.t,
        "model": None,
        "backtest": None,
    }
    function = (
        registry.one(services, registry.FUNCTION_TYPE, record["function"], head)
        if record.get("function")
        else None
    )
    if function is None:
        return {**row, "status": FAILING, "reason": "the question names no registered function"}
    try:
        # Asked as the manifest's example asks it: a function that takes arguments at
        # call time (a path between two objects) cannot be asked without any.
        running.run(services, function, head, dict(example), write=False)
    except DtaError as exc:
        log.warn(
            "a question's function did not answer",
            question=record["question"],
            function=function["uid"],
            code=exc.code,
        )
        return {**row, "status": FAILING, "reason": f"{exc.code}: {exc.message}"}
    floor, target = _noise_floor(manifest, function)
    if floor is None or target is None:
        return {**row, "status": VALIDATED, "reason": None}
    row.update(basis="noise_floor", measured_error=float(floor))
    metric, tolerance = record.get("metric"), record.get("tolerance")
    unit = running.unit_of(services, head, target)
    comparable = (
        metric in _IN_TARGET_UNIT
        and tolerance is not None
        and (record.get("unit") is None or record.get("unit") == unit)
    )
    if comparable and float(floor) > tolerance:
        return {
            **row,
            "status": FAILING,
            "reason": f"the noise of what {target} is read from, {float(floor):g}, is above "
            f"the tolerance of {tolerance:g}",
        }
    return {**row, "status": VALIDATED, "reason": None}


def _measure_model(
    services: Services,
    manifest: Manifest,
    record: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]],
    head: VersionRef,
) -> dict[str, Any]:
    active = next((model for model in models if model["status"] == "active"), None)
    scores, backtest_uid = None, None
    enough, few = True, ""
    if active is not None:
        stored = predicting.latest_backtest(services, active["uid"], head)
        if stored is not None:
            scores = registry.from_json(stored.get("scores"), {}).get("model")
            backtest_uid = stored["uid"]
    else:
        asked = _questions(manifest).get(record["question"])
        declared = manifest.models.get(asked.model or "") if asked is not None else None
        if declared is None and models:
            declared = manifest.models.get(models[0]["spec"])
        if declared is not None:
            try:
                layout, _ = _layout(services, manifest, declared, head)
            except DtaError as exc:
                log.warn(
                    "the folds of a specification cannot be laid out",
                    question=record["question"],
                    code=exc.code,
                )
                layout = None
            if layout is not None and not layout.enough:
                enough, few = False, _too_few(layout, declared)
    return judge_question(
        record,
        active=active,
        scores=scores,
        backtest_uid=backtest_uid,
        enough=enough,
        few=few,
        head=head,
    )


def measure_work(services: Services, questions: Sequence[str] | None) -> Work:
    """Record, for each question asked for (all when none), what it may claim at the head
    in force when the job starts."""

    def work() -> Outcome:
        started = time.monotonic()
        head = services.store.head()
        manifest = services.twin.manifest(at=head)
        records = {
            row["question"]: row for row in registry.rows(services, registry.QUESTION_TYPE, head)
        }
        wanted = list(dict.fromkeys(questions)) if questions is not None else sorted(records)
        unknown = [found for found in wanted if found not in records]
        if unknown:
            raise ApiError(
                f"no question {', '.join(unknown)} is registered", code="question.not_found"
            )
        models: dict[str, list[dict[str, Any]]] = {}
        for model in registry.rows(services, registry.MODEL_TYPE, head):
            if model.get("question") and model["status"] != "retired":
                models.setdefault(model["question"], []).append(model)
        declared = _questions(manifest)
        rows, found = [], []
        for question_id in wanted:
            record = records[question_id]
            behind = declared.get(question_id)
            if models.get(question_id) or (behind is not None and behind.model):
                row = _measure_model(services, manifest, record, models.get(question_id, []), head)
            else:
                example = behind.example if behind is not None else {}
                row = _measure_function(services, manifest, record, head, example)
            rows.append(row)
            found.append(
                {
                    "id": question_id,
                    "status": row["status"],
                    "basis": row["basis"],
                    "measured_error": row["measured_error"],
                    "tolerance": record.get("tolerance"),
                    "metric": record.get("metric"),
                    "reason": row["reason"],
                    "model": row["model"],
                    "backtest": row["backtest"],
                }
            )
            log.info(
                "question measured",
                question=question_id,
                status=row["status"],
                basis=row["basis"],
                measured_error=row["measured_error"],
            )
        version = None
        if rows:
            version = registry.commit(
                services,
                {registry.QUESTION_TYPE: rows},
                note="questions measured",
                extra={"measured": wanted, "at": head.id},
                t_valid=head.t,
            )
        log.info(
            "measuring finished",
            questions=len(rows),
            at_seq=head.seq,
            wrote=None if version is None else version.seq,
            duration_ms=round((time.monotonic() - started) * 1000.0),
        )
        return Outcome(
            result={
                "at": version_json(head),
                "version": None if version is None else version_json(version),
                "questions": found,
            }
        )

    return work
