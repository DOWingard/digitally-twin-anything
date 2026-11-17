"""`below` and `above` over a range, `integrate` and `series`, against values worked
out by hand from the story of the kit (its docstring has the history)."""

from __future__ import annotations

import pyarrow as pa
import pytest
from dta_runtime import inforce
from functions_testkit import FUNCTION, History, ask, at, node, refusal, rows, same

WHOLE = {"from": "#2", "to": "#9"}
HOUR = 3600.0


def test_spells_below_a_threshold(story: History):
    answer = ask(story, "below", {"property": "Tank.level", "threshold": 2, **WHOLE})
    assert answer.unit == "m"
    assert answer.value.column_names == ["uid", "start", "end", "extreme"]
    assert answer.value.schema.field("start").type == inforce.TIME
    assert answer.value.schema.field("end").type == inforce.TIME
    assert rows(answer) == [
        # Ended by the version that took the level away, then begun again.
        {"uid": node("T1"), "start": at(5, 12), "end": at(6, 6), "extreme": 1.0},
        {"uid": node("T1"), "start": at(6, 12), "end": at(7, 12), "extreme": 1.2},
        # Already held at `from`: it starts there.
        {"uid": node("T2"), "start": at(5, 6), "end": at(5, 12), "extreme": 1.5},
        # Still holds at `to`: no end. The lowest of 1.0 and 1.9.
        {"uid": node("T2"), "start": at(6, 12), "end": None, "extreme": 1.0},
        # Ended by the removal of the tank.
        {"uid": node("T3"), "start": at(5, 18), "end": at(6, 6), "extreme": 1.8},
        # The 0.5 of #4 was replaced at the same time by #5 and was never in force.
        {"uid": node("T4"), "start": at(7, 0), "end": None, "extreme": 1.0},
    ]


def test_spells_above_a_threshold(story: History):
    answer = ask(story, "above", {"property": "Tank.level", "threshold": 4, **WHOLE})
    assert rows(answer) == [
        {"uid": node("T1"), "start": at(5, 6), "end": at(5, 12), "extreme": 5.0},
        # Begun by the last version of the range.
        {"uid": node("T1"), "start": at(7, 12), "end": None, "extreme": 7.0},
        {"uid": node("T3"), "start": at(5, 6), "end": at(5, 18), "extreme": 8.0},
        # An object that appears inside the range.
        {"uid": node("T4"), "start": at(5, 18), "end": at(7, 0), "extreme": 6.0},
    ]


def test_spells_over_a_part_of_the_history(story: History):
    answer = ask(
        story, "below", {"property": "Tank.level", "threshold": 2, "from": "#4", "to": "#7"}
    )
    assert rows(answer) == [
        {"uid": node("T1"), "start": at(5, 18), "end": at(6, 6), "extreme": 1.0},
        {"uid": node("T1"), "start": at(6, 12), "end": None, "extreme": 1.2},
        {"uid": node("T2"), "start": at(6, 12), "end": None, "extreme": 1.0},
        {"uid": node("T3"), "start": at(5, 18), "end": at(6, 6), "extreme": 1.8},
    ]
    # Versions named by their times give the same range.
    by_time = ask(
        story,
        "below",
        {
            "property": "Tank.level",
            "threshold": 2,
            "from": "2026-01-05T18:00:00Z",
            "to": "2026-01-06T12:00:00Z",
        },
    )
    # The last version at 18:00 is #5, where the range then starts.
    assert rows(by_time) == rows(answer)


def test_spells_with_a_threshold_in_another_unit_and_some_objects(story: History):
    call = {"property": "Tank.level", "threshold": 200, "unit": "cm", **WHOLE}
    whole = ask(story, "below", call)
    assert whole.unit == "m"
    assert rows(whole) == rows(ask(story, "below", {**call, "threshold": 2, "unit": "m"}))
    some = ask(story, "below", {**call, "uids": [node("T2"), node("T3")]})
    assert [row["uid"] for row in rows(some)] == [node("T2"), node("T2"), node("T3")]
    # `where` is decided at the last version of the range: T3 is gone by then.
    passing = ask(
        story, "below", {**call, "uids": [node("T2"), node("T3")], "where": ["elevation:gt:60"]}
    )
    assert [row["uid"] for row in rows(passing)] == [node("T2"), node("T2")]
    assert rows(ask(story, "below", {**call, "where": ["elevation:gt:900"]})) == []


def test_a_range_of_one_version_is_that_version(story: History):
    answer = ask(
        story, "below", {"property": "Tank.level", "threshold": 2, "from": "#9", "to": "#9"}
    )
    assert rows(answer) == [
        {"uid": node("T2"), "start": at(7, 12), "end": None, "extreme": 1.9},
        {"uid": node("T4"), "start": at(7, 12), "end": None, "extreme": 1.0},
    ]


