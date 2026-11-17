"""What the worker runs for a fit, a backtest and a prediction.

Each handler takes the request, a reader of the twin and the twin's artifacts
(`protocol.worker.Handler`): it reads its frames through the reader, hands
them to the family, stores what is produced through the artifacts and gives
back the result. It fills neither `duration_ms` nor `logs`: the worker does.

A refusal is a `DtaError` whose code the worker passes on; the API answers
each with the status beside it:

    model.unknown_family     400  no shipped family has the name
    model.bad_family         400  the twin's own entry is not a family, or gave a
                                  prediction or folds of another shape
    model.bad_params         400  a parameter, level, measure or baseline the family
                                  or the twin does not take
    model.bad_frame          400  a series too short, no series left, missing drivers
    model.not_interventional 400  interventions for a family that cannot hold under them
    model.bad_state          409  not a fitted state, of another family, another format,
                                  another major version of a library, or truncated
    model.failed             500  the family or its library raised; the message is in
                                  the log, the result names only the type

The frames' own codes (`training.*`, `store.*`) come through as they are.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pyarrow as pa
import utilsforecast.processing as ufp
from dta_core.errors import DtaError
from dta_core.families import FAMILIES
from dta_core.log import Log

from ..protocol.answers import Artifacts, Reader
from ..protocol.families import DS, MEAN, UNIQUE_ID, Family, Intervention, Y, high, low
from ..protocol.worker import (
    BacktestRequest,
    BacktestResult,
    FamilyRef,
    FitRequest,
    FitResult,
    FrameRef,
    PredictRequest,
    PredictResult,
)
from .errors import FAILED, NOT_INTERVENTIONAL, ModelError, bad_family, bad_frame, bad_params
from .families import label, make
from .scores import (
    CUTOFF,
    RESIDUALS_MEDIA_TYPE,
    check_metrics,
    intervals_from_residuals,
    read_residuals,
    residual_quantiles,
    residuals_file,
    score,
)
from .selection import choose_refit
from .series import (
    NAMED,
    Prepared,
    features_of,
    from_arrow,
    outside,
    prediction_table,
    prepare,
    require_rows,
)
from .shipped import level_values
from .state import MEDIA_TYPE, dump, load

log = Log("models.ops")

BASELINE = "baseline"
NO_INTERVALS = "no_intervals"
OUTSIDE_VALIDITY = "outside_validity"
# How many distinct warnings of one library call are logged.
_WARNINGS = 5
# The clock a backtest times its first fit by, named so that a test can set the time.
_clock = time.perf_counter


def _call(what: str, family: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one call of a family, timed and logged. Whatever it raises that is not a
    refusal of the twin's becomes `model.failed`: the message stays in the log."""
    started = time.perf_counter()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = function(*args, **kwargs)
    except DtaError:
        raise
    except Exception as exc:
        log.error("the family raised", exc=exc, call=what, family=family)
        raise ModelError(
            f"{family}: {what} failed ({type(exc).__name__}); the cause is in the log",
            code=FAILED,
            detail={"family": family, "call": what, "error": type(exc).__name__},
        ) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    messages = dict.fromkeys(f"{found.category.__name__}: {found.message}" for found in caught)
    for message in list(messages)[:_WARNINGS]:
        log.warn("the library warned", call=what, family=family, warning=message[:300])
    log.info("library call", call=what, family=family, elapsed_ms=elapsed_ms)
    return result


