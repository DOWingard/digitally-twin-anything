"""The parameters of a call: the manifest's fixed ones and the call's, read into the
values a built-in takes, and every way they are refused."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dta_core.functions import BUILTINS, Parameter, ParamKind
from dta_core.manifest.models import FunctionSpec
from dta_core.store import Selector
from dta_runtime.functions import FunctionError, resolve
from dta_runtime.functions.params import from_text


def _spec(entry: str, **fixed: Any) -> FunctionSpec:
    return FunctionSpec(kind="builtin", entry=entry, params=fixed)


def _refused(spec: FunctionSpec, call: dict[str, Any]) -> FunctionError:
    with pytest.raises(FunctionError) as raised:
        resolve("low_tanks", spec, call)
    return raised.value


def test_fixed_and_given_parameters_become_the_values_a_built_in_takes():
    spec = _spec("below", property="Tank.level", unit="cm", uids=["node:T1", "node:T2"])
    found = resolve(
        "low_tanks",
        spec,
        {"threshold": 200, "from": "#2", "to": "head", "where": ["level:lt:5", "name:like:T%"]},
    )
    assert found == {
        "property": "Tank.level",
        "threshold": 200.0,
        "unit": "cm",
        "from": Selector("seq", seq=2),
        "to": Selector("head"),
        "uids": ["node:T1", "node:T2"],
        "where": ["level:lt:5", "name:like:T%"],
    }
    assert isinstance(found["threshold"], float)


def test_a_parameter_left_out_is_left_out():
    # What a parameter is when nobody gives it is for the run to say.
    assert resolve("routes", _spec("paths"), {"source": "node:J1", "target": "node:J3"}) == {
        "source": "node:J1",
        "target": "node:J3",
    }


def test_a_duration_is_read_whether_fixed_or_given():
    fixed = resolve(
        "energy",
        _spec("integrate", property="Pump.speed", period="P1D"),
        {"from": "#1", "to": "#2"},
    )
    assert fixed["period"] == timedelta(days=1)
    given = resolve(
        "energy",
        _spec("integrate", property="Pump.speed"),
        {"from": "#1", "to": "#2", "period": "PT15M"},
    )
    assert given["period"] == timedelta(minutes=15)
    kept = resolve(
        "history",
        _spec("series", property="Tank.level"),
        {"from": "#1", "to": "#2", "step": timedelta(hours=1)},
    )
    assert kept["step"] == timedelta(hours=1)


def test_values_of_a_query_string_are_read_as_their_kinds():
    found = resolve(
        "low_tanks",
        _spec("below", property="Tank.level"),
        {
            "threshold": " 2.5e1 ",
            "uids": "node:T1, node:T2,node:T1",
            "where": "status:in:open,closed",
            # A `+` that was not encoded arrives as a space.
            "from": "2026-01-05T06:00:00 01:00",
            "to": "2026-01-07T12:00:00Z",
        },
    )
    assert found["threshold"] == 25.0
    assert found["uids"] == ["node:T1", "node:T2"]
    # A clause may hold commas of its own, so one text is one clause.
    assert found["where"] == ["status:in:open,closed"]
    assert found["from"] == Selector("time", time=datetime(2026, 1, 5, 5, tzinfo=UTC))
    assert found["to"] == Selector("time", time=datetime(2026, 1, 7, 12, tzinfo=UTC))

    routes = resolve(
        "routes",
        _spec("paths", source="node:J1"),
        {
            "target": "node:J3",
            "k": "3",
            "max_depth": "+4",
            "edge_types": "Pipe,Valve",
            "edge_where": "Pipe.status:in:open,closed",
            "default_weight": "1",
            "weight": "length",
            "direction": "out",
        },
    )
    assert routes == {
        "source": "node:J1",
        "target": "node:J3",
        "edge_types": ["Pipe", "Valve"],
        "edge_where": ["Pipe.status:in:open,closed"],
        "weight": "length",
        "default_weight": 1.0,
        "direction": "out",
        "k": 3,
        "max_depth": 4,
    }
    assert type(routes["k"]) is int

    for levels in ("50, 80,95", ["50", "80", "95"], ["50", 80, 95.0], (50, 80, 95)):
        forecast = resolve("ahead", _spec("forecast", question="level_ahead"), {"levels": levels})
        assert forecast["levels"] == (50.0, 80.0, 95.0)


def test_true_and_false_are_read_from_text():
    flag = Parameter("exact", ParamKind.boolean, "Whether")
    assert [from_text(flag, text) for text in ("true", "TRUE", "1", "false", "0")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    # Anything else stays as it is, for the shape rule to refuse.
    assert from_text(flag, "yes") == "yes"
    assert from_text(flag, True) is True


def test_a_fixed_value_is_not_read_from_text():
    # The manifest holds typed values; text where a number belongs is a mistake there.
    error = _refused(_spec("below", property="Tank.level", threshold="20"), {})
    assert error.code == "function.bad_param"
    assert error.detail["parameter"] == "threshold"


def test_a_parameter_the_entry_does_not_have_is_refused():
    for spec, call in (
        (_spec("latest", property="Tank.level"), {"threshold": 2}),
        (_spec("latest", property="Tank.level", threshold=2), {}),
        (_spec("latest", property="Tank.level"), {"at": "head"}),
    ):
        error = _refused(spec, call)
        assert error.code == "function.param_unknown"
        assert error.detail["function"] == "low_tanks"
        assert error.detail["parameter"] in ("threshold", "at")
        assert "latest" in error.message


def test_a_fixed_parameter_given_again_is_refused_even_with_the_same_value():
    spec = _spec("below", property="Tank.level", threshold=2)
    for call in ({"threshold": 3}, {"threshold": 2}, {"property": "Tank.level"}):
        error = _refused(spec, call)
        assert error.code == "function.param_fixed"
        assert error.detail == {"function": "low_tanks", "parameter": next(iter(call))}


def test_a_required_parameter_given_nowhere_is_refused():
    error = _refused(_spec("below", property="Tank.level"), {"unit": "m"})
    assert error.code == "function.param_missing"
    assert error.detail == {"function": "low_tanks", "parameter": "threshold"}
    assert _refused(_spec("integrate"), {"from": "#1", "to": "#2"}).detail["parameter"] == (
        "property"
    )
    # Each entry asks for what the table says it requires.
    for entry, builtin in BUILTINS.items():
        error = _refused(_spec(entry), {})
        assert error.code == "function.param_missing"
        assert error.detail["parameter"] == builtin.required[0]


@pytest.mark.parametrize(
    ("entry", "fixed", "call", "parameter"),
    [
        ("below", {"property": "Tank.level"}, {"threshold": "low"}, "threshold"),
        ("below", {"property": "Tank.level"}, {"threshold": True}, "threshold"),
        ("below", {"property": "Tank.level"}, {"threshold": "nan"}, "threshold"),
        ("below", {"property": "Tank.level"}, {"threshold": "1_000"}, "threshold"),
        ("below", {"property": "level"}, {"threshold": 2}, "property"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"unit": " "}, "unit"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"uids": []}, "uids"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"uids": ","}, "uids"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"where": [3]}, "where"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"from": ""}, "from"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"from": "yesterday"}, "from"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"from": "#1..#3"}, "from"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"to": "2026-01-05T06:00:00"}, "to"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"to": 9}, "to"),
        ("below", {"property": "Tank.level", "threshold": 2}, {"unit": None}, "unit"),
        ("integrate", {"property": "Tank.level", "from": "#1", "to": "#2"}, {"period": "15"}, None),
        (
            "integrate",
            {"property": "Tank.level", "from": "#1", "to": "#2"},
            {"period": "PT0S"},
            None,
        ),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"k": "0"}, "k"),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"k": "1.5"}, "k"),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"k": 2.0}, "k"),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"direction": "sideways"}, None),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"weight": "Pipe.length"}, "weight"),
        ("paths", {"source": "node:J1", "target": "node:J3"}, {"edge_types": [""]}, "edge_types"),
        ("aggregate", {"property": "Pipe.length"}, {"op": "median"}, "op"),
        ("forecast", {"question": "level_ahead"}, {"levels": "80,high"}, "levels"),
        ("forecast", {"question": "level_ahead"}, {"levels": ""}, "levels"),
        ("forecast", {"question": "level_ahead"}, {"horizon": "tomorrow"}, "horizon"),
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(entry, fixed, call, parameter):
    error = _refused(_spec(entry, **fixed), call)
    assert error.code == "function.bad_param", error.message
    assert error.detail["function"] == "low_tanks"
    assert error.detail["parameter"] == (parameter or next(iter(call)))
    assert error.detail["parameter"] in error.message


def test_an_entry_that_is_no_built_in_is_refused():
    error = _refused(_spec("median"), {})
    assert error.code == "function.unknown_entry"
    assert error.detail["entry"] == "median"


def test_code_of_the_twin_takes_its_fixed_arguments_and_the_calls():
    spec = FunctionSpec(kind="registered", entry="plant:headroom", params={"margin": 0.5})
    assert resolve("headroom", spec, {"uids": ["node:T1"]}) == {
        "margin": 0.5,
        "uids": ["node:T1"],
    }
    with pytest.raises(FunctionError) as raised:
        resolve("headroom", spec, {"margin": 0.5})
    assert raised.value.code == "function.param_fixed"
    assert raised.value.detail == {"function": "headroom", "parameter": "margin"}
