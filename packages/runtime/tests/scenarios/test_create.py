"""Making a scenario: its branch at the base version, what is assumed from the base time
on, its record on the live line, and every reason a request is refused for."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from dta_core.store import StoreError, VersionRef
from dta_core.store.model import iso_utc
from dta_runtime import scenarios
from dta_runtime.scenarios import ScenarioError, ScenarioRequest, records
from dta_runtime.scenarios.request import scenario_id
from scenarios_testkit import (
    STEP,
    World,
    branch_names,
    branch_versions,
    cells,
    closed,
    create,
    grid,
    link,
    model,
    node,
    origins,
    setting,
)


def refusal(world: World, request: ScenarioRequest, **more: Any) -> ScenarioError:
    with pytest.raises(ScenarioError) as caught:
        scenarios.create(world.twin, request, **{"at": world.base, "step": STEP, **more})
    return caught.value


# -- what is made ------------------------------------------------------------------------


def test_a_scenario_is_a_branch_at_its_base_and_a_record_on_the_live_line(world: World):
    base = world.base
    before = world.store.head()
    request = world.request(models=("level-naive", "demand-last"), description="a dry week")
    made = create(world, request)

    uid, branch = made["uid"], made["branch"]
    assert made["created"] is True
    assert made["id"] == scenario_id(request, base, STEP)
    assert (uid, branch) == (f"twin_scenario:{made['id']}", f"scenario-{made['id']}")

    # The branch starts at the base version and holds nothing more.
    head = world.store.head(branch)
    assert (head.id, head.seq, head.t, head.branch) == (base.id, base.seq, base.t, branch)

    # The record is an object of the live line.
    stored = world.store.get(uid)
    assert stored.type == "TwinScenario"
    assert stored.values == {
        "name": request.name,
        "description": "a dry week",
        "branch": branch,
        "base_version": base.id,
        "base_t": base.t,
        "interventions": "[]",
        "horizon": "PT2H",
        "models": [model("level-naive"), model("demand-last")],
        "status": "created",
        "error": None,
    }
    assert made == {"uid": uid, **stored.values, "id": made["id"], "created": True}
    assert world.store.get(uid, branch=branch) is None

    # One version, written as the twin's other registrations are: at the head's time.
    written = world.store.versions(f"#{before.seq + 1}..head")
    assert [(v.cause, v.actor, v.t) for v in written] == [("registration", "scenarios", before.t)]
    assert written[0].extra == {"scenario": uid, "status": "created", "step_ms": 900_000}


def test_what_is_assumed_from_the_base_time_is_one_version_of_the_branch(world: World):
    base = world.base
    made = create(
        world,
        world.request(
            interventions=[setting(7.5, grid(0)), setting(3.0, grid(2), grid(4), key="V2")]
        ),
    )
    uid, branch = made["uid"], made["branch"]
    added = branch_versions(world, branch)
    assert [(v.seq, v.cause, v.actor, v.t) for v in added] == [
        (base.seq + 1, "scenario", "scenarios", base.t)
    ]
    assert added[0].extra == {"scenario": uid, "step": 0}

    held = cells(world, "Valve", "setting", "head", branch)
    assert held[link("V1")][0] == 7.5
    assert {key: held[link("V1")][1][key] for key in "skt"} == {
        "s": uid,
        "k": "assumed",
        "t": iso_utc(base.t),
    }
    # What starts later is not there yet, and nothing else of the valve was touched.
    assert held[link("V2")][0] == 2.0 and held[link("V2")][1]["k"] == "observed"
    status = cells(world, "Valve", "status", "head", branch)[link("V1")]
    assert (status[0], status[1]["k"], status[1]["s"]) == ("open", "observed", "scada")

    # The live line holds the valve as it was observed, and nothing assumed.
    live = cells(world, "Valve", "setting")[link("V1")]
    assert (live[0], live[1]["k"]) == (1.0, "observed")
    assert {kind for kind, _ in origins(world)} == {"observed"}
    assert ("assumed", uid) in origins(world, "head", branch)


def test_asking_for_the_same_scenario_again_gives_the_one_there_is(world: World, predictor):
    request = world.request(interventions=[setting(7.5, grid(0))])
    first = create(world, request)
    heads = (world.store.head().id, world.store.head(first["branch"]).id)

    asked = ScenarioRequest(**{**request.model_dump(), "description": "asked again"})
    again = create(world, asked)
    assert again == {**first, "created": False}
    assert (world.store.head().id, world.store.head(first["branch"]).id) == heads

    # Whatever has become of it since.
    scenarios.run(world.twin, first["uid"], predict=predictor, budget_s=60)
    ran = (world.store.head().id, world.store.head(first["branch"]).id)
    again = create(world, asked)
    assert (again["status"], again["created"], again["id"]) == ("done", False, first["id"])
    assert (world.store.head().id, world.store.head(first["branch"]).id) == ran


def test_a_deleted_scenario_is_made_again(world: World):
    request = world.request(interventions=[setting(7.5, grid(0))])
    first = create(world, request)
    scenarios.delete(world.twin, first["uid"])
    assert first["branch"] not in branch_names(world)

    again = create(world, request)
    assert (again["id"], again["created"], again["status"]) == (first["id"], True, "created")
    assert world.store.get(first["uid"]).values["status"] == "created"
    # From the base, with what is assumed there.
    added = branch_versions(world, first["branch"])
    assert [(v.cause, v.t) for v in added] == [("scenario", world.base.t)]
    assert cells(world, "Valve", "setting", "head", first["branch"])[link("V1")][0] == 7.5


def test_an_abstract_class_names_the_property_of_its_concrete_types(world: World):
    made = create(
        world,
        world.request(
            interventions=[
                {"target": "Node.elevation", "uid": node("T2"), "value": 61.5, "start": grid(0)}
            ]
        ),
    )
    held = cells(world, "Tank", "elevation", "head", made["branch"])
    assert (held[node("T2")][0], held[node("T2")][1]["k"]) == (61.5, "assumed")
    assert held[node("T1")][1]["k"] == "observed"


def test_a_branch_left_behind_without_a_record_is_replaced(world: World):
    request = world.request()
    name = f"scenario-{scenario_id(request, world.base, STEP)}"
    # What an attempt that died between the branch and the record leaves.
    world.store.branch_create(name, at=world.versions[3])
    made = create(world, request)
    assert made["branch"] == name and made["created"] is True
    assert world.store.head(name).id == world.base.id


# -- refusals ----------------------------------------------------------------------------

NAIVE_START = datetime(2026, 1, 5, 7, 0)

REFUSALS: list[tuple[str, dict[str, Any], list[tuple[str, str]]]] = [
    (
        "a model that is not there",
        {"models": ["twin_model:nowhere"]},
        [("models[0]", "is not a model at the base version")],
    ),
    (
        "something that is not a model",
        {"models": [node("T1")]},
        [("models[0]", "is not a model at the base version")],
    ),
    (
        "a model that is not active",
        {"models": [model("level-candidate")]},
        [("models[0]", "is candidate, not active")],
    ),
    (
        "a model without a fitted state",
        {"models": [model("level-unfitted")]},
        [("models[0]", "has no fitted state")],
    ),
    (
        "a model whose target the ontology does not have",
        {"models": [model("lost-target")]},
        [("models[0]", "Tank has no property 'depth'")],
    ),
    (
        "a model whose target is not a number",
        {"models": [model("tank-name")]},
        [("models[0]", "a prediction is a number")],
    ),
    (
        "two models of one target",
        {"models": [model("level-naive"), model("demand-last"), model("level-again")]},
        [("models[2]", "which models[0] (twin_model:level-naive) predicts")],
    ),
    (
        "a horizon between two grid times",
        {"horizon": timedelta(minutes=20)},
        [("horizon", "not a whole multiple of the step of 900 s")],
    ),
    (
        "a horizon shorter than a step",
        {"horizon": timedelta(minutes=5)},
        [("horizon", "not a whole multiple")],
    ),
    (
        "a horizon of too many steps",
        {"horizon": 2001 * STEP},
        [("horizon", "2001 steps of the grid; at most 2000")],
    ),
    (
        "an intervention on an unknown type",
        {"interventions": [{**setting(1.0, grid(1)), "target": "Basin.level"}]},
        [("interventions[0].target", "unknown type 'Basin'")],
    ),
    (
        "an intervention on an unknown property",
        {"interventions": [{**setting(1.0, grid(1)), "target": "Valve.colour"}]},
        [("interventions[0].target", "Valve has no property 'colour'")],
    ),
    (
        "an intervention on something that is not a property",
        {"interventions": [{**setting(1.0, grid(1)), "target": "setting"}]},
        [("interventions[0].target", "not written Type.property")],
    ),
    (
        "an intervention on the uid",
        {"interventions": [{**setting("link:V7", grid(1)), "target": "Valve.uid"}]},
        [("interventions[0].target", "Valve has no property 'uid'")],
    ),
    (
        "an intervention on an object of another type",
        {"interventions": [{**setting(1.0, grid(1)), "uid": node("T1")}]},
        [("interventions[0].uid", "'node:T1' is not a Valve at the base version")],
    ),
    (
        "an intervention on an object that is not there",
        {"interventions": [setting(1.0, grid(1), key="V9")]},
        [("interventions[0].uid", "'link:V9' is not a Valve at the base version")],
    ),
    (
        "an object outside the abstract class",
        {
            "interventions": [
                {**setting("open", grid(1)), "target": "Link.status", "uid": "node:J1"}
            ]
        },
        [("interventions[0].uid", "'node:J1' is not a Link at the base version")],
    ),
    (
        "a start before the base",
        {"interventions": [setting(1.0, grid(-1))]},
        [("interventions[0].start", "is before the base time 2026-01-05T06:00:00Z")],
    ),
    (
        "a start at the end of the horizon",
        {"interventions": [setting(1.0, grid(8))]},
        [("interventions[0].start", "is not before the end of the horizon, 2026-01-05T08:00:00Z")],
    ),
    (
        "a start without a time zone",
        {"interventions": [setting(1.0, NAIVE_START)]},
        [("interventions[0].start", "has no time zone")],
    ),
    (
        "an end that is the start",
        {"interventions": [setting(1.0, grid(2), grid(2))]},
        [("interventions[0].end", "is not after start")],
    ),
    (
        "an end after the horizon",
        {"interventions": [setting(1.0, grid(2), grid(8.5))]},
        [("interventions[0].end", "is after the end of the horizon")],
    ),
    (
        "an end without a time zone",
        {"interventions": [setting(1.0, grid(0), NAIVE_START)]},
        [("interventions[0].end", "has no time zone")],
    ),
    (
        "two interventions on one property at once",
        {"interventions": [setting(1.0, grid(1), grid(4)), setting(2.0, grid(3), grid(5))]},
        [("interventions[1]", "overlaps interventions[0] on Valve.setting of link:V1")],
    ),
    (
        "one that starts while another holds to the end",
        {
            "interventions": [
                setting(1.0, grid(1), key="V2"),
                setting(1.0, grid(2)),
                setting(2.0, grid(5), grid(6), key="V2"),
            ]
        },
        [("interventions[2]", "overlaps interventions[0] on Valve.setting of link:V2")],
    ),
    (
        "one property named through its class and through its type",
        {
            "interventions": [
                {**setting("closed", grid(1), grid(4)), "target": "Link.status"},
                {**setting("open", grid(2), grid(3)), "target": "Valve.status"},
            ]
        },
        [("interventions[1]", "overlaps interventions[0] on Valve.status of link:V1")],
    ),
    (
        "a property that is predicted",
        {
            "interventions": [
                {"target": "Tank.level", "uid": node("T1"), "value": 3.0, "start": grid(1)}
            ]
        },
        [
            (
                "interventions[0].target",
                "Tank.level of node:T1 is predicted by twin_model:level-naive",
            )
        ],
    ),
    (
        "a value outside the enumeration",
        {"interventions": [{**setting("ajar", grid(1)), "target": "Valve.status"}]},
        [("interventions[0].value", "'open' or 'closed'")],
    ),
    (
        "a value of another type",
        {"interventions": [setting("wide", grid(1))]},
        [("interventions[0].value", "setting")],
    ),
    (
        "a value beyond the property's bounds",
        {
            "interventions": [
                {"target": "Pipe.diameter", "uid": link("P1"), "value": 9000.0, "start": grid(1)}
            ]
        },
        [("interventions[0].value", "diameter")],
    ),
    (
        "nothing for a property that is required",
        {"interventions": [{**setting(None, grid(1)), "target": "Valve.valve_type"}]},
        [("interventions[0].value", "valve_type")],
    ),
]


@pytest.mark.parametrize(
    ("change", "expected"),
    [pytest.param(change, expected, id=name) for name, change, expected in REFUSALS],
)
def test_a_request_that_cannot_be_met_is_refused_with_its_problem(
    world: World, change: dict[str, Any], expected: list[tuple[str, str]]
):
    before = (world.store.head().id, branch_names(world))
    request = ScenarioRequest(**{**world.request().model_dump(), **change})
    error = refusal(world, request)
    assert error.code == "scenario.bad_request"
    problems = error.detail["problems"]
    assert [found["member"] for found in problems] == [member for member, _ in expected]
    for found, (_, said) in zip(problems, expected, strict=True):
        assert said in found["message"], found
    assert expected[0][1] in error.message
    # Nothing was written.
    assert (world.store.head().id, branch_names(world)) == before


def test_every_problem_of_a_request_is_reported_together(world: World):
    before = (world.store.head().id, branch_names(world))
    request = world.request(
        models=("level-naive", "level-candidate", "lost-target"),
        horizon=timedelta(minutes=125),
        interventions=[
            setting(1.0, grid(-2), grid(3)),
            {**setting(1.0, grid(1)), "target": "Valve.colour"},
            setting("wide", grid(1), grid(9), key="V9"),
            setting("wide", grid(1), grid(2), key="V2"),
            {"target": "Tank.level", "uid": node("T2"), "value": 30.0, "start": grid(3)},
        ],
    )
    error = refusal(world, request)
    assert error.code == "scenario.bad_request"
    assert [found["member"] for found in error.detail["problems"]] == [
        "horizon",
        # Not active, and of a target another model of the request predicts.
        "models[1]",
        "models[1]",
        "models[2]",
        "interventions[0].start",
        "interventions[1].target",
        "interventions[2].end",
        "interventions[2].uid",
        "interventions[3].value",
        "interventions[4].target",
    ]
    assert "(and 9 more)" in error.message
    assert (world.store.head().id, branch_names(world)) == before


def test_a_base_or_a_step_that_gives_no_grid_is_refused_before_anything_else(world: World):
    base = world.base
    request = world.request(models=("lost-target",))
    elsewhere = VersionRef(base.id, base.seq, base.t, "scenario-0123456789ab")
    error = refusal(world, request, at=elsewhere)
    assert error.code == "scenario.bad_request"
    assert [found["member"] for found in error.detail["problems"]] == ["at"]
    assert "a version of main" in error.detail["problems"][0]["message"]

    unknown = VersionRef("0123456789abcdefghijklmnopqrstuv", 3, base.t, "main")
    error = refusal(world, request, at=unknown)
    assert [found["member"] for found in error.detail["problems"]] == ["at"]

    for step in (timedelta(0), -STEP, timedelta(minutes=15, microseconds=1)):
        error = refusal(world, request, step=step)
        assert error.code == "scenario.bad_request"
        assert [found["member"] for found in error.detail["problems"]] == ["step"]


def test_adjacent_interventions_on_one_property_do_not_overlap(world: World):
    made = create(
        world,
        world.request(
            interventions=[
                setting(5.0, grid(1), grid(3)),
                setting(6.0, grid(3), grid(5)),
                setting(7.0, grid(5)),
            ]
        ),
    )
    assert made["created"] is True and made["status"] == "created"


# -- the rule on controls ------------------------------------------------------------------


def test_a_control_is_changed_only_under_models_that_hold_when_it_is(world: World):
    before = (world.store.head().id, branch_names(world))
    error = refusal(world, world.request(models=("level-naive",), interventions=[closed(grid(1))]))
    assert error.code == "scenario.not_interventional"
    assert error.detail == {
        "models": [model("level-naive")],
        "interventions": ["interventions[0]"],
    }
    assert model("level-naive") in error.message

    # Every model has to hold, and those that do not are the ones named.
    error = refusal(
        world,
        world.request(
            models=("level-balance", "demand-last"),
            interventions=[setting(4.0, grid(1)), closed(grid(2), grid(5))],
        ),
    )
    assert error.code == "scenario.not_interventional"
    assert error.detail == {
        "models": [model("demand-last")],
        "interventions": ["interventions[1]"],
    }
    # The control is the same one when it is named through the class above its type.
    error = refusal(world, world.request(interventions=[closed(grid(1), target="Link.status")]))
    assert error.code == "scenario.not_interventional"
    assert (world.store.head().id, branch_names(world)) == before


def test_a_model_that_holds_under_controls_predicts_a_scenario_that_changes_one(world: World):
    made = create(
        world, world.request(models=("level-balance",), interventions=[closed(grid(0), grid(4))])
    )
    assert made["created"] is True
    held = cells(world, "Valve", "status", "head", made["branch"])[link("V1")]
    assert (held[0], held[1]["k"], held[1]["s"]) == ("closed", "assumed", made["uid"])


def test_what_is_not_a_control_is_changed_under_any_model(world: World):
    made = create(
        world,
        world.request(
            models=("level-naive", "demand-last"),
            interventions=[
                setting(4.0, grid(1)),
                {
                    "target": "Junction.base_demand",
                    "uid": node("J2"),
                    "value": 6.5,
                    "start": grid(2),
                },
            ],
        ),
    )
    assert made["created"] is True
    # A request without interventions is under no rule at all.
    assert create(world, world.request(models=("level-naive",)))["created"] is True


# -- failures after the branch was made ----------------------------------------------------


def test_a_store_that_refuses_what_is_assumed_leaves_no_branch(world: World):
    # A tank's level has to lie within its bounds: a rule of the type, which the
    # database holds. Lowering the ceiling below the level breaks it.
    request = world.request(
        interventions=[
            {"target": "Tank.max_level", "uid": node("T1"), "value": 1.0, "start": grid(0)}
        ]
    )
    before = (world.store.head().id, branch_names(world))
    with pytest.raises(StoreError) as caught:
        create(world, request)
    assert caught.value.code == "store.violation"
    assert (world.store.head().id, branch_names(world)) == before
    uid = records.scenario_uid(scenario_id(request, world.base, STEP))
    assert world.store.get(uid) is None
    # The request is not held against a later one: without the ceiling it is made.
    assert create(world, world.request())["created"] is True


def test_a_record_that_cannot_be_written_leaves_no_branch(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    request = world.request(interventions=[setting(7.5, grid(0))])
    before = (world.store.head().id, branch_names(world))

    def unwritable(*args: Any, **kwargs: Any) -> None:
        raise StoreError("the live line cannot be written", code="store.database")

    monkeypatch.setattr(records, "write", unwritable)
    with pytest.raises(StoreError) as caught:
        create(world, request)
    assert caught.value.code == "store.database"
    assert (world.store.head().id, branch_names(world)) == before
    monkeypatch.undo()

    made = create(world, request)
    assert made["created"] is True
    assert len(branch_versions(world, made["branch"])) == 1
