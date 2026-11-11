"""The table of built-in functions and the shapes their parameters take."""

from datetime import timedelta

import pytest
from dta_core.functions import BUILTINS, REGISTERED_ENTRY, ParamKind, shape_problem


def test_the_built_ins_and_what_each_requires():
    assert sorted(BUILTINS) == [
        "above",
        "aggregate",
        "below",
        "forecast",
        "integrate",
        "latest",
        "paths",
        "series",
    ]
    assert {entry: found.required for entry, found in BUILTINS.items()} == {
        "latest": ("property",),
        "below": ("property", "threshold"),
        "above": ("property", "threshold"),
        "integrate": ("property", "from", "to"),
        "paths": ("source", "target"),
        "aggregate": ("property", "op"),
        "series": ("property", "from", "to"),
        "forecast": ("question",),
    }
    for found in BUILTINS.values():
        names = [parameter.name for parameter in found.parameters]
        assert len(names) == len(set(names)), found.entry
        # The version and the branch a call reads at are not parameters.
        assert not {"at", "branch"} & set(names)
        for parameter in found.parameters:
            assert parameter.description
            assert bool(parameter.choices) == (parameter.kind == ParamKind.choice)
            if parameter.default is not None:
                assert not parameter.required
                assert shape_problem(parameter, parameter.default) is None


def _parameter(entry: str, name: str):
    parameter = BUILTINS[entry].parameter(name)
    assert parameter is not None
    return parameter


@pytest.mark.parametrize(
    ("entry", "name", "good", "bad"),
    [
        ("latest", "property", ["Tank.level"], ["level", "Tank.", 3, None, "Tank.level.x"]),
        ("below", "threshold", [20, 0.5, -1], ["20", True, float("nan"), float("inf"), None]),
        ("below", "unit", ["m", "kW.h"], ["", " ", 3]),
        (
            "integrate",
            "period",
            ["P1D", "PT15M", "P1DT6H", "PT0.5S", timedelta(hours=1)],
            ["1 day", "15", "P", "PT", "PT0S", 86400],
        ),
        ("paths", "edge_types", [["Pipe"], ["Pipe", "Pump"]], ["Pipe", [], [3], [""]]),
        ("paths", "edge_where", [["Pipe.status:eq:open"]], ["Pipe.status:eq:open", []]),
        ("paths", "weight", ["length"], ["Pipe.length", "", 3]),
        ("paths", "direction", ["both", "out", "in"], ["forward", None]),
        ("paths", "k", [1, 5], [0, -1, 1.5, True, "3"]),
        ("aggregate", "op", ["sum", "mean", "min", "max", "count"], ["median", 1]),
        ("forecast", "levels", [[80, 95], (50,)], [[], 80, ["80"]]),
        ("latest", "uids", [["asset:TNK-1"]], ["asset:TNK-1", []]),
        ("latest", "against", ["#12", "2026-01-05T00:00:00Z"], ["", 12]),
    ],
)
def test_a_value_has_the_shape_of_its_parameter_or_is_told_why_not(entry, name, good, bad):
    parameter = _parameter(entry, name)
    for value in good:
        assert shape_problem(parameter, value) is None, value
    for value in bad:
        problem = shape_problem(parameter, value)
        assert problem and problem.startswith("must "), value


def test_code_is_named_as_a_module_and_a_function():
    for entry in ("checks:low_pressure", "plant.hydraulics:simulate", "_m:_f"):
        assert REGISTERED_ENTRY.match(entry), entry
    for entry in ("low_pressure", "plant/hydraulics:simulate", "plant:", ":simulate", "a:b:c"):
        assert not REGISTERED_ENTRY.match(entry), entry