def test_no_spell_gives_an_empty_table_of_the_same_columns(story: History):
    answer = ask(story, "above", {"property": "Tank.level", "threshold": 50, **WHOLE})
    assert answer.value.num_rows == 0
    assert answer.value.column_names == ["uid", "start", "end", "extreme"]
    assert answer.value.schema.field("extreme").type == pa.float64()


def test_the_integral_of_the_value_in_force(story: History):
    answer = ask(story, "integrate", {"property": "Tank.level", **WHOLE})
    # The property's unit times seconds.
    assert answer.unit == "m.s"
    assert answer.value.column_names == ["uid", "value", "covered_s"]
    same(
        rows(answer),
        [
            # 5.0 for 6 h, 1.0 for 18 h, nothing for 6 h, 1.2 for 24 h.
            {"uid": node("T1"), "value": 76.8 * HOUR, "covered_s": 48 * HOUR},
            # 1.5 for 6 h, 3.0 for 24 h, 1.0 for 12 h, 1.9 for 12 h.
            {"uid": node("T2"), "value": 115.8 * HOUR, "covered_s": 54 * HOUR},
            # 8.0 for 12 h, 1.8 for 12 h, then removed.
            {"uid": node("T3"), "value": 117.6 * HOUR, "covered_s": 24 * HOUR},
            # From 18:00 on: 6.0 for 30 h, 1.0 for 12 h.
            {"uid": node("T4"), "value": 192.0 * HOUR, "covered_s": 42 * HOUR},
            # Never a level: nothing added, nothing covered.
            {"uid": node("T5"), "value": 0.0, "covered_s": 0.0},
        ],
    )


def test_the_integral_in_a_unit_asked_for(story: History):
    hours = ask(story, "integrate", {"property": "Tank.level", "unit": "m.h", **WHOLE})
    assert hours.unit == "m.h"
    same(
        [{"uid": row["uid"], "value": row["value"]} for row in rows(hours)],
        [
            {"uid": node("T1"), "value": 76.8},
            {"uid": node("T2"), "value": 115.8},
            {"uid": node("T3"), "value": 117.6},
            {"uid": node("T4"), "value": 192.0},
            {"uid": node("T5"), "value": 0.0},
        ],
    )
    # Litres a second, over seconds, are litres: 2 L/s and 4 L/s held for the 54 hours.
    litres = ask(story, "integrate", {"property": "Junction.demand", "unit": "m3", **WHOLE})
    assert litres.unit == "m3"
    same(
        rows(litres),
        [
            {"uid": node("J1"), "value": 2.0 * 54 * HOUR / 1000, "covered_s": 54 * HOUR},
            {"uid": node("J2"), "value": 4.0 * 54 * HOUR / 1000, "covered_s": 54 * HOUR},
            {"uid": node("J3"), "value": 0.0, "covered_s": 0.0},
        ],
    )
    assert ask(story, "integrate", {"property": "Junction.demand", **WHOLE}).unit == "L/s.s"


def test_the_integral_per_day_with_days_the_range_cuts_at_both_ends(story: History):
    answer = ask(
        story, "integrate", {"property": "Tank.level", "period": "P1D", "unit": "m.h", **WHOLE}
    )
    assert answer.value.column_names == ["uid", "period_start", "value", "covered_s"]
    assert answer.value.schema.field("period_start").type == inforce.TIME
    days = [at(5), at(6), at(7)]
    # The range runs from 06:00 on the 5th to 12:00 on the 7th.
    expected = {
        "T1": [(42.0, 18), (20.4, 18), (14.4, 12)],
        "T2": [(45.0, 18), (48.0, 24), (22.8, 12)],
        "T3": [(106.8, 18), (10.8, 6), (0.0, 0)],
        "T4": [(36.0, 6), (144.0, 24), (12.0, 12)],
        "T5": [(0.0, 0), (0.0, 0), (0.0, 0)],
    }
    same(
        rows(answer),
        [
            {
                "uid": node(key),
                "period_start": day,
                "value": value,
                "covered_s": covered * HOUR,
            }
            for key, parts in expected.items()
            for day, (value, covered) in zip(days, parts, strict=True)
        ],
    )


