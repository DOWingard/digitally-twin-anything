"""The stuck and the bias test of an observed signal, at the edges of their constants."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from dta_runtime.fidelity.quality import (
    FEW_CHANGES,
    FORECAST,
    INDEPENDENT,
    MARKOV,
    NEVER_CHANGED,
    NO_ALARM,
    NOT_TESTED,
    OTHER_SIDE,
    SMOOTH,
    STUCK_ALPHA,
    BiasTest,
    StuckTest,
    against_forecast,
    bias_test,
    biased,
    changes_per_interval,
    forecast_margin,
    needed_run,
    run_probability,
    steps_per_interval,
    stuck_test,
    uncorroborated,
    wants_forecast,
    without_forecast,
)

T0 = datetime(2026, 1, 5, tzinfo=UTC)
D = timedelta(minutes=15)


def test_changes_are_counted_per_interval_and_a_value_written_again_is_none():
    events = [
        (T0 + D / 2, 2),  # interval 0 changes
        (T0 + D, 2),  # the same value at the end of interval 0
        (T0 + 1.5 * D, 3),  # interval 1
        (T0 + 3 * D, 3),  # interval 2: written again, no change
        (T0 + 4 * D, 4),  # interval 3
        (T0 + 5 * D, 5),  # past the lookback
    ]
    assert changes_per_interval(T0, D, 4, 1, events) == [True, True, False, True]
    assert changes_per_interval(T0, D, 2, None, [(T0 - D, 7), (T0 + D, 7)]) == [False, False]
    # How far it moved over each interval, from the value before it to the last in it.
    assert steps_per_interval(T0, D, 4, 1, events) == [1.0, 1.0, None, 1.0]
    text = steps_per_interval(T0, D, 1, "a", [(T0 + D, "b")])
    assert len(text) == 1 and math.isnan(text[0])


def _stuck(k: int, u: int, n: int, *, idle: int = 0) -> tuple[list[bool], list[bool]]:
    """k intervals ending with a change, u of them without one, then n delivering
    intervals and `idle` in which the source delivered nothing."""
    changed = [False] * u + [True] * (k - u)
    changed += [False] * (n + idle)
    delivering = [True] * k + [True] * n + [False] * idle
    return changed, delivering


def test_a_signal_that_changes_every_interval_is_stuck_at_the_sixth_in_a_short_lookback():
    # k = 21, u = 0: p0 = 1/23; p0^5 = 1.6e-7 is not under 1e-7, p0^6 = 6.7e-9 is.
    not_yet = stuck_test(*_stuck(21, 0, 5))
    assert not_yet.tested and not not_yet.stuck
    assert not_yet.p0 == pytest.approx(1 / 23)
    assert not_yet.model == INDEPENDENT and not_yet.needed == 6
    found = stuck_test(*_stuck(21, 0, 6))
    assert found.stuck
    assert (found.k, found.u, found.n) == (21, 0, 6)
    assert found.document()["p"] == pytest.approx((1 / 23) ** 6)
    assert found.document()["alpha"] == STUCK_ALPHA == 1e-7


def test_in_a_day_of_intervals_it_is_stuck_at_the_fourth():
    # k = 93, u = 0: p0 = 1/95; p0^3 = 1.2e-6, p0^4 = 1.2e-8.
    assert not stuck_test(*_stuck(93, 0, 3)).stuck
    assert stuck_test(*_stuck(92, 0, 4)).stuck


def test_a_signal_that_changed_less_often_needs_more_intervals():
    # k = 22, u = 1: p0 = 1/12; p0^6 = 3.3e-7, p0^7 = 2.8e-8.
    assert not stuck_test(*_stuck(22, 1, 6)).stuck
    found = stuck_test(*_stuck(22, 1, 7))
    assert found.stuck and found.needed == 7


def test_no_change_intervals_that_cluster_need_a_run_as_long_as_their_clusters_say():
    # Eight changes, then three intervals without one, eight times, then a change:
    # k = 89, u = 24, p0 = 25/91. Taken as independent draws a run of 13 would be called
    # stuck; the clusters (n_NN = 16 of the 24 transitions out of N, G-test p = 1e-6)
    # give P(N | N) = a = 0.637 and P(N | C) = b = 0.130, and 33 are needed.
    history = ([True] * 8 + [False] * 3) * 8 + [True]
    found = stuck_test([*history, *[False] * 20], [True] * (len(history) + 20))
    assert (found.k, found.u) == (89, 24)
    assert (found.cc, found.cn, found.nc, found.nn) == (56, 8, 8, 16)
    assert found.model == MARKOV and found.cluster_p < 0.01
    assert found.a == pytest.approx((16 + 2 * 25 / 91) / 26)
    assert found.b == pytest.approx((8 + 2 * 25 / 91) / 66)
    assert found.needed == 33 and not found.stuck
    assert needed_run(25 / 91, 25 / 91) == 13
    assert (
        run_probability(found.b, found.a, 33) < STUCK_ALPHA <= run_probability(found.b, found.a, 32)
    )
    assert stuck_test([*history, *[False] * 33], [True] * (len(history) + 33)).stuck


def test_a_smooth_signal_is_not_tested_and_a_sawtooth_is():
    # A level rising by one step of resolution every other interval: every change keeps
    # its sign. However long it stays flat, the length of its run does not call it stuck:
    # it is left to the forecast.
    values = [round(10 + 0.005 * index, 2) for index in range(96)]
    steps = [None] + [None if b == a else b - a for a, b in zip(values, values[1:], strict=False)]
    changed = [found is not None for found in steps]
    found = stuck_test(changed + [False] * 30, [True] * 126, steps + [None] * 30)
    assert not found.tested and not found.stuck and found.skipped == SMOOTH
    assert (found.pairs, found.kept) == (46, 46) and found.smooth_p < 0.01
    assert wants_forecast(found) and found.n == 31
    # Without the steps nothing is taken for smooth.
    assert stuck_test(changed + [False] * 30, [True] * 126).tested
    # Three steps up and one down keep the sign half the time: tested.
    saw = [10 + 0.5 * (index % 4) for index in range(97)]
    steps = [b - a for a, b in zip(saw, saw[1:], strict=False)][:93] + [None] * 3
    found = stuck_test([True] * 93 + [False] * 3, [True] * 96, steps)
    assert found.tested and found.skipped is None and found.smooth_p > 0.5


def test_a_smooth_signal_in_a_run_is_stuck_when_its_reading_lies_off_the_forecast():
    smooth = StuckTest(False, False, 3, 40, 2, 0.07, 39, skipped=SMOOTH)
    assert wants_forecast(smooth)
    assert not wants_forecast(StuckTest(False, False, 2, 40, 2, 0.07, 39, skipped=SMOOTH))
    assert not wants_forecast(StuckTest(False, False, 9, 10, 2, None, 9, skipped=FEW_CHANGES))
    # Half the tolerance, in the property's units or relative to the reading.
    assert forecast_margin("mae", 0.5, 11.41) == 0.25
    assert forecast_margin("rmse", 0.5, 11.41) == forecast_margin("max_abs", 0.5, 11.41) == 0.25
    assert forecast_margin("mape", 0.05, -11.41) == pytest.approx(0.28525)
    assert forecast_margin("mape", 0.05, 0.0) is None
    assert forecast_margin("error_rate", 0.1, 3.0) is None
    assert forecast_margin("mae", None, 3.0) is None
    document = {"question": "level", "lead": 3, "value": 10.3, "reading": 10.0}
    found = against_forecast(smooth, value=10.3, reading=10.0, margin=0.25, document=document)
    assert (found.tested, found.stuck, found.skipped, found.model) == (True, True, None, FORECAST)
    assert found.document()["forecast"] == document
    # On the margin is not past it.
    assert not against_forecast(smooth, value=10.25, reading=10.0, margin=0.25, document={}).stuck
    untested = without_forecast(smooth, {"refused": {"code": "no_question"}})
    assert (untested.tested, untested.stuck, untested.skipped) == (False, False, SMOOTH)
    assert untested.document()["forecast"]["refused"]["code"] == "no_question"


def test_only_signals_that_changed_in_twenty_intervals_are_tested():
    below = stuck_test(*_stuck(20, 1, 10))
    assert not below.tested and not below.stuck and below.skipped == FEW_CHANGES
    edge = stuck_test(*_stuck(20, 0, 10))
    assert edge.tested and edge.stuck


def test_intervals_in_which_the_source_delivered_nothing_do_not_count():
    found = stuck_test(*_stuck(21, 0, 5, idle=5))
    assert found.n == 5 and not found.stuck


def test_a_status_that_seldom_changes_is_never_called_stuck():
    changed = [index % 10 == 0 for index in range(96)]
    found = stuck_test(changed, [True] * 96)
    assert not found.tested and not found.stuck
    never = stuck_test([False] * 8, [True] * 8)
    assert never.last is None and never.skipped == NEVER_CHANGED


TOLERANCE = 1.0
CALM = [0.0, 0.1, -0.1, 0.2, -0.2]


def test_bias_needs_eight_origins():
    assert not bias_test(CALM + [1.0, 1.0], TOLERANCE).tested
    found = bias_test(CALM + [1.0, 1.0, 1.0], TOLERANCE)
    assert found.tested and found.alarm
    # Median 0, MAD 0.1: sigma = 1.4826 * 0.1; z = 1 / 0.14826 = 6.74 each.
    assert found.median == pytest.approx(0.0)
    assert found.sigma == pytest.approx(0.14826)
    assert found.z == pytest.approx((1 / 0.14826,) * 3)
    assert found.s > 5.0


def test_the_cusum_threshold_with_the_floor_on_sigma():
    flat = [0.0] * 5
    # MAD 0: sigma is the floor, 0.1 * tolerance. z = 2: S = 1.5, 3.0, 4.5 <= 5.
    under = bias_test(flat + [0.2] * 3, TOLERANCE)
    assert under.sigma == pytest.approx(0.1)
    assert under.upper == pytest.approx(4.5)
    assert not under.alarm
    # z = 2.5: S = 2, 4, 6 > 5; but a quarter of the tolerance is not material.
    small = bias_test(flat + [0.25] * 3, TOLERANCE)
    assert small.upper == pytest.approx(6.0)
    assert small.shift == pytest.approx(0.25)
    assert not small.alarm
    # The same steadiness with a shift of the tolerance itself: an alarm.
    over = bias_test(flat + [1.0] * 3, TOLERANCE)
    assert over.upper == pytest.approx(28.5) and over.shift == pytest.approx(1.0)
    assert over.alarm
    falling = bias_test(flat + [-1.0] * 3, TOLERANCE)
    assert falling.alarm and falling.s == pytest.approx(-28.5)
    assert not bias_test(flat + [0.99] * 3, TOLERANCE).alarm


def test_a_material_shift_inside_the_series_own_spread_is_no_alarm():
    # Baseline spread: MAD 1, sigma 1.4826. A shift of 1.2 is material against the
    # tolerance and still within what the series does by itself.
    spread = [-2.0, -1.0, 0.0, 1.0, 2.0]
    found = bias_test(spread + [1.2] * 3, TOLERANCE)
    assert found.sigma == pytest.approx(1.4826)
    assert found.shift == pytest.approx(1.2)
    assert found.upper < 5.0 and not found.alarm


def test_the_baseline_leaves_out_the_newest_three():
    # The baseline is the first five only; the shift sits in the newest three.
    found = bias_test([5.0] * 5 + [5.05, 4.95, 5.0], TOLERANCE)
    assert found.median == pytest.approx(5.0)
    assert not found.alarm


def _results(alarms: int, quiet: int, untested: int = 0) -> dict[str, BiasTest]:
    found: dict[str, BiasTest] = {}
    for index in range(alarms):
        found[f"a{index}"] = BiasTest(True, True, 8)
    for index in range(quiet):
        found[f"q{index}"] = BiasTest(True, False, 8)
    for index in range(untested):
        found[f"u{index}"] = BiasTest(False, False, 3)
    return found


def test_series_are_biased_only_when_fewer_than_half_alarm():
    assert biased(_results(1, 3)) == {"a0"}
    # Exactly half: it is the model.
    assert biased(_results(2, 2)) == set()
    assert biased(_results(2, 3)) == {"a0", "a1"}
    assert biased(_results(3, 1)) == set()
    # Series without enough origins are not in the count.
    assert biased(_results(2, 2, untested=5)) == set()


def test_the_side_of_an_alarm_is_the_side_that_ended_above_h():
    flat = [0.0] * 5
    assert bias_test(flat + [1.0] * 3, TOLERANCE).side == 1
    assert bias_test(flat + [-1.0] * 3, TOLERANCE).side == -1
    assert bias_test(flat + [0.2] * 3, TOLERANCE).side == 0


def test_a_series_is_borne_out_only_by_every_other_question_alarming_on_its_side():
    flat = [0.0] * 5
    rising = bias_test(flat + [1.0] * 3, TOLERANCE)
    falling = bias_test(flat + [-1.0] * 3, TOLERANCE)
    quiet = bias_test(flat + [0.0] * 3, TOLERANCE)
    young = bias_test([1.0] * 3, TOLERANCE)
    assert uncorroborated(rising, {}) == {}
    assert uncorroborated(rising, {"b": rising, "c": rising}) == {}
    assert uncorroborated(rising, {"b": rising, "c": quiet}) == {"c": NO_ALARM}
    assert uncorroborated(falling, {"b": rising}) == {"b": OTHER_SIDE}
    assert uncorroborated(rising, {"b": young, "c": None}) == {"b": NOT_TESTED, "c": NOT_TESTED}
