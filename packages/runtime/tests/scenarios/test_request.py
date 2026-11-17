"""What a scenario is asked with, the name it gets, and what the package may import."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest
from dta_core.store import VersionRef
from dta_runtime import scenarios
from dta_runtime.scenarios.request import (
    ScenarioRequest,
    canonical,
    duration_of,
    duration_text,
    intervention_documents,
    interventions_of,
    scenario_id,
)
from pydantic import ValidationError

T = datetime(2026, 1, 5, 6, tzinfo=UTC)
STEP = timedelta(minutes=15)
BASE = VersionRef("0123456789abcdefghijklmnopqrstuv", 25, T, "main")
CLOSED = {
    "target": "Valve.status",
    "uid": "link:V1",
    "value": "closed",
    "start": T + STEP,
    "end": T + 4 * STEP,
}
BODY = {
    "name": "close the north valve",
    "description": "what the tanks do without it",
    "interventions": [CLOSED],
    "horizon": 8 * STEP,
    "models": ["twin_model:level", "twin_model:demand"],
}


def test_the_package_imports_no_model_library():
    """The API imports the package; what fits and predicts lives in the worker."""
    code = (
        "import sys\n"
        "import dta_runtime.scenarios\n"
        "heavy = ('pandas', 'statsforecast', 'mlforecast', 'sklearn', 'utilsforecast', "
        "'scipy', 'numba')\n"
        "print(sorted(name for name in heavy if name in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_the_package_exports_what_the_api_calls():
    for name in (
        "ScenarioRequest",
        "Predictor",
        "ScenarioError",
        "create",
        "run",
        "compare",
        "delete",
        "get",
        "list_all",
    ):
        assert name in scenarios.__all__ and hasattr(scenarios, name)
    assert issubclass(scenarios.ScenarioError, Exception)
    codes = {
        scenarios.BAD_REQUEST: "scenario.bad_request",
        scenarios.NOT_INTERVENTIONAL: "scenario.not_interventional",
        scenarios.NOT_FOUND: "scenario.not_found",
        scenarios.RUNNING: "scenario.running",
        scenarios.ALREADY_RUN: "scenario.already_run",
        scenarios.DELETED: "scenario.deleted",
        scenarios.TIMEOUT: "scenario.timeout",
        scenarios.BAD_PREDICTION: "scenario.bad_prediction",
    }
    assert all(code == text for code, text in codes.items())
    # The status the API answers each with is stated where the API reads it.
    for code in codes.values():
        assert code in scenarios.__doc__


def test_a_request_is_read_from_json_and_keeps_its_members():
    request = ScenarioRequest.model_validate_json(
        '{"name": "dry week", "horizon": "PT2H", "models": ["twin_model:level"], '
        '"interventions": [{"target": "Valve.status", "uid": "link:V1", "value": "closed", '
        '"start": "2026-01-05T06:15:00Z"}]}'
    )
    assert request.horizon == timedelta(hours=2)
    assert request.description is None
    assert request.interventions[0].start == T + STEP and request.interventions[0].end is None
    assert ScenarioRequest(name="x", horizon=STEP, models=["twin_model:a"]).interventions == []


@pytest.mark.parametrize(
    ("change", "member"),
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 121}, "name"),
        ({"horizon": timedelta(0)}, "horizon"),
        ({"horizon": -STEP}, "horizon"),
        ({"models": []}, "models"),
        ({"models": ["twin_model:level", "twin_model:level"]}, "models"),
        ({"budget_s": 3}, "budget_s"),
        ({"interventions": [{**CLOSED, "start": None}]}, "interventions"),
        ({"interventions": [{"target": "Valve.status", "uid": "link:V1"}]}, "interventions"),
    ],
)
def test_a_request_of_another_shape_is_refused(change, member):
    with pytest.raises(ValidationError) as caught:
        ScenarioRequest(**{**BODY, **change})
    assert member in {str(error["loc"][0]) for error in caught.value.errors()}
    assert ScenarioRequest(**{**BODY, "name": "n" * 120}).name == "n" * 120


def test_the_same_request_at_the_same_base_has_the_same_id():
    first = scenario_id(ScenarioRequest(**BODY), BASE, STEP)
    assert len(first) == 12 and set(first) <= set("0123456789abcdef")
    assert scenario_id(ScenarioRequest(**BODY), BASE, STEP) == first
    # The description, the order of the models, the order of the interventions and the
    # zone a time is written in say nothing of what the scenario is.
    later = {**CLOSED, "uid": "link:V2"}
    one = ScenarioRequest(**{**BODY, "interventions": [CLOSED, later]})
    other = ScenarioRequest(
        **{
            **BODY,
            "description": "another",
            "models": list(reversed(BODY["models"])),
            "interventions": [
                later,
                {
                    **CLOSED,
                    "start": CLOSED["start"].astimezone(timezone(timedelta(hours=5))),
                },
            ],
        }
    )
    assert scenario_id(one, BASE, STEP) == scenario_id(other, BASE, STEP)
    # Nothing of the version but its commit goes in.
    moved = VersionRef(BASE.id, 99, T + STEP, "main")
    assert scenario_id(ScenarioRequest(**BODY), moved, STEP) == first


@pytest.mark.parametrize(
    "change",
    [
        {"name": "close the south valve"},
        {"horizon": 9 * STEP},
        {"models": ["twin_model:level"]},
        {"models": ["twin_model:level", "twin_model:pressure"]},
        {"interventions": []},
        {"interventions": [{**CLOSED, "target": "Link.status"}]},
        {"interventions": [{**CLOSED, "uid": "link:V2"}]},
        {"interventions": [{**CLOSED, "value": "open"}]},
        {"interventions": [{**CLOSED, "start": T + 2 * STEP}]},
        {"interventions": [{**CLOSED, "end": None}]},
        {"interventions": [{**CLOSED, "end": T + 5 * STEP}]},
        {"interventions": [CLOSED, {**CLOSED, "uid": "link:V2"}]},
    ],
)
def test_the_id_differs_when_any_member_differs(change):
    first = scenario_id(ScenarioRequest(**BODY), BASE, STEP)
    assert scenario_id(ScenarioRequest(**{**BODY, **change}), BASE, STEP) != first


def test_the_id_differs_with_the_base_and_with_the_step():
    request = ScenarioRequest(**BODY)
    first = scenario_id(request, BASE, STEP)
    other = VersionRef("vutsrqponmlkjihgfedcba9876543210", BASE.seq, BASE.t, BASE.branch)
    assert scenario_id(request, other, STEP) != first
    assert scenario_id(request, BASE, STEP / 3) != first


def test_the_record_keeps_interventions_and_horizon_in_a_form_that_reads_back():
    request = ScenarioRequest(
        **{**BODY, "interventions": [{**CLOSED, "uid": "link:V9", "end": None}, CLOSED]}
    )
    text = canonical(intervention_documents(request.interventions))
    assert text == (
        '[{"end":"2026-01-05T07:00:00Z","start":"2026-01-05T06:15:00Z","target":"Valve.status",'
        '"uid":"link:V1","value":"closed"},'
        '{"end":null,"start":"2026-01-05T06:15:00Z","target":"Valve.status",'
        '"uid":"link:V9","value":"closed"}]'
    )
    assert interventions_of(text) == sorted(request.interventions, key=lambda found: found.uid)
    assert duration_text(8 * STEP) == "PT2H" and duration_of("PT2H") == 8 * STEP
    # A value JSON has no form for is said to be one.
    with pytest.raises(ValueError):
        canonical({"value": float("nan")})
    with pytest.raises(TypeError):
        canonical({"value": object()})
