"""How long the shipped families take on ten series of a week of quarter hours, in a
process that has the libraries imported. Run with `-m scale`."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from dta_core.families import ORDER
from dta_core.log import Log
from dta_runtime.models import ops
from models_testkit import (
    Frames,
    Kept,
    backtest_request,
    fit_request,
    frame,
    noisy,
    predict_request,
)

pytestmark = pytest.mark.scale
log = Log("test.speed")


def report(what: str, **measured: Any) -> None:
    """To the log, and to the terminal of a run with `-s`."""
    log.info(what, **measured)
    print(what, measured, flush=True)


SERIES, POINTS, HORIZON = 10, 672, 8
# Seconds; fixed before anything was measured.
PREDICT_S = 2.0
FIT_S = {"ets": 20.0, "theta": 20.0, "arima": 20.0, "mstl": 20.0}
FIT_OTHERS_S = 5.0
BACKTEST_S = 120.0
BACKTESTED = ("seasonal_naive", "theta", "ets", "ridge_lags")
WINDOWS, BUDGET_S = 20, 240.0

VALUES = {f"node:T{index}": noisy(POINTS, 96, index) for index in range(SERIES)}
TABLE = frame(VALUES)
# The same week seen from a day later: a history that begins elsewhere than the fit's.
LATER = frame({uid: values[96:] for uid, values in VALUES.items()}, first=96)


def timed(call: Callable[..., Any], *args: Any) -> tuple[float, Any]:
    started = time.perf_counter()
    result = call(*args)
    return time.perf_counter() - started, result


@pytest.fixture(scope="module")
def kept() -> Kept:
    return Kept()


@pytest.fixture(scope="module")
def fitted(kept: Kept) -> dict[str, tuple[float, str]]:
    """Every family fitted once with its defaults: how long it took, and the state."""
    import mlforecast  # noqa: F401
    import statsforecast  # noqa: F401

    found = {}
    for family in ORDER:
        took, result = timed(ops.fit, fit_request(family, HORIZON), Frames(fit=TABLE), kept)
        found[family] = (took, result.state)
        report(
            "speed: fit", family=family, seconds=round(took, 2), bytes=len(kept.files[result.state])
        )
    return found


@pytest.mark.parametrize("family", ORDER)
def test_a_fit_is_within_its_time(fitted, family):
    took, _ = fitted[family]
    assert took <= FIT_S.get(family, FIT_OTHERS_S), f"{family}: fit took {took:.2f} s"


@pytest.mark.parametrize("family", ORDER)
def test_a_prediction_from_a_stored_state_is_within_its_time(fitted, kept, family):
    _, state = fitted[family]
    request = predict_request(family, state, HORIZON, levels=[80, 95])
    took, (table, _) = timed(ops.predict, request, Frames(history=TABLE), kept)
    later, (shifted, result) = timed(ops.predict, request, Frames(history=LATER), kept)
    report(
        "speed: predict",
        family=family,
        seconds=round(took, 3),
        seconds_from_a_later_start=round(later, 3),
        notes=result.notes,
    )
    assert table.num_rows == shifted.num_rows == SERIES * HORIZON
    assert took <= PREDICT_S, f"{family}: predict took {took:.2f} s"
    assert later <= PREDICT_S, f"{family}: predict from a later start took {later:.2f} s"


@pytest.mark.parametrize("family", BACKTESTED)
def test_a_backtest_with_the_refit_chosen_for_its_budget_is_within_its_time(family):
    request = backtest_request(
        family,
        horizon=HORIZON,
        step=4,
        windows=WINDOWS,
        metrics=["mae", "rmse"],
        baseline="seasonal_naive",
        budget_s=BUDGET_S,
    )
    took, result = timed(ops.backtest, request, Frames(fit=TABLE), Kept())
    report(
        "speed: backtest",
        family=family,
        seconds=round(took, 2),
        refit=result.refit,
        first_fit_ms=round(result.first_fit_ms),
        mae=round(result.scores["mae"], 4),
        baseline_mae=round(result.baseline_scores["mae"], 4),
    )
    assert result.folds == WINDOWS
    assert took <= BACKTEST_S, f"{family}: backtest took {took:.1f} s"
