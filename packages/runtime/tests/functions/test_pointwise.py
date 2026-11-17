"""`latest`, `below` and `above` at one version, and `aggregate`, against values worked
out by hand from the story of the kit."""

from __future__ import annotations

import pyarrow as pa
import pytest
from functions_testkit import FUNCTION, History, ask, link, node, refusal, rows, same


def test_latest_is_the_value_of_every_object_that_exists(story: History):
    answer = ask(story, "latest", {"property": "Tank.level"})
    assert answer.unit == "m"
    assert answer.value.column_names == ["uid", "value"]
    # T3 was removed; T5 has no level, and a null stays a null.
    assert rows(answer) == [
        {"uid": node("T1"), "value": 7.0},
        {"uid": node("T2"), "value": 1.9},
        {"uid": node("T4"), "value": 1.0},
        {"uid": node("T5"), "value": None},
    ]


def test_latest_reads_the_pinned_version(story: History):
    assert rows(ask(story, "latest", {"property": "Tank.level"}, version=4)) == [
        {"uid": node("T1"), "value": 1.0},
        {"uid": node("T2"), "value": 3.0},
        {"uid": node("T3"), "value": 1.8},
        {"uid": node("T4"), "value": 0.5},
        {"uid": node("T5"), "value": None},
    ]


def test_latest_against_an_earlier_version_adds_the_value_then_and_the_change(story: History):
    answer = ask(story, "latest", {"property": "Tank.level", "against": "#3"})
    assert answer.value.column_names == ["uid", "value", "previous", "change"]
    same(
        rows(answer),
        [
            {"uid": node("T1"), "value": 7.0, "previous": 1.0, "change": 6.0},
            {"uid": node("T2"), "value": 1.9, "previous": 3.0, "change": -1.1},
            # T4 was not there at #3.
            {"uid": node("T4"), "value": 1.0, "previous": None, "change": None},
            {"uid": node("T5"), "value": None, "previous": None, "change": None},
        ],
    )
    # The version may be named by its time: the last one at or before it.
    by_time = ask(story, "latest", {"property": "Tank.level", "against": "2026-01-05T17:59:00Z"})
    assert rows(by_time) == rows(answer)


def test_latest_of_what_is_not_a_number_has_no_change(story: History):
    answer = ask(story, "latest", {"property": "Pipe.status", "against": "#2"})
    assert answer.unit is None
    assert answer.value.column_names == ["uid", "value", "previous"]
    assert rows(answer)[1] == {"uid": link("P2"), "value": "closed", "previous": "closed"}


def test_an_abstract_class_stands_for_its_concrete_types(story: History):
    answer = ask(story, "latest", {"property": "Node.elevation"})
    assert answer.unit == "m"
    # Junctions, the reservoir and the tanks, in one uid order.
    assert rows(answer) == [
        {"uid": node("J1"), "value": 50.0},
        {"uid": node("J2"), "value": 45.0},
        {"uid": node("J3"), "value": 40.0},
        {"uid": node("R1"), "value": 100.0},
        {"uid": node("T1"), "value": 60.0},
        {"uid": node("T2"), "value": 61.0},
        {"uid": node("T4"), "value": 64.0},
        {"uid": node("T5"), "value": 65.0},
    ]
    above = ask(story, "above", {"property": "Node.elevation", "threshold": 60})
    assert [row["uid"] for row in rows(above)] == [node("R1"), node("T2"), node("T4"), node("T5")]


def test_uids_and_where_together_restrict_the_objects(story: History):
    call = {
        "property": "Tank.level",
        "uids": [node("T1"), node("T2"), node("T5")],
        "where": ["level:lt:5", "elevation:ge:61"],
    }
    # T1 is at 7.0, T4 is not among the uids, T5 has no level to compare.
    assert rows(ask(story, "latest", call)) == [{"uid": node("T2"), "value": 1.9}]
    assert rows(ask(story, "latest", {**call, "where": ["level:null"]})) == [
        {"uid": node("T5"), "value": None}
    ]
    assert rows(ask(story, "latest", {**call, "uids": [node("T9")]})) == []
    # Fixed by the manifest or given with the call, it is the same restriction.
    fixed = ask(story, "latest", {"where": call["where"]}, fixed={"property": "Tank.level"})
    assert rows(fixed) == [{"uid": node("T2"), "value": 1.9}, {"uid": node("T4"), "value": 1.0}]


