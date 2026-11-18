"""The stuck and the bias test of an observed signal, at the edges of their constants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dta_runtime.fidelity.quality import (
    NO_ALARM,
    NOT_TESTED,
    OTHER_SIDE,
    BiasTest,
    bias_test,
    biased,
    changes_per_interval,
    stuck_test,
    uncorroborated,
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


def _stuck(k: int, u: int, n: int, *, idle: int = 0) -> tuple[list[bool], list[bool]]:
    """k intervals ending with a change, u of them without one, then n delivering
    intervals and `idle` in which the source delivered nothing."""
    changed = [False] * u + [True] * (k - u)
    changed += [False] * (n + idle)
    delivering = [True] * k + [True] * n + [False] * idle
    return changed, delivering


def test_stuck_at_the_third_interval_when_p0_cubed_is_under_the_limit():
    # k = 21, u = 0: p0 = 1/23, p0^2 = 1.9e-3, p0^3 = 8.2e-5 < 1e-4.
    not_yet = stuck_test(*_stuck(21, 0, 2))
    assert not_yet.tested and not not_yet.stuck
    assert not_yet.p0 == pytest.approx(1 / 23)
    found = stuck_test(*_stuck(21, 0, 3))
    assert found.stuck
    assert (found.k, found.u, found.n) == (21, 0, 3)
    assert found.document()["p0_n"] == pytest.approx((1 / 23) ** 3)


def test_a_signal_that_changed_less_often_needs_more_intervals():
    # k = 22, u = 1: p0 = 2/24; p0^3 = 5.8e-4 > 1e-4; p0^4 = 4.8e-5 < 1e-4.
    assert not stuck_test(*_stuck(22, 1, 3)).stuck
    assert stuck_test(*_stuck(22, 1, 4)).stuck


def test_only_signals_that_changed_in_twenty_intervals_are_tested():
    below = stuck_test(*_stuck(20, 1, 10))
    assert not below.tested and not below.stuck
    edge = stuck_test(*_stuck(20, 0, 10))
    assert edge.tested and edge.stuck


def test_intervals_in_which_the_source_delivered_nothing_do_not_count():
    found = stuck_test(*_stuck(21, 0, 2, idle=5))
    assert found.n == 2 and not found.stuck


def test_a_status_that_seldom_changes_is_never_called_stuck():
    changed = [index % 10 == 0 for index in range(96)]
    found = stuck_test(changed, [True] * 96)
    assert not found.tested and not found.stuck
    assert stuck_test([False] * 8, [True] * 8).last is None


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
    # z = 2.5: S = 2, 4, 6 > 5.
    over = bias_test(flat + [0.25] * 3, TOLERANCE)
    assert over.upper == pytest.approx(6.0)
    assert over.alarm
    falling = bias_test(flat + [-0.25] * 3, TOLERANCE)
    assert falling.alarm and falling.s == pytest.approx(-6.0)


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
    assert bias_test(flat + [0.25] * 3, TOLERANCE).side == 1
    assert bias_test(flat + [-0.25] * 3, TOLERANCE).side == -1
    assert bias_test(flat + [0.2] * 3, TOLERANCE).side == 0


def test_a_series_is_borne_out_only_by_every_other_question_alarming_on_its_side():
    flat = [0.0] * 5
    rising = bias_test(flat + [0.25] * 3, TOLERANCE)
    falling = bias_test(flat + [-0.25] * 3, TOLERANCE)
    quiet = bias_test(flat + [0.0] * 3, TOLERANCE)
    young = bias_test([0.25] * 3, TOLERANCE)
    assert uncorroborated(rising, {}) == {}
    assert uncorroborated(rising, {"b": rising, "c": rising}) == {}
    assert uncorroborated(rising, {"b": rising, "c": quiet}) == {"c": NO_ALARM}
    assert uncorroborated(falling, {"b": rising}) == {"b": OTHER_SIDE}
    assert uncorroborated(rising, {"b": young, "c": None}) == {"b": NOT_TESTED, "c": NOT_TESTED}
