"""How a backtest is laid out, when a candidate is accepted and which one is selected."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pytest
from dta_core.errors import DtaError
from dta_core.families import ORDER
from dta_core.manifest.models import Acceptance
from dta_runtime.models import (
    Plan,
    Verdict,
    choose_refit,
    horizon_steps,
    judge,
    plan,
    select,
)

STEP = timedelta(minutes=15)


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def test_a_horizon_is_the_fewest_steps_that_cover_it():
    assert horizon_steps(minutes(120), STEP) == 8
    assert horizon_steps(minutes(121), STEP) == 9
    assert horizon_steps(minutes(1), STEP) == 1
    assert horizon_steps(STEP, STEP) == 1
    for horizon, step in ((minutes(0), STEP), (minutes(-15), STEP), (STEP, minutes(0))):
        with pytest.raises(DtaError) as caught:
            horizon_steps(horizon, step)
        assert caught.value.code == "model.bad_params"


def laid_out(rows: int, **more) -> Plan:
    return plan(
        rows,
        **{
            "initial_window": minutes(15 * 96),
            "origin_step": minutes(60),
            "horizon": minutes(120),
            "step": STEP,
            "min_folds": 3,
            **more,
        },
    )


def test_the_folds_a_history_has_room_for():
    # w = 96, h = 8, s = 4: windows = (n - 104) // 4 + 1.
    assert laid_out(104) == Plan(horizon=8, step=4, windows=1, enough=False)
    # One row short of the initial window and a horizon: no fold.
    assert laid_out(103) == Plan(horizon=8, step=4, windows=0, enough=False)
    assert laid_out(0).windows == 0
    assert laid_out(107).windows == 1
    assert laid_out(108).windows == 2
    assert laid_out(112) == Plan(horizon=8, step=4, windows=3, enough=True)
    assert laid_out(111).enough is False
    assert laid_out(672).windows == (672 - 104) // 4 + 1
    # Durations that are not whole steps are rounded up, each on its own.
    rounded = laid_out(30, initial_window=minutes(100), horizon=minutes(31), origin_step=STEP)
    assert rounded == Plan(horizon=3, step=1, windows=30 - 7 - 3 + 1, enough=True)
    assert laid_out(104, min_folds=1).enough is True


@pytest.mark.parametrize(
    "more",
    [
        {"origin_step": minutes(20)},
        {"origin_step": minutes(0)},
        {"origin_step": minutes(-15)},
        {"initial_window": minutes(0)},
        {"horizon": minutes(0)},
        {"step": minutes(0)},
    ],
)
def test_a_layout_that_cannot_be_is_refused(more):
    with pytest.raises(DtaError) as caught:
        laid_out(500, **more)
    assert caught.value.code == "model.bad_params"


def test_refitting_is_as_frequent_as_half_the_budget_allows():
    # 20 origins, 120 s to spend on fits.
    assert choose_refit(1.0, 20, 240.0) == 1
    assert choose_refit(6.0, 20, 240.0) == 1
    # 20 fits of 6.1 s are more than 120 s; 10 are not.
    assert choose_refit(6.1, 20, 240.0) == 2
    assert choose_refit(30.0, 20, 240.0) == 5
    # ceil(20 / 7) = 3 fits of 40 s.
    assert choose_refit(40.0, 20, 240.0) == 7
    assert choose_refit(60.0, 20, 240.0) == 10
    assert choose_refit(120.0, 20, 240.0) == 20
    # Even one fit is more than half the budget: fit once and say so.
    assert choose_refit(120.1, 20, 240.0) is False
    assert choose_refit(0.0, 1, 1.0) == 1
    assert choose_refit(1.0, 1, 1.0) is False
    assert choose_refit(0.5, 1, 1.0) == 1


def accept(threshold: float = 0.5, baseline: str | None = "persistence") -> Acceptance:
    return Acceptance(metric="mae", threshold=threshold, baseline=baseline)


def test_a_candidate_is_accepted_within_the_threshold_and_below_the_baseline():
    assert judge({"mae": 0.3, "rmse": 9.0}, {"mae": 0.6}, accept()) == Verdict(
        accepted=True, reasons=(), error=0.3, skill=0.5
    )
    # At the threshold is within it.
    assert judge({"mae": 0.5}, {"mae": 0.6}, accept()).accepted
    above = judge({"mae": 0.51}, {"mae": 0.6}, accept())
    assert (above.accepted, above.reasons) == (False, ("above_threshold",))
    assert above.skill == pytest.approx(1 - 0.51 / 0.6)
    # As good as the baseline is not better than it.
    equal = judge({"mae": 0.4}, {"mae": 0.4}, accept())
    assert (equal.accepted, equal.reasons, equal.skill) == (
        False,
        ("not_better_than_baseline",),
        0.0,
    )
    both = judge({"mae": 0.9}, {"mae": 0.6}, accept())
    assert both.reasons == ("above_threshold", "not_better_than_baseline")
    assert both.skill == pytest.approx(-0.5)
    # A baseline that was never wrong cannot be beaten, and gives no skill.
    perfect = judge({"mae": 0.0}, {"mae": 0.0}, accept())
    assert (perfect.accepted, perfect.skill) == (False, None)
    # Without a declared baseline only the threshold counts, whatever was scored.
    alone = judge({"mae": 0.4}, {"mae": 0.1}, accept(baseline=None))
    assert (alone.accepted, alone.reasons, alone.skill) == (True, (), None)
    assert judge({"mae": 0.4}, None, accept(baseline=None)).accepted
    # An error that is not a number is never accepted.
    assert not judge({"mae": float("nan")}, {"mae": 0.6}, accept()).accepted


def test_a_verdict_needs_the_measure_it_is_judged_in():
    for scores, baseline in (({"rmse": 0.1}, {"mae": 0.2}), ({"mae": 0.1}, None),
                             ({"mae": 0.1}, {"rmse": 0.2})):  # fmt: skip
        with pytest.raises(DtaError) as caught:
            judge(scores, baseline, accept())
        assert caught.value.code == "model.bad_params"


def verdict(error: float, accepted: bool = True) -> Verdict:
    return Verdict(accepted=accepted, reasons=() if accepted else ("above_threshold",),
                   error=error, skill=None)  # fmt: skip


def test_the_lowest_error_is_selected_and_ties_go_to_the_simpler_family():
    assert ORDER.index("theta") < ORDER.index("ets") < ORDER.index("gbm_lags")
    families = {"a": "gbm_lags", "b": "ets", "c": "theta", "d": "plant.hydraulics:Network"}
    assert select({"a": verdict(0.2), "b": verdict(0.3)}, families) == "a"
    # Not accepted: not selected, however low its error.
    assert select({"a": verdict(0.1, accepted=False), "b": verdict(0.3)}, families) == "b"
    assert select({"a": verdict(0.1, accepted=False)}, families) is None
    assert select({}, families) is None
    # Equal errors: the family earlier among the shipped ones.
    assert select({"a": verdict(0.3), "b": verdict(0.3), "c": verdict(0.3)}, families) == "c"
    # Equal to 1e-12 of the error is equal; further apart is not.
    assert select({"a": verdict(0.3), "b": verdict(0.3 * (1 + 5e-13))}, families) == "b"
    assert select({"a": verdict(0.3), "b": verdict(0.3 * (1 + 5e-12))}, families) == "a"
    # A family of the twin's own comes after every shipped one.
    assert select({"a": verdict(0.3), "d": verdict(0.3)}, families) == "a"
    assert select({"d": verdict(0.3)}, families) == "d"
    # The same family twice: the candidate whose id sorts first.
    same = {"z": "ets", "m": "ets", "q": "ets"}
    assert select({name: verdict(0.3) for name in same}, same) == "m"
    assert (
        select({"own2": verdict(0.0), "own1": verdict(0.0)}, {"own1": "x:Y", "own2": "x:Y"})
        == "own1"
    )


@pytest.mark.parametrize(
    "module", ["dta_runtime.models", "dta_runtime.models.selection", "dta_runtime.training"]
)
def test_what_the_api_imports_brings_no_model_library(module):
    heavy = ["statsforecast", "mlforecast", "utilsforecast", "sklearn", "pandas", "scipy",
             "coreforecast"]  # fmt: skip
    if module != "dta_runtime.training":
        # Frames are joined in DuckDB over Arrow, which rest on numpy; rules do not.
        heavy.append("numpy")
    script = (
        f"import sys, {module}\n"
        f"found = sorted(name for name in {heavy!r} if name in sys.modules)\n"
        "print(','.join(found))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "", f"{module} imports {done.stdout.strip()}"