def test_below_and_above_at_a_version_are_strict_and_leave_out_objects_without_a_value(
    story: History,
):
    below = ask(story, "below", {"property": "Tank.level", "threshold": 1.9})
    assert below.unit == "m"
    assert below.value.column_names == ["uid", "value"]
    # T2 is at 1.9 exactly, which is not below it; T5 has no level.
    assert rows(below) == [{"uid": node("T4"), "value": 1.0}]
    assert rows(ask(story, "below", {"property": "Tank.level", "threshold": 2})) == [
        {"uid": node("T2"), "value": 1.9},
        {"uid": node("T4"), "value": 1.0},
    ]
    assert rows(ask(story, "above", {"property": "Tank.level", "threshold": 1.9})) == [
        {"uid": node("T1"), "value": 7.0}
    ]
    assert rows(ask(story, "above", {"property": "Tank.level", "threshold": 7})) == []


def test_a_threshold_in_another_unit_is_converted_and_the_data_is_not(story: History):
    for call in (
        {"threshold": 200, "unit": "cm"},
        {"threshold": 0.002, "unit": "km"},
        {"threshold": 2, "unit": "m"},
    ):
        answer = ask(story, "below", {"property": "Tank.level", **call})
        # The values stay in the property's unit.
        assert answer.unit == "m"
        assert rows(answer) == [
            {"uid": node("T2"), "value": 1.9},
            {"uid": node("T4"), "value": 1.0},
        ]
    # 6 ft is 1.8288 m: T2, at 1.9 m, is no longer below it.
    feet = ask(story, "below", {"property": "Tank.level", "threshold": 6, "unit": "[ft_i]"})
    assert rows(feet) == [{"uid": node("T4"), "value": 1.0}]


def test_below_on_whole_numbers(story: History):
    answer = ask(story, "below", {"property": "Sensor.sample_interval", "threshold": 45.5})
    assert answer.unit == "s"
    assert answer.value.schema.field("value").type == pa.int64()
    assert rows(answer) == [{"uid": "sensor:S2", "value": 30}]


def test_aggregate_is_one_number_over_the_values_that_are_not_null(story: History):
    expected = {"sum": 160.0, "mean": 160.0 / 6, "min": 5.0, "max": 100.0}
    for op, value in expected.items():
        answer = ask(story, "aggregate", {"property": "Pipe.length", "op": op})
        assert answer.unit == "m"
        assert answer.value.column_names == ["value", "count"]
        same(rows(answer), [{"value": value, "count": 6}])
    counted = ask(story, "aggregate", {"property": "Pipe.length", "op": "count"})
    assert counted.unit is None
    assert rows(counted) == [{"value": 6, "count": 6}]
    # T5 has no level: three values of four tanks.
    same(
        rows(ask(story, "aggregate", {"property": "Tank.level", "op": "mean"})),
        [{"value": (7.0 + 1.9 + 1.0) / 3, "count": 3}],
    )
    # Anything can be counted.
    assert rows(ask(story, "aggregate", {"property": "Sensor.quantity", "op": "count"})) == [
        {"value": 3, "count": 3}
    ]


def test_aggregate_over_nothing_has_no_value(story: History):
    call = {"property": "Tank.level", "uids": [node("T5")]}
    assert rows(ask(story, "aggregate", {**call, "op": "sum"})) == [{"value": None, "count": 0}]
    assert rows(ask(story, "aggregate", {**call, "op": "count"})) == [{"value": 0, "count": 0}]


def test_aggregate_per_value_of_another_property(story: History):
    answer = ask(story, "aggregate", {"property": "Pipe.length", "op": "sum", "group_by": "status"})
    assert answer.unit == "m"
    assert answer.value.column_names == ["group", "value", "count"]
    # In group order; the pipes without a status form one group, which comes last.
    assert rows(answer) == [
        {"group": "closed", "value": 110.0, "count": 2},
        {"group": "open", "value": 20.0, "count": 3},
        {"group": None, "value": 30.0, "count": 1},
    ]
    longest = ask(
        story, "aggregate", {"property": "Pipe.length", "op": "max", "group_by": "status"}
    )
    assert [row["value"] for row in rows(longest)] == [100.0, 10.0, 30.0]