def _read(twin: Reader, ref: FrameRef, what: str) -> pd.DataFrame:
    started = time.perf_counter()
    frame = from_arrow(twin.frame(ref), what)
    log.info(
        f"{what} read",
        target=ref.target,
        rows=len(frame),
        series=int(frame[UNIQUE_ID].nunique()),
        columns=list(frame.columns),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return frame


def _inputs(family: Family, features: Sequence[str]) -> list[str]:
    """The feature columns the family reads: all of them unless it says which."""
    declared = getattr(family, "inputs", None)
    return list(declared(list(features))) if callable(declared) else list(features)


def _notes(*groups: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(note for group in groups for note in group))


def _family_notes(family: Family) -> list[str]:
    return [str(note) for note in getattr(family, "notes", None) or ()]


def _moment(value: Any) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def _validity(family: Family, name: str, frame: pd.DataFrame) -> dict[str, tuple[float, float]]:
    found = _call("validity", name, family.validity, frame)
    try:
        return {str(column): (float(low_), float(high_)) for column, (low_, high_) in found.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise bad_family(f"{name}: validity is not a range per column", entry=name) from exc


# -- fit ---------------------------------------------------------------------------------


def fit(request: FitRequest, twin: Reader, artifacts: Artifacts) -> FitResult:
    """Fit the family on the frame and store the state."""
    name, step = label(request.family), request.frame.step
    log.info("fit started", family=name, target=request.frame.target, horizon=request.horizon)
    family = make(request.family, step=step, twin=twin)
    frame = _read(twin, request.frame, "frame")
    prepared = prepare(frame, _inputs(family, features_of(frame)), step)
    state = _call("fit", name, family.fit, prepared.frame, request.params, horizon=request.horizon)
    validity = _validity(family, name, prepared.frame)
    gives = getattr(family, "levels", None)
    fitted_from = _moment(prepared.frame[DS].min())
    fitted_to = _moment(prepared.frame[DS].max())
    series = int(prepared.frame[UNIQUE_ID].nunique())
    content = dump(
        state,
        family=request.family,
        params=request.params,
        step=step,
        horizon=request.horizon,
        levels=gives(state) if callable(gives) else None,
        columns=list(frame.columns),
        fitted_from=fitted_from,
        fitted_to=fitted_to,
        series=series,
        validity=validity,
    )
    stored = artifacts.put(content, MEDIA_TYPE)
    result = FitResult(
        duration_ms=0.0,
        state=stored,
        series=series,
        rows=len(prepared.frame),
        fitted_from=fitted_from,
        fitted_to=fitted_to,
        validity=validity,
        notes=_notes(prepared.notes, _family_notes(family)),
    )
    log.info(
        "fit finished",
        family=name,
        state=stored,
        state_bytes=len(content),
        series=series,
        rows=result.rows,
        notes=result.notes,
    )
    return result


# -- backtest ----------------------------------------------------------------------------


def _fold_by_fold(
    family: Family,
    name: str,
    frame: pd.DataFrame,
    params: Mapping[str, Any],
    *,
    horizon: int,
    step: int,
    windows: int,
    refit: int | bool,
    levels: Sequence[float],
    inputs: Sequence[str],
    grid: timedelta,
) -> pd.DataFrame:
    """Walk forward with a family that has no evaluation of its own: fit at the origins
    `refit` says, predict from every one. The folds are the library's (its splits of the
    frame), so the origins are those its own `cross_validation` gives."""
    splits = ufp.backtest_splits(
        frame,
        n_windows=windows,
        h=horizon,
        id_col=UNIQUE_ID,
        time_col=DS,
        freq=pd.tseries.frequencies.to_offset(pd.Timedelta(grid)),
        step_size=step,
    )
    state = None
    parts = []
    for index, (cutoffs, train, valid) in enumerate(splits):
        # The libraries' rule: at the first origin, then at every `refit`-th.
        if index == 0 or (refit > 0 and index % refit == 0):
            state = _call("fit", name, family.fit, train, params, horizon=horizon)
        drivers = valid[[UNIQUE_ID, DS, *inputs]] if inputs else None
        predicted = _call(
            "predict", name, family.predict, state, train, horizon, levels=levels, drivers=drivers
        )
        truth = valid[[UNIQUE_ID, DS, Y]].merge(cutoffs, on=UNIQUE_ID)
        parts.append(truth.merge(_shaped(predicted, name), on=[UNIQUE_ID, DS], how="left"))
    folds = pd.concat(parts, ignore_index=True)
    first = [UNIQUE_ID, DS, CUTOFF, Y]
    return folds[first + [column for column in folds.columns if column not in first]]


def _shaped(prediction: Any, name: str) -> pd.DataFrame:
    if not isinstance(prediction, pd.DataFrame) or any(
        column not in prediction.columns for column in (UNIQUE_ID, DS, MEAN)
    ):
        raise bad_family(
            f"{name}: a prediction is a frame with {UNIQUE_ID}, {DS} and {MEAN}", entry=name
        )
    return prediction


def _checked_folds(folds: Any, name: str) -> pd.DataFrame:
    wanted = (UNIQUE_ID, DS, CUTOFF, Y, MEAN)
    if not isinstance(folds, pd.DataFrame) or any(column not in folds.columns for column in wanted):
        raise bad_family(f"{name}: folds are a frame with {', '.join(wanted)}", entry=name)
    missing = int(folds[MEAN].isna().sum())
    if missing:
        log.error("folds hold predictions that are not numbers", family=name, rows=missing)
        raise ModelError(
            f"{name}: {missing} predictions of the backtest are not numbers",
            code=FAILED,
            detail={"family": name, "rows": missing},
        )
    return folds.sort_values([UNIQUE_ID, CUTOFF, DS], kind="stable").reset_index(drop=True)


def _baseline_params(baseline: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The naive model has the candidate's season, when the candidate names one."""
    season = params.get("season_length")
    if isinstance(season, list) and season:
        season = min(season)
    if baseline == "seasonal_naive" and isinstance(season, int) and not isinstance(season, bool):
        return {"season_length": season}
    return {}


def backtest(request: BacktestRequest, twin: Reader, artifacts: Artifacts) -> BacktestResult:
    """Walk forward through the frame and score the family, and the baseline on the same
    folds; store the quantiles of the absolute error per step ahead."""
    name, grid = label(request.family), request.frame.step
    horizon, step, windows = request.horizon, request.step, request.windows
    log.info(
        "backtest started",
        family=name,
        target=request.frame.target,
        horizon=horizon,
        step=step,
        windows=windows,
        refit=request.refit,
        baseline=request.baseline,
    )
    levels = level_values(request.levels)
    if request.baseline is not None and not (
        request.baseline in FAMILIES and FAMILIES[request.baseline].baseline
    ):
        naive = [found.name for found in FAMILIES.values() if found.baseline]
        raise bad_params(
            f"{request.baseline!r} is not a naive family; the baselines are {', '.join(naive)}",
            param="baseline",
        )
    family = make(request.family, step=grid, twin=twin)
    frame = _read(twin, request.frame, "frame")
    check_metrics(request.metrics, frame[Y])
    inputs = _inputs(family, features_of(frame))
    prepared = prepare(frame, inputs, grid)
    history = prepared.frame
    test = horizon + step * (windows - 1)
    require_rows(
        history,
        test + 1,
        f"{windows} origins {step} steps apart, each predicting {horizon} steps",
    )

    # What refitting costs is judged by one fit on what the first origin knows.
    from_end = history.groupby(UNIQUE_ID, sort=False).cumcount(ascending=False)
    started = _clock()
    _call("fit", name, family.fit, history[from_end >= test], request.params, horizon=horizon)
    first_fit_s = _clock() - started
    refit = request.refit
    if refit is None:
        refit = choose_refit(first_fit_s, windows, request.budget_s)
        log.info(
            "refit chosen",
            family=name,
            refit=refit,
            first_fit_ms=round(first_fit_s * 1000.0, 1),
            windows=windows,
            budget_s=request.budget_s,
        )

    walk = getattr(family, "cross_validate", None)
    if callable(walk):
        folds = _call(
            "cross_validate",
            name,
            walk,
            history,
            request.params,
            horizon=horizon,
            step=step,
            windows=windows,
            refit=refit,
            levels=levels,
        )
    else:
        folds = _fold_by_fold(
            family,
            name,
            history,
            request.params,
            horizon=horizon,
            step=step,
            windows=windows,
            refit=refit,
            levels=levels,
            inputs=inputs,
            grid=grid,
        )
    folds = _checked_folds(folds, name)
    notes = _notes(prepared.notes, _family_notes(family))
    if any(low(level) not in folds.columns or high(level) not in folds.columns for level in levels):
        notes = _notes(notes, [NO_INTERVALS])

    models = [MEAN]
    if request.baseline is not None:
        naive_family = make(FamilyRef(name=request.baseline), step=grid, twin=twin)
        naive = _checked_folds(
            _call(
                "cross_validate",
                request.baseline,
                naive_family.cross_validate,  # type: ignore[attr-defined]
                history[[UNIQUE_ID, DS, Y]],
                _baseline_params(request.baseline, request.params),
                horizon=horizon,
                step=step,
                windows=windows,
                refit=True,
            ),
            request.baseline,
        )
        keys = [UNIQUE_ID, CUTOFF, DS]
        if not folds[keys].equals(naive[keys]):
            log.error(
                "the baseline's folds are not the candidate's",
                family=name,
                baseline=request.baseline,
                cutoffs=[str(found) for found in folds[CUTOFF].unique()[:NAMED]],
                baseline_cutoffs=[str(found) for found in naive[CUTOFF].unique()[:NAMED]],
            )
            raise ModelError(
                f"{name}: the baseline {request.baseline} was not evaluated on the same folds",
                code=FAILED,
                detail={"family": name, "baseline": request.baseline},
            )
        folds = folds.assign(**{BASELINE: naive[MEAN].to_numpy()})
        models.append(BASELINE)

    scores = score(folds, models, request.metrics)
    residuals = artifacts.put(residuals_file(residual_quantiles(folds, grid)), RESIDUALS_MEDIA_TYPE)
    result = BacktestResult(
        duration_ms=0.0,
        scores=scores[MEAN],
        baseline_scores=scores.get(BASELINE),
        folds=int(folds[CUTOFF].nunique()),
        series=int(folds[UNIQUE_ID].nunique()),
        refit=refit,
        first_fit_ms=first_fit_s * 1000.0,
        residuals=residuals,
        notes=notes,
    )
    log.info(
        "backtest finished",
        family=name,
        scores=result.scores,
        baseline_scores=result.baseline_scores,
        folds=result.folds,
        series=result.series,
        refit=refit,
        first_fit_ms=round(result.first_fit_ms, 1),
        residuals=residuals,
        notes=notes,
    )
    return result


# -- predict -----------------------------------------------------------------------------


def _drivers(
    twin: Reader, request: PredictRequest, prepared: Prepared, inputs: Sequence[str]
) -> pd.DataFrame | None:
    """The features' values over the horizon, for the series predicted."""
    if request.drivers is None:
        return None
    raw = _read(twin, request.drivers, "drivers")
    missing = [column for column in inputs if column not in raw.columns]
    if missing:
        raise bad_frame(
            f"the drivers lack {', '.join(missing)}, which the state was fitted with",
            missing=missing,
        )
    until = prepared.last + request.horizon * pd.Timedelta(request.history.step)
    kept = prepared.frame[UNIQUE_ID].unique()
    rows = raw[(raw[DS] > prepared.last) & (raw[DS] <= until) & raw[UNIQUE_ID].isin(kept)]
    drivers = rows[[UNIQUE_ID, DS, *inputs]].sort_values([UNIQUE_ID, DS]).reset_index(drop=True)
    known = drivers.dropna().groupby(UNIQUE_ID).size().reindex(kept, fill_value=0)
    short = known[known < request.horizon]
    if inputs and len(short):
        names = list(short.index[:NAMED])
        raise bad_frame(
            f"the drivers do not hold every feature at each of the {request.horizon} steps "
            f"after the history for {len(short)} series: {', '.join(names)}",
            series=names,
        )
    return drivers


def predict(
    request: PredictRequest, twin: Reader, artifacts: Artifacts
) -> tuple[pa.Table, PredictResult]:
    """Apply a fitted state to the history: `horizon` steps after its last time."""
    name, step = label(request.family), request.history.step
    log.info(
        "predict started",
        family=name,
        target=request.history.target,
        horizon=request.horizon,
        levels=request.levels,
        interventions=len(request.interventions),
    )
    levels = level_values(request.levels)
    family = make(request.family, step=step, twin=twin)
    if request.interventions and not family.interventional:
        raise ModelError(
            f"{name} does not say its predictions hold under interventions; "
            f"{len(request.interventions)} were given",
            code=NOT_INTERVENTIONAL,
            detail={"family": name},
        )
    header, state = load(artifacts.get(request.state), request.family)
    if header["step"] != step.total_seconds():
        raise bad_frame(
            f"the state was fitted on a grid of {header['step']:g} s, the history has a "
            f"step of {step.total_seconds():g} s",
            fitted_step=header["step"],
        )
    features = [column for column in header["columns"] if column not in (UNIQUE_ID, DS, Y)]
    inputs = _inputs(family, features)
    history = _read(twin, request.history, "history")
    missing = [column for column in inputs if column not in history.columns]
    if missing:
        raise bad_frame(
            f"the history lacks {', '.join(missing)}, which the state was fitted with",
            missing=missing,
        )
    prepared = prepare(history, inputs, step)
    drivers = _drivers(twin, request, prepared, inputs)
    interventions = [Intervention(**found.model_dump()) for found in request.interventions]

    notes = list(prepared.notes)
    fitted_to = pd.Timestamp(header["fitted_to"])
    unseen = prepared.frame[prepared.frame[DS] > fitted_to]
    beyond = outside(unseen, header["validity"])
    if drivers is not None:
        beyond += outside(drivers, header["validity"])
    if beyond:
        # Asked beyond what the fit saw: the prediction is made, and marked.
        notes.append(OUTSIDE_VALIDITY)
        log.warn("inputs outside the range the fit saw", family=name, columns=sorted(set(beyond)))

    prediction = _shaped(
        _call(
            "predict",
            name,
            family.predict,
            state,
            prepared.frame,
            request.horizon,
            levels=levels,
            drivers=drivers,
            interventions=interventions,
        ),
        name,
    )
    if prediction[MEAN].isna().any():
        rows = int(prediction[MEAN].isna().sum())
        log.error("a prediction holds values that are not numbers", family=name, rows=rows)
        raise ModelError(
            f"{name}: {rows} predicted values are not numbers",
            code=FAILED,
            detail={"family": name, "rows": rows},
        )
    without = [
        level
        for level in levels
        if low(level) not in prediction.columns or high(level) not in prediction.columns
    ]
    if without and request.residuals is not None:
        # The family has no intervals of its own: the errors of its backtest give them.
        stored = read_residuals(artifacts.get(request.residuals))
        prediction = intervals_from_residuals(prediction, stored, without)
        log.info("intervals taken from the backtest's residuals", family=name, levels=without)
        without = []
    if without:
        notes.append(NO_INTERVALS)
    table = prediction_table(prediction, [level for level in levels if level not in without])
    result = PredictResult(
        duration_ms=0.0,
        rows=table.num_rows,
        series=int(prediction[UNIQUE_ID].nunique()),
        notes=_notes(notes, _family_notes(family)),
    )
    log.info(
        "predict finished", family=name, rows=result.rows, series=result.series, notes=result.notes
    )
    return table, result
