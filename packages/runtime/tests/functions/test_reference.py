"""The built-ins that span versions give, from one table of events, what reading the
store version by version gives: on a generated history of 32 tanks over 60 versions
with removals, returns, nulls and versions that share a time."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from functions_testkit import (
    GENERATED_TANKS,
    GENERATED_VERSIONS,
    History,
    ask,
    reference_grid,
    reference_integral,
    reference_series,
    reference_spells,
    rows,
    same,
    states,
)

# The first data version and the last; and a part of the history that starts and ends
# in the middle of what the tanks were doing.
WHOLE = (2, GENERATED_VERSIONS + 1)
PART = (14, 47)
SOME = GENERATED_TANKS[3:20:2]
RANGES = [(WHOLE, None), (PART, None), (PART, SOME)]


def _call(span: tuple[int, int], uids: list[str] | None, **more: Any) -> dict[str, Any]:
    call = {"property": "Tank.level", "from": f"#{span[0]}", "to": f"#{span[1]}", **more}
    if uids is not None:
        call["uids"] = uids
    return call


def test_the_history_has_what_the_comparison_needs(generated: History):
    walked = states(generated, *WHOLE)
    assert len(walked) == GENERATED_VERSIONS >= 60
    assert len({uid for _, state in walked for uid in state}) == len(GENERATED_TANKS) >= 30
    times = [version.t for version, _ in walked]
    assert len(set(times)) < len(times), "no two versions share a time"
    sizes = [len(state) for _, state in walked]
    assert min(sizes) < max(sizes), "no tank was ever removed"
    returned = any(
        uid in walked[index][1] and uid not in walked[index - 1][1] and uid in walked[0][1]
        for index in range(1, len(walked))
        for uid in GENERATED_TANKS
    )
    assert returned, "no removed tank came back"
    assert any(value is None for _, state in walked for value in state.values()), "no null"
    # Something changes within a pair of versions that share a time, so that which of
    # them is in force matters.
    assert any(
        walked[index][0].t == walked[index - 1][0].t and walked[index][1] != walked[index - 1][1]
        for index in range(1, len(walked))
    )


@pytest.mark.parametrize(("span", "uids"), RANGES)
def test_spells_are_those_of_a_walk_through_the_versions(generated: History, span, uids):
    walked = states(generated, *span)
    below = ask(generated, "below", _call(span, uids, threshold=4.0))
    expected = reference_spells(walked, lambda value: value < 4.0, min, uids)
    assert len(expected) > 10
    assert any(row["end"] is None for row in expected)
    assert any(row["start"] == walked[0][0].t for row in expected)
    same(rows(below), expected)
    above = ask(generated, "above", _call(span, uids, threshold=6.0))
    same(rows(above), reference_spells(walked, lambda value: value > 6.0, max, uids))


@pytest.mark.parametrize(("span", "uids"), RANGES)
@pytest.mark.parametrize("period", [None, timedelta(hours=1), timedelta(minutes=20)])
def test_integrals_are_those_of_a_walk_through_the_versions(generated: History, span, uids, period):
    walked = states(generated, *span)
    more = {} if period is None else {"period": period}
    answer = ask(generated, "integrate", _call(span, uids, **more))
    expected = reference_integral(walked, period, uids)
    assert any(row["covered_s"] == 0.0 for row in expected) or period is None
    assert sum(row["value"] for row in expected) > 0
    same(rows(answer), expected)


@pytest.mark.parametrize(("span", "uids"), RANGES)
def test_a_series_is_that_of_a_walk_through_the_versions(generated: History, span, uids):
    walked = states(generated, *span)
    answer = ask(generated, "series", _call(span, uids))
    expected = reference_series(walked, uids)
    assert len(expected) > len({row["uid"] for row in expected})
    same(rows(answer), expected)


@pytest.mark.parametrize(("span", "uids"), RANGES)
@pytest.mark.parametrize("step", [timedelta(minutes=10), timedelta(minutes=15)])
def test_a_series_on_a_grid_is_the_state_at_each_time(generated: History, span, uids, step):
    # Ten minutes falls between version times and, every third step, on one; fifteen
    # falls on the times two versions share.
    walked = states(generated, *span)
    answer = ask(generated, "series", _call(span, uids, step=step))
    same(rows(answer), reference_grid(walked, step, uids))
