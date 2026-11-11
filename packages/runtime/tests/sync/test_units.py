"""Units: a mapping's unit is converted to the ontology's, or the run is refused."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import pytest
from dta_runtime.sync import MappingInvalid, UnitMismatch
from dta_runtime.sync.mapper import Session, _double
from dta_runtime.sync.units import Affine, UnitProblem, conversion
from dta_runtime.twin import Twin
from sync_testkit import Sources, columns, objects, source, table
from water_scenario import JUNCTIONS

Run = Callable[..., Any]
MakeTwin = Callable[..., Twin]


def test_a_conversion_is_a_factor_and_for_some_units_an_offset():
    assert conversion("mm", "m") == Affine(0.001, 0.0)
    assert conversion("m", "m").is_identity
    assert conversion("m3/h", "L/s").factor == pytest.approx(1000.0 / 3600.0, rel=1e-15)
    psi = conversion("[psi]", "kPa")
    assert (psi.factor, psi.offset) == (pytest.approx(6.894757293168, rel=1e-12), 0.0)
    celsius = conversion("Cel", "K")
    assert (celsius.factor, celsius.offset) == (1.0, 273.15)
    fahrenheit = conversion("[degF]", "Cel")
    assert fahrenheit.factor == pytest.approx(5.0 / 9.0, rel=1e-14)
    assert fahrenheit.apply(212.0) == pytest.approx(100.0, abs=1e-9)
    assert fahrenheit.apply(32.0) == pytest.approx(0.0, abs=1e-9)
    # Pressure as a height of water is a pressure, not a length.
    assert conversion("m[H2O]", "kPa").factor == pytest.approx(9.80665)


@pytest.mark.parametrize(
    ("given", "wanted"),
    [("L/s", "m"), ("m", "[psi]"), ("furlongs", "m"), ("", "m"), ("m", "nonsense unit")],
)
def test_units_that_do_not_convert_are_refused(given: str, wanted: str):
    with pytest.raises(UnitProblem):
        conversion(given, wanted)


def test_a_factor_reaches_the_query_digit_for_digit():
    generator = random.Random(11)
    values = [generator.uniform(-1, 1) * 10 ** generator.randint(-12, 12) for _ in range(500)]
    values += [1000.0 / 3600.0, 5.0 / 9.0, 6.894757293168363, 273.15, 0.001]
    session = Session()
    try:
        for value in values:
            assert session.scalar(f"SELECT {_double(value)} = ?", [value]) is True
    finally:
        session.close()


# The water network with a temperature on its tanks, so that a unit with an offset applies.
def with_temperature(ontology_text: str) -> str:
    marker = "      volume_curve:\n"
    assert marker in ontology_text
    addition = (
        "      temperature:\n"
        "        range: float\n"
        "        unit:\n"
        "          ucum_code: Cel\n"
        "      inspections:\n"
        "        range: integer\n"
        "        unit:\n"
        "          ucum_code: /a\n"
    )
    return ontology_text.replace(marker, addition + marker)


def tank_source(properties: dict[str, Any]) -> dict[str, Any]:
    return source(
        {
            "tanks": {
                "dataset": "tanks",
                "object_type": "Tank",
                "identity": ["id"],
                "properties": {**columns("name"), **properties},
            },
            "junctions": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": {
                    **columns("name", "elevation"),
                    "base_demand": {"column": "base_demand", "unit": "m3/h"},
                },
            },
        }
    )


TANK = {
    "id": "T1",
    "name": "Hill tank",
    "elev_ft": 100.0,
    "min_cm": 50,
    "max_mm": 6000.0,
    "temp_f": 50.0,
    "levels_cm": [0.0, 150.0],
    "per_month": 2,
}


def test_values_are_converted_to_the_unit_of_the_ontology(
    sync_twin: MakeTwin, run: Run, water_ontology_text: str
):
    registry = tank_source(
        {
            "elevation": {"column": "elev_ft", "unit": "[ft_i]"},
            "min_level": {"column": "min_cm", "unit": "cm"},
            "max_level": {"column": "max_mm", "unit": "mm"},
            # An offset as well as a factor.
            "temperature": {"column": "temp_f", "unit": "[degF]"},
            # Twice a month is 24 times a year: an integer stays one.
            "inspections": {"column": "per_month", "unit": "/mo_j"},
            # The same unit as the ontology's: nothing to convert.
            "x": {"expression": "12.5", "unit": "m"},
        }
    )
    twin = sync_twin({"registry": registry}, ontology_text=with_temperature(water_ontology_text))
    report = run(twin, Sources(registry={"tanks": table([TANK]), "junctions": table(JUNCTIONS)}))

    assert report.rows_rejected == 0
    tank = objects(twin.store, "Tank")["node:T1"]
    assert tank["elevation"] == pytest.approx(30.48, rel=1e-12)
    assert tank["min_level"] == pytest.approx(0.5, rel=1e-12)
    assert tank["max_level"] == pytest.approx(6.0, rel=1e-12)
    assert tank["temperature"] == pytest.approx(10.0, abs=1e-9)
    assert tank["inspections"] == 24
    assert tank["x"] == 12.5
    junctions = objects(twin.store, "Junction")
    assert junctions["node:J1"]["base_demand"] == pytest.approx(1.5 / 3.6, rel=1e-12)
    assert junctions["node:J3"]["base_demand"] is None

    # Converted values are the same on every run, so nothing is written again.
    head = twin.store.head()
    sources = Sources(registry={"tanks": table([TANK]), "junctions": table(JUNCTIONS)})
    assert run(twin, sources).versions_written == 0
    assert twin.store.head() == head


def test_an_integer_that_is_no_longer_whole_after_conversion_is_set_aside(
    sync_twin: MakeTwin, run: Run, water_ontology_text: str
):
    registry = source(
        {
            "sensors": {
                "dataset": "sensors",
                "object_type": "Sensor",
                "identity": ["id"],
                "properties": {
                    **columns("quantity", "active", measures="node"),
                    "sample_interval": {"column": "interval_ms", "unit": "ms"},
                },
            },
            "junctions": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation"),
            },
        }
    )
    sensors = [
        {"id": "a", "node": "J1", "quantity": "flow", "active": True, "interval_ms": 60000},
        {"id": "b", "node": "J1", "quantity": "flow", "active": True, "interval_ms": 1500},
    ]
    twin = sync_twin({"registry": registry})
    report = run(twin, Sources(registry={"sensors": table(sensors), "junctions": table(JUNCTIONS)}))

    stored = objects(twin.store, "Sensor")
    assert set(stored) == {"sensor:a"}
    assert stored["sensor:a"]["sample_interval"] == 60
    assert report.mappings["registry.sensors"].rows_rejected == 1


def test_units_that_cannot_be_converted_fail_the_run_before_anything_is_read(
    sync_twin: MakeTwin, run: Run, water_ontology_text: str
):
    registry = tank_source(
        {
            "elevation": {"column": "elev_ft", "unit": "[ft_i]"},
            # A flow is not a length.
            "min_level": {"column": "min_cm", "unit": "L/s"},
            # Not a unit at all.
            "max_level": {"column": "max_mm", "unit": "furlongs"},
        }
    )
    twin = sync_twin({"registry": registry})
    sources = Sources(registry={"tanks": table([TANK]), "junctions": table(JUNCTIONS)})
    head = twin.store.head()

    with pytest.raises(UnitMismatch) as caught:
        run(twin, sources)

    assert caught.value.code == "sync.unit_mismatch"
    problems = {p["where"]: p["message"] for p in caught.value.detail["problems"]}
    assert problems == {
        "sources.registry.mappings.tanks.properties.min_level.unit": (
            "'L/s' cannot be converted to 'm'"
        ),
        "sources.registry.mappings.tanks.properties.max_level.unit": (
            "'furlongs' is not a UCUM unit code"
        ),
    }
    assert sources.opened == []
    assert twin.store.head() == head


def test_a_unit_problem_among_other_problems_is_reported_with_them(sync_twin: MakeTwin, run: Run):
    registry = tank_source({"min_level": {"column": "min_cm", "unit": "L/s"}})
    registry["mappings"]["tanks"]["properties"]["name"] = {"column": "name", "unit": "m"}
    twin = sync_twin({"registry": registry})

    with pytest.raises(MappingInvalid) as caught:
        run(twin, Sources(registry={}))

    assert caught.value.code == "sync.mapping_invalid"
    codes = {p["where"].rsplit(".", 2)[-2]: p["code"] for p in caught.value.detail["problems"]}
    assert codes == {"min_level": "sync.unit_mismatch", "name": "sync.mapping_invalid"}
