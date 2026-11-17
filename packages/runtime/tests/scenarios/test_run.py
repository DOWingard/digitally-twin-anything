"""Filling a scenario's branch: one version per grid time, holding what the models
predict and what is assumed from then on, and nothing of it on the live line."""

from __future__ import annotations

from typing import Any

import pytest
from dta_core.store.model import iso_utc
from dta_runtime import scenarios
from dta_runtime.protocol.worker import InterventionRef
from scenarios_testkit import (
    INFLOW,
    LAST,
    OUTFLOW,
    STEP,
    InProcess,
    World,
    branch_versions,
    cells,
    closed,
    create,
    demand,
    grid,
    level,
    link,
    model,
    node,
    origins,
    setting,
)

STEPS = 8


def run(world: World, predictor: InProcess, made: dict[str, Any]) -> dict[str, Any]:
    return scenarios.run(world.twin, made["uid"], predict=predictor, budget_s=60.0)


def origin(found: tuple[Any, dict[str, Any]]) -> tuple[Any, str, str, str]:
    """A cell as its value, kind, source and valid time."""
    value, provenance = found
    return (value, provenance["k"], provenance["s"], provenance["t"])


def by_step(world: World, branch: str, type_name: str, name: str, uid: str) -> list[tuple]:
    """One property of one object at each version the branch has after the base."""
    return [
        origin(cells(world, type_name, name, version.ref, branch)[uid])
        for version in branch_versions(world, branch)
    ]


def test_a_run_writes_what_the_models_predict_one_version_per_grid_time(
    world: World, predictor: InProcess
):
    base = world.base
    made = create(world, world.request(models=("level-naive", "demand-last")))
    uid, branch = made["uid"], made["branch"]
    live = world.store.head()
    done = run(world, predictor, made)

    assert (done["status"], done["error"], done["versions"]) == ("done", None, STEPS)
    assert done["duration_ms"] > 0
    assert set(done) == (set(made) - {"created"}) | {"versions", "duration_ms"}

    # The branch gained a version at each grid time, and at no other.
    added = branch_versions(world, branch)
    assert [(v.seq, v.t, v.cause, v.actor) for v in added] == [
        (base.seq + j, grid(j), "scenario", "scenarios") for j in range(1, STEPS + 1)
    ]
    assert [v.extra for v in added] == [{"scenario": uid, "step": j} for j in range(1, STEPS + 1)]

    # Two models of different targets land in the same versions, each cell simulated
    # by its model and valid at the version's time.
    for j, version in enumerate(added, start=1):
        valid = iso_utc(grid(j))
        levels = cells(world, "Tank", "level", version.ref, branch)
        assert {key: origin(found) for key, found in levels.items()} == {
            node("T1"): (level("T1", LAST + j), "simulated", model("level-naive"), valid),
            node("T2"): (level("T2", LAST + j), "simulated", model("level-naive"), valid),
        }
        demands = cells(world, "Junction", "demand", version.ref, branch)
        assert {key: origin(found) for key, found in demands.items()} == {
            node("J1"): (demand("J1", LAST), "simulated", model("demand-last"), valid),
            node("J2"): (demand("J2", LAST), "simulated", model("demand-last"), valid),
        }
        # What no model predicts and nothing assumes is as it was observed.
        elevation = cells(world, "Tank", "elevation", version.ref, branch)[node("T1")]
        assert (elevation[0], elevation[1]["k"]) == (60.0, "observed")

    # The live line moved by the record's two versions and holds nothing of the run.
    moved = world.store.versions(f"#{live.seq + 1}..head")
    assert [(v.cause, v.actor, v.t, v.extra) for v in moved] == [
        ("registration", "scenarios", live.t, {"scenario": uid, "status": "running"}),
        ("registration", "scenarios", live.t, {"scenario": uid, "status": "done"}),
    ]
    assert {kind for kind, _ in origins(world)} == {"observed"}
    assert cells(world, "Tank", "level")[node("T1")][0] == level("T1", LAST)
    assert world.store.get(uid).values["status"] == "done"

    # Each model was asked once, in the order of the record, with its record as it
    # was at the base version.
    assert [call["model"] for call in predictor.calls] == [
        model("level-naive"),
        model("demand-last"),
    ]
    for call in predictor.calls:
        assert (call["base"], call["branch"], call["horizon"], call["step"]) == (
            base.id,
            branch,
            STEPS,
            STEP,
        )
        assert call["interventions"] == [] and 0 < call["deadline_s"] <= 60.0
    assert predictor.calls[1]["deadline_s"] < predictor.calls[0]["deadline_s"]