def test_periods_are_counted_from_the_epoch_and_one_that_starts_at_the_end_is_left_out(
    story: History,
):
    # 06:00 on the 5th to 12:00 on the 6th, in periods of 12 hours: 00:00-12:00 is cut
    # at its start, and the range ends where 12:00-24:00 of the 6th would begin.
    answer = ask(
        story,
        "integrate",
        {
            "property": "Tank.level",
            "from": "#2",
            "to": "#7",
            "period": "PT12H",
            "unit": "m.h",
            "uids": [node("T1")],
        },
    )
    same(
        rows(answer),
        [
            {"uid": node("T1"), "period_start": at(5, 0), "value": 30.0, "covered_s": 6 * HOUR},
            {"uid": node("T1"), "period_start": at(5, 12), "value": 12.0, "covered_s": 12 * HOUR},
            {"uid": node("T1"), "period_start": at(6, 0), "value": 6.0, "covered_s": 6 * HOUR},
        ],
    )


def test_the_integral_of_whole_numbers(story: History):
    answer = ask(story, "integrate", {"property": "Sensor.sample_interval", **WHOLE})
    assert answer.unit == "s.s"
    same(
        rows(answer),
        [
            {"uid": "sensor:S1", "value": 60 * 54 * HOUR, "covered_s": 54 * HOUR},
            {"uid": "sensor:S2", "value": 30 * 54 * HOUR, "covered_s": 54 * HOUR},
            {"uid": "sensor:S3", "value": 0.0, "covered_s": 0.0},
        ],
    )


def test_the_history_of_a_property_on_the_versions_that_changed_it(story: History):
    answer = ask(story, "series", {"property": "Tank.level", **WHOLE})
    assert answer.unit == "m"
    assert answer.value.column_names == ["uid", "t", "value"]
    assert rows(answer) == [
        {"uid": node("T1"), "t": at(5, 6), "value": 5.0},
        {"uid": node("T1"), "t": at(5, 12), "value": 1.0},
        # The level became null.
        {"uid": node("T1"), "t": at(6, 6), "value": None},
        {"uid": node("T1"), "t": at(6, 12), "value": 1.2},
        {"uid": node("T1"), "t": at(7, 12), "value": 7.0},
        {"uid": node("T2"), "t": at(5, 6), "value": 1.5},
        {"uid": node("T2"), "t": at(5, 12), "value": 3.0},
        {"uid": node("T2"), "t": at(6, 12), "value": 1.0},
        {"uid": node("T2"), "t": at(7, 0), "value": 1.9},
        {"uid": node("T3"), "t": at(5, 6), "value": 8.0},
        {"uid": node("T3"), "t": at(5, 18), "value": 1.8},
        # Removed: no value is in force any more.
        {"uid": node("T3"), "t": at(6, 6), "value": None},
        # One row per version, also for two versions of one time.
        {"uid": node("T4"), "t": at(5, 18), "value": 0.5},
        {"uid": node("T4"), "t": at(5, 18), "value": 6.0},
        {"uid": node("T4"), "t": at(7, 0), "value": 1.0},
        # A null at `from` is the value in force there, and it never changed.
        {"uid": node("T5"), "t": at(5, 6), "value": None},
    ]


def test_the_history_of_some_objects_over_a_part_of_the_range(story: History):
    answer = ask(
        story,
        "series",
        {"property": "Tank.level", "from": "#5", "to": "#8", "uids": [node("T3"), node("T4")]},
    )
    assert rows(answer) == [
        {"uid": node("T3"), "t": at(5, 18), "value": 1.8},
        {"uid": node("T3"), "t": at(6, 6), "value": None},
        {"uid": node("T4"), "t": at(5, 18), "value": 6.0},
        {"uid": node("T4"), "t": at(7, 0), "value": 1.0},
    ]


def test_the_history_of_text_and_of_an_abstract_class(story: History):
    status = ask(story, "series", {"property": "Link.status", **WHOLE})
    assert status.unit is None
    # Nothing changed a link: one row each, at `from`.
    assert [(row["uid"], row["value"]) for row in rows(status)] == [
        ("link:P1", "open"),
        ("link:P2", "closed"),
        ("link:P3", "open"),
        ("link:P4", "open"),
        ("link:P5", "closed"),
        ("link:P6", None),
        ("link:V1", "open"),
    ]
    assert {row["t"] for row in rows(status)} == {at(5, 6)}


