"""Whether a prediction of a model reads a property: a shipped family its target, its
features and what it declares in `reads`; code of the twin's own its features and what
it declares; code that declares nothing every property."""

from __future__ import annotations

from typing import Any

import pytest
from dta_runtime.fidelity import model_inputs, model_properties, reads_property

FEATURE = '{"property": "Gauge.flow", "lag": 1}'


def model(family: str, reads: list[str] | None) -> dict[str, Any]:
    return {"family": family, "target": "Gauge.level", "features": [FEATURE], "reads": reads}


SHIPPED = model("persistence", ["Pump.speed"])
DECLARED = model("plant.hydraulic:Model", ["Pump.speed"])
UNDECLARED = model("plant.hydraulic:Model", None)


@pytest.mark.parametrize(
    ("found", "property_text", "expected"),
    [
        (SHIPPED, "Gauge.level", True),  # a shipped family reads its target
        (SHIPPED, "Gauge.flow", True),  # its features
        (SHIPPED, "Pump.speed", True),  # and what it declares
        (SHIPPED, "Gauge.pressure", False),
        (DECLARED, "Gauge.flow", True),  # code: its features
        (DECLARED, "Pump.speed", True),  # and what it declares
        (DECLARED, "Gauge.level", False),  # not its target unless declared
        (DECLARED, "Gauge.pressure", False),
        (UNDECLARED, "Gauge.level", True),  # code declaring nothing reads everything
        (UNDECLARED, "Gauge.pressure", True),
        (model("persistence", None), "Gauge.pressure", False),  # a family needs no reads
        (model("plant.hydraulic:Model", []), "Gauge.level", False),  # declared: nothing more
    ],
)
def test_what_a_prediction_reads(found, property_text, expected):
    assert reads_property(found, property_text) is expected


def test_an_abstract_type_stands_for_its_concrete_types():
    found = model("plant.hydraulic:Model", ["Asset.speed"])
    concrete = {"Asset.speed": ("Pump.speed", "Fan.speed")}

    def expand(text: str) -> tuple[str, ...]:
        return concrete.get(text, (text,))

    assert reads_property(found, "Fan.speed", expand)
    assert not reads_property(found, "Fan.speed")


def test_inputs_and_what_a_prediction_computes_from():
    assert model_inputs(DECLARED) == ["Gauge.flow", "Pump.speed"]
    assert model_inputs(UNDECLARED) is None
    assert model_properties(SHIPPED) == ["Gauge.level", "Gauge.flow", "Pump.speed"]
    assert model_properties(UNDECLARED) is None