def test_an_intervention_shows_from_the_first_grid_time_at_or_after_its_start_to_its_end(
    world: World, predictor: InProcess
):
    made = create(
        world,
        world.request(
            interventions=[
                # Between grid times 2 and 3, to between 5 and 6.
                setting(7.5, grid(2.4), grid(5.2)),
                # On grid times.
                setting(0.5, grid(4), grid(6), key="V2"),
            ]
        ),
    )
    uid, branch = made["uid"], made["branch"]
    # Nothing is assumed at the base, so nothing was written there.
    assert branch_versions(world, branch) == []
    assert run(world, predictor, made)["versions"] == STEPS

    observed = (1.0, "observed", "scada", iso_utc(grid(1 - LAST)))
    held = (7.5, "assumed", uid, iso_utc(grid(3)))
    back = (1.0, "assumed", uid, iso_utc(grid(6)))
    assert by_step(world, branch, "Valve", "setting", link("V1")) == [
        observed,
        observed,
        held,
        held,
        held,
        back,
        back,
        back,
    ]
    values = [found[0] for found in by_step(world, branch, "Valve", "setting", link("V2"))]
    assert values == [2.0, 2.0, 2.0, 0.5, 0.5, 2.0, 2.0, 2.0]
    kinds = [found[1] for found in by_step(world, branch, "Valve", "setting", link("V2"))]
    assert kinds == ["observed"] * 3 + ["assumed"] * 5
    # The predictions are in the same versions.
    assert [found[1] for found in by_step(world, branch, "Tank", "level", node("T1"))] == [
        "simulated"
    ] * STEPS
    # A family that does not hold under interventions was given none.
    assert predictor.calls[0]["interventions"] == [
        InterventionRef(**setting(7.5, grid(2.4), grid(5.2))),
        InterventionRef(**setting(0.5, grid(4), grid(6), key="V2")),
    ]
    assert {kind for kind, _ in origins(world)} == {"observed"}


def test_interventions_follow_one_another_on_one_property(world: World, predictor: InProcess):
    made = create(
        world,
        world.request(
            interventions=[
                setting(5.0, grid(0), grid(2)),
                setting(6.0, grid(2), grid(3.5)),
                # Starts and ends between two grid times: it never shows.
                setting(9.0, grid(4.2), grid(4.8)),
                # To the end of the horizon.
                setting(7.0, grid(6)),
            ]
        ),
    )
    uid, branch = made["uid"], made["branch"]
    done = run(world, predictor, made)
    # The version at the base time was there before the run.
    assert done["versions"] == STEPS and len(branch_versions(world, branch)) == STEPS + 1

    found = by_step(world, branch, "Valve", "setting", link("V1"))
    assert [(value, kind, source) for value, kind, source, _ in found] == [
        (value, "assumed", uid) for value in (5.0, 5.0, 6.0, 6.0, 1.0, 1.0, 7.0, 7.0, 7.0)
    ]
    # Each is valid from the grid time it took effect at.
    assert [valid for _, _, _, valid in found] == [
        iso_utc(grid(j)) for j in (0, 0, 2, 2, 4, 5, 6, 6, 6)
    ]


def test_closing_the_valve_changes_what_the_tank_is_predicted_to_do(
    world: World, predictor: InProcess
):
    plain = create(world, world.request(models=("level-balance",)))
    # Closed from between grid times 2 and 3 until grid time 6: at 3, 4 and 5.
    shut = create(
        world,
        world.request(models=("level-balance",), interventions=[closed(grid(2.5), grid(6))]),
    )
    run(world, predictor, plain)
    run(world, predictor, shut)

    def levels(made: dict[str, Any], key: str) -> list[float]:
        return [found[0] for found in by_step(world, made["branch"], "Tank", "level", node(key))]

    # By hand: a tank gains INFLOW a step while its valve is open and loses OUTFLOW.
    start = level("T1", LAST)
    open_steps = [j - min(max(j - 2, 0), 3) for j in range(1, STEPS + 1)]
    assert levels(plain, "T1") == pytest.approx(
        [start + (INFLOW - OUTFLOW) * j for j in range(1, STEPS + 1)]
    )
    assert levels(shut, "T1") == pytest.approx(
        [start + INFLOW * still - OUTFLOW * j for j, still in enumerate(open_steps, start=1)]
    )
    assert [a - b for a, b in zip(levels(shut, "T1"), levels(plain, "T1"), strict=True)] == (
        pytest.approx([0.0, 0.0, -0.5, -1.0, -1.5, -1.5, -1.5, -1.5])
    )
    # The other tank is fed by another valve.
    assert levels(shut, "T2") == pytest.approx(levels(plain, "T2"))
    assert levels(plain, "T2") == pytest.approx(
        [level("T2", LAST) + (INFLOW - OUTFLOW) * j for j in range(1, STEPS + 1)]
    )

    # The branch shows the valve as the model was told it is.
    status = by_step(world, shut["branch"], "Valve", "status", link("V1"))
    assert [(value, kind) for value, kind, _, _ in status] == [
        ("open", "observed"),
        ("open", "observed"),
        ("closed", "assumed"),
        ("closed", "assumed"),
        ("closed", "assumed"),
        ("open", "assumed"),
        ("open", "assumed"),
        ("open", "assumed"),
    ]
    assert predictor.calls[-1]["interventions"] == [InterventionRef(**closed(grid(2.5), grid(6)))]
    assert predictor.calls[0]["interventions"] == []


def test_a_horizon_of_one_step_is_one_version(world: World, predictor: InProcess):
    made = create(world, world.request(horizon=STEP, interventions=[setting(3.0, grid(0.5))]))
    done = run(world, predictor, made)
    assert done["versions"] == 1
    added = branch_versions(world, made["branch"])
    assert [v.t for v in added] == [grid(1)]
    assert cells(world, "Valve", "setting", "head", made["branch"])[link("V1")][0] == 3.0
    assert cells(world, "Tank", "level", "head", made["branch"])[node("T1")][0] == level(
        "T1", LAST + 1
    )