def test_the_value_in_force_on_a_regular_grid(story: History):
    answer = ask(story, "series", {"property": "Tank.level", "step": "PT12H", **WHOLE})
    assert answer.unit == "m"
    assert answer.value.column_names == ["uid", "t", "value"]
    grid = [at(5, 6), at(5, 18), at(6, 6), at(6, 18), at(7, 6)]
    expected = {
        "T1": [5.0, 1.0, None, 1.2, 1.2],
        "T2": [1.5, 3.0, 3.0, 1.0, 1.9],
        # Removed at 06:00 on the 6th: no row from then on.
        "T3": [8.0, 1.8],
        # Appears at 18:00 on the 5th, where the later of two versions is in force.
        "T4": [None, 6.0, 6.0, 6.0, 1.0],
        "T5": [None, None, None, None, None],
    }
    wanted = [
        {"uid": node(key), "t": moment, "value": value}
        for key, values in expected.items()
        for moment, value in zip(grid, values, strict=False)
        if not (key == "T4" and moment == at(5, 6))
    ]
    assert rows(answer) == wanted
    # A step the range is not a multiple of stops at the last time inside it.
    daily = ask(story, "series", {"property": "Tank.level", "step": "P1D", **WHOLE})
    assert sorted({row["t"] for row in rows(daily)}) == [at(5, 6), at(6, 6), at(7, 6)]


@pytest.mark.parametrize(
    ("entry", "call", "code", "parameter"),
    [
        (
            "below",
            {"property": "Tank.level", "threshold": 2, "from": "#2"},
            "function.bad_param",
            "to",
        ),
        (
            "above",
            {"property": "Tank.level", "threshold": 2, "to": "#9"},
            "function.bad_param",
            "from",
        ),
        (
            "below",
            {"property": "Tank.level", "threshold": 2, "from": "#9", "to": "#2"},
            "function.bad_param",
            "from",
        ),
        (
            "series",
            {"property": "Tank.level", "from": "#7", "to": "#3"},
            "function.bad_param",
            "from",
        ),
        (
            "integrate",
            {"property": "Tank.level", "from": "#2", "to": "#40"},
            "store.unknown_version",
            None,
        ),
        ("integrate", {"property": "Sensor.quantity", **WHOLE}, "function.bad_param", "property"),
        (
            "integrate",
            {"property": "Tank.level", "unit": "m", **WHOLE},
            "function.bad_unit",
            "unit",
        ),
        (
            "integrate",
            {"property": "Tank.level", "unit": "L", **WHOLE},
            "function.bad_unit",
            "unit",
        ),
        (
            "integrate",
            {"property": "Pipe.roughness", "unit": "m.s", **WHOLE},
            "function.bad_unit",
            "unit",
        ),
        (
            "integrate",
            {"property": "Tank.level", "period": "PT0.001S", **WHOLE},
            "function.bad_param",
            "period",
        ),
        (
            "series",
            {"property": "Tank.level", "step": "PT0.001S", **WHOLE},
            "function.bad_param",
            "step",
        ),
        ("series", {"property": "Tank.depth", **WHOLE}, "store.unknown_property", None),
    ],
)
def test_what_a_range_refuses(story: History, entry, call, code, parameter):
    error = refusal(story, entry, call)
    assert error.code == code, error.message
    if parameter is not None:
        assert error.detail["function"] == FUNCTION
        assert error.detail["parameter"] == parameter


def test_a_range_is_seen_from_the_pinned_version(story: History):
    call = {"property": "Tank.level", "from": "#2"}
    # `head` is the version the call is pinned at, whatever was committed since.
    pinned = ask(story, "series", {**call, "to": "head"}, version=5)
    assert rows(pinned) == rows(ask(story, "series", {**call, "to": "#5"}))
    # So is a time after it.
    later = ask(story, "series", {**call, "to": "2026-01-09T00:00:00Z"}, version=5)
    assert rows(later) == rows(pinned)
    # A version named outright that lies after it is refused.
    error = refusal(story, "series", {**call, "to": "#8"}, version=5)
    assert error.code == "function.bad_param"
    assert error.detail["parameter"] == "to"
    assert (
        refusal(story, "latest", {"property": "Tank.level", "against": "#8"}, version=5).detail[
            "parameter"
        ]
        == "against"
    )


def test_an_integral_without_a_unit_has_none(story: History):
    answer = ask(story, "integrate", {"property": "Pipe.roughness", **WHOLE})
    assert answer.unit is None
    assert answer.value.num_rows == 6


def test_the_range_may_be_as_long_as_a_step(story: History):
    one = ask(story, "series", {"property": "Tank.level", "from": "#3", "to": "#3", "step": "PT1H"})
    assert [row["t"] for row in rows(one)] == [at(5, 12)] * 4
    whole = ask(story, "integrate", {"property": "Tank.level", "from": "#3", "to": "#3"})
    assert {(row["value"], row["covered_s"]) for row in rows(whole)} == {(0.0, 0.0)}
    # With a period, the one the instant lies in.
    day = ask(
        story, "integrate", {"property": "Tank.level", "from": "#3", "to": "#3", "period": "P1D"}
    )
    assert {row["period_start"] for row in rows(day)} == {at(5)}
    assert day.value.num_rows == 4