def test_aggregate_per_object_referred_to(story: History):
    call = {"property": "Sensor.sample_interval", "group_by": "measures"}
    answer = ask(story, "aggregate", {**call, "op": "sum"})
    assert answer.unit == "s"
    # S3, on T1, has no interval: its group has nothing to add up.
    assert rows(answer) == [
        {"group": node("J1"), "value": 90, "count": 2},
        {"group": node("T1"), "value": None, "count": 0},
    ]
    assert rows(ask(story, "aggregate", {**call, "op": "count"})) == [
        {"group": node("J1"), "value": 2, "count": 2},
        {"group": node("T1"), "value": 0, "count": 0},
    ]
    same(
        rows(ask(story, "aggregate", {**call, "op": "mean"})),
        [
            {"group": node("J1"), "value": 45.0, "count": 2},
            {"group": node("T1"), "value": None, "count": 0},
        ],
    )


def test_aggregate_over_an_abstract_class_and_some_of_its_objects(story: History):
    highest = ask(story, "aggregate", {"property": "Node.elevation", "op": "max"})
    assert rows(highest) == [{"value": 100.0, "count": 8}]
    closed = ask(
        story,
        "aggregate",
        {
            "property": "Pipe.length",
            "op": "sum",
            "uids": [link("P1"), link("P2"), link("P5")],
            "where": ["status:eq:closed"],
        },
    )
    assert rows(closed) == [{"value": 110.0, "count": 2}]


@pytest.mark.parametrize(
    ("entry", "call", "code", "parameter"),
    [
        ("latest", {"property": "Cistern.level"}, "store.unknown_type", None),
        ("latest", {"property": "Tank.depth"}, "store.unknown_property", None),
        # Only the tanks under Node have a level.
        ("latest", {"property": "Node.level"}, "store.unknown_property", None),
        ("latest", {"property": "Tank.uid"}, "store.unknown_property", None),
        (
            "latest",
            {"property": "Tank.level", "where": ["depth:gt:1"]},
            "store.unknown_property",
            None,
        ),
        (
            "latest",
            {"property": "Tank.level", "where": ["level:gt:deep"]},
            "function.bad_param",
            "where",
        ),
        ("latest", {"property": "Tank.level", "where": ["level"]}, "function.bad_param", "where"),
        (
            "below",
            {"property": "Sensor.quantity", "threshold": 2},
            "function.bad_param",
            "property",
        ),
        (
            "below",
            {"property": "Tank.volume_curve", "threshold": 2},
            "function.bad_param",
            "property",
        ),
        (
            "below",
            {"property": "Tank.level", "threshold": 2, "unit": "s"},
            "function.bad_unit",
            "unit",
        ),
        (
            "below",
            {"property": "Tank.level", "threshold": 2, "unit": "furlongs"},
            "function.bad_unit",
            "unit",
        ),
        # A roughness has no unit to convert a threshold to.
        (
            "above",
            {"property": "Pipe.roughness", "threshold": 2, "unit": "mm"},
            "function.bad_unit",
            "unit",
        ),
        (
            "aggregate",
            {"property": "Sensor.quantity", "op": "sum"},
            "function.bad_param",
            "property",
        ),
        (
            "aggregate",
            {"property": "Pipe.length", "op": "sum", "group_by": "colour"},
            "store.unknown_property",
            None,
        ),
        (
            "aggregate",
            {"property": "Tank.level", "op": "sum", "group_by": "volume_curve"},
            "function.bad_param",
            "group_by",
        ),
    ],
)
def test_what_the_ontology_refuses(story: History, entry, call, code, parameter):
    error = refusal(story, entry, call)
    assert error.code == code, error.message
    if parameter is not None:
        assert error.detail["function"] == FUNCTION
        assert error.detail["parameter"] == parameter
