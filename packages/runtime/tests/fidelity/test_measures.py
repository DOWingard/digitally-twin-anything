"""The error of an origin, the window error, a breach and a warning, by hand."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from dta_runtime.fidelity import measures
from dta_runtime.fidelity.measures import (
    Undefined,
    breach,
    evidence,
    in_window,
    score_origin,
    warning_due,
    warning_sum,
    window_error,
)

T0 = datetime(2026, 1, 5, tzinfo=UTC)
D = timedelta(minutes=15)

# Two series: a at two times, b at one.
SERIES = ["a", "a", "b"]
Y = [1.0, 2.0, 3.0]
YHAT = [0.0, 4.0, 3.5]


def test_mae_is_the_mean_over_series_of_each_series_mean():
    score = score_origin(SERIES, Y, YHAT, "mae")
    # a: (|1| + |-2|) / 2 = 1.5; b: 0.5; across: 1.0.
    assert score.error == pytest.approx(1.0)
    assert score.n == 3
    # Observed less predicted, per series: a: (1 - 2) / 2; b: -0.5.
    assert score.residuals == pytest.approx({"a": -0.5, "b": -0.5})


def test_rmse_mape_and_max_abs():
    rmse = score_origin(SERIES, Y, YHAT, "rmse").error
    assert rmse == pytest.approx((math.sqrt((1 + 4) / 2) + 0.5) / 2)
    mape = score_origin(SERIES, Y, YHAT, "mape").error
    assert mape == pytest.approx(((1 / 1 + 2 / 2) / 2 + 0.5 / 3) / 2)
    assert score_origin(SERIES, Y, YHAT, "max_abs").error == pytest.approx(2.0)


def test_error_rate_counts_rounded_predictions_that_miss():
    score = score_origin(["p", "p", "q"], [1.0, 0.0, 3.0], [1.2, 0.7, 3.4], "error_rate")
    # p: 1 == 1, 1 != 0 -> 0.5; q: 3 == 3 -> 0; across: 0.25.
    assert score.error == pytest.approx(0.25)


def test_metrics_without_a_value_are_refused():
    with pytest.raises(Undefined):
        score_origin(["a"], [0.0], [1.0], "mape")
    with pytest.raises(Undefined):
        score_origin(["a"], [1.5], [1.0], "error_rate")
    with pytest.raises(Undefined):
        score_origin([], [], [], "mae")
    with pytest.raises(Undefined):
        score_origin(["a"], [1.0], [1.0], "median")


def test_the_window_holds_origins_whose_forecast_ends_inside_it_and_after_the_fit():
    now, horizon, window = T0 + 10 * D, 2 * D, 4 * D
    # (now - W, now] on o + h: o in (4D, 8D].
    assert not in_window(T0 + 4 * D, now=now, horizon=horizon, window=window)
    assert in_window(T0 + 5 * D, now=now, horizon=horizon, window=window)
    assert in_window(T0 + 8 * D, now=now, horizon=horizon, window=window)
    assert not in_window(T0 + 9 * D, now=now, horizon=horizon, window=window)
    scored = [(T0 + i * D, float(i)) for i in range(4, 10)]
    error, n = window_error(scored, now=now, horizon=horizon, window=window, fitted_to=T0 + 6 * D)
    assert (error, n) == (pytest.approx((6 + 7 + 8) / 3), 3)
    assert window_error([], now=now, horizon=horizon, window=window, fitted_to=None) == (None, 0)


def test_a_breach_needs_three_origins_and_an_error_over_the_tolerance():
    tolerance = 0.5
    assert not breach(10.0, 2, tolerance)
    assert not breach(0.5, 3, tolerance)
    assert breach(0.5000001, 3, tolerance)
    assert not breach(None, 0, tolerance)
    assert measures.MIN_ORIGINS == 3


def test_the_warning_sum_and_its_limit():
    tolerance = 0.5
    # x = e / epsilon; S <- max(0, S + x - 0.8).
    first = warning_sum(0.0, 0.7, tolerance)
    assert first == pytest.approx(0.6)
    second = warning_sum(first, 0.7, tolerance)
    assert second == pytest.approx(1.2)
    assert warning_sum(0.3, 0.0, tolerance) == 0.0
    assert not warning_due(first, None, now=T0, window=4 * D)
    assert warning_due(second, None, now=T0, window=4 * D)
    # Just under and just over 1.0.
    assert not warning_due(1.0, None, now=T0, window=4 * D)
    assert warning_due(1.0 + 1e-9, None, now=T0, window=4 * D)
    # Not again within the window.
    assert not warning_due(2.0, T0 - 3 * D, now=T0, window=4 * D)
    assert warning_due(2.0, T0 - 4 * D, now=T0, window=4 * D)


def test_the_evidence_of_an_incident_counts_origins_at_or_after_its_cut():
    scored = [(T0 + i * D, e) for i, e in enumerate([2.0, 0.1, 0.2, 0.3, 0.9])]
    found = evidence(scored, T0 + D)
    assert (found.n, found.error) == (4, pytest.approx(1.5 / 4))
    assert found.holds(0.5) is True
    # Two origins are not enough to say.
    assert evidence(scored, T0 + 3 * D).holds(0.5) is None
    # (0.2 + 0.3 + 0.9) / 3 = 0.4667.
    assert evidence(scored, T0 + 2 * D).holds(0.5) is True
    assert evidence(scored, T0 + 2 * D).holds(0.4) is False
    assert evidence(scored, None).n == 0
