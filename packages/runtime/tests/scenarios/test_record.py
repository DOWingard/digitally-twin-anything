"""The record's versions on a live line that moves, a base the ontology has changed
since, and what the package logs of what it does."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from dta_core.schema import FactKind
from dta_core.store import LIVE_BRANCH, ChangeSet, CommitMeta, Provenance
from dta_runtime import scenarios
from dta_runtime.scenarios import ScenarioError
from dta_runtime.twin import Bundle, install_twin
from scenarios_testkit import (
    STEP,
    InProcess,
    World,
    branch_names,
    closed,
    create,
    grid,
    link,
    manifest_for,
    node,
    setting,
)

_LINED = "      lined:\n        range: boolean\n"
CONTEXT = "runtime.scenarios"


def test_the_record_is_written_again_when_the_live_line_moves_under_it(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A sync that lands between reading the live head and writing the record gives the
    head a later time; the record is then written at that time."""
    store = world.store
    real = store.commit
    landed: list[Any] = []

    def racing(changes: Any, meta: Any, branch: str = LIVE_BRANCH) -> Any:
        if branch == LIVE_BRANCH and meta.cause == "registration" and not landed:
            t = grid(1)
            observed = ChangeSet(
                upserts={"Tank": [{"uid": node("T1"), "level": 4.2}]},
                provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
            )
            landed.append(real(observed, CommitMeta(cause="sync", actor="scada", t=t)))
        return real(changes, meta, branch)

    monkeypatch.setattr(store, "commit", racing)
    with caplog.at_level(logging.WARNING, logger="dta"):
        made = create(world, world.request())
    monkeypatch.undo()

    assert made["created"] is True and len(landed) == 1
    written = store.versions(f"#{landed[0].seq + 1}..head")
    assert [(v.cause, v.t, v.extra["scenario"]) for v in written] == [
        ("registration", grid(1), made["uid"])
    ]
    assert store.head(made["branch"]).id == world.base.id
    warned = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and getattr(record, "context", "") == CONTEXT
    ]
    assert len(warned) == 1 and "the live line moved on" in warned[0]


def test_a_base_the_ontology_has_changed_since_is_refused(make_world: Callable[[], World]):
    world = make_world()
    text = world.twin.ontology.source
    assert _LINED in text
    grown = text.replace(_LINED, _LINED + "      material:\n        range: string\n")
    migrated = install_twin(
        world.store.settings, Bundle(manifest=manifest_for(grown), ontology_text=grown)
    )
    try:
        assert "migrated" in migrated.actions
        request = world.request()
        with pytest.raises(ScenarioError) as caught:
            scenarios.create(migrated.twin, request, at=world.base, step=STEP)
        assert caught.value.code == "scenario.bad_request"
        assert [found["member"] for found in caught.value.detail["problems"]] == ["at"]
        assert "the ontology has changed" in caught.value.detail["problems"][0]["message"]
        assert branch_names(world) == ["main"]
        # From a version that holds the ontology in force, the same request is met.
        made = scenarios.create(migrated.twin, request, at=migrated.twin.store.head(), step=STEP)
        assert made["created"] is True
    finally:
        migrated.twin.close()


# -- what is logged ------------------------------------------------------------------------


def logged(caplog: pytest.LogCaptureFixture, *levels: str) -> list[tuple[str, dict[str, Any]]]:
    """(message, data) of what the package logged at the levels asked for."""
    return [
        (record.getMessage(), getattr(record, "data", {}))
        for record in caplog.records
        if getattr(record, "context", "") == CONTEXT and record.levelname in levels
    ]


def everything(caplog: pytest.LogCaptureFixture) -> str:
    """Every line any part of the twin logged, as it would be written."""
    return "\n".join(
        json.dumps([record.getMessage(), getattr(record, "data", {})], default=str)
        for record in caplog.records
    )


def test_the_milestones_of_a_scenario_are_logged_and_its_values_never(
    world: World, predictor: InProcess, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.DEBUG, logger="dta"):
        made = create(
            world,
            world.request(
                models=("level-balance",),
                interventions=[setting(7.31415, grid(0), grid(3)), closed(grid(2), grid(5))],
            ),
        )
        done = scenarios.run(world.twin, made["uid"], predict=predictor, budget_s=60.0)
        scenarios.compare(world.twin, made["uid"], targets=["Valve.setting"])
        scenarios.delete(world.twin, made["uid"])

    milestones = dict(logged(caplog, "INFO"))
    assert milestones["scenario created"]["scenario"] == made["uid"]
    # What is changed and when, never to what.
    assert milestones["scenario created"]["interventions"] == [
        {
            "target": "Valve.setting",
            "uid": link("V1"),
            "start": "2026-01-05T06:00:00Z",
            "end": "2026-01-05T06:45:00Z",
        },
        {
            "target": "Valve.status",
            "uid": link("V1"),
            "start": "2026-01-05T06:30:00Z",
            "end": "2026-01-05T07:15:00Z",
        },
    ]
    assert milestones["scenario run started"]["steps"] == 8
    finished = milestones["scenario run finished"]
    assert (finished["versions"], finished["duration_ms"]) == (8, done["duration_ms"])
    # Two levels at each grid time, and three changes of what is assumed: the valve
    # closed at 2, the setting back at 3, the valve open again at 5.
    assert finished["rows"] == 8 * 2 + 3
    assert milestones["scenario deleted"]["branch_removed"] is True
    assert milestones["scenario compared"]["rows"] == 2 * 9
    assert logged(caplog, "WARNING", "ERROR") == []
    assert "7.31415" not in everything(caplog)


def test_refusals_are_warnings_and_failures_are_errors(
    world: World, predictor: InProcess, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.DEBUG, logger="dta"):
        with pytest.raises(ScenarioError):
            create(world, world.request(interventions=[setting("sesame-7.31415", grid(1))]))
        with pytest.raises(ScenarioError):
            create(world, world.request(interventions=[closed(grid(1))]))
        with pytest.raises(ScenarioError):
            scenarios.get(world.twin, "twin_scenario:000000000000")
        made = create(world, world.request(interventions=[setting(7.31415, grid(1))]))
        with pytest.raises(ScenarioError):
            scenarios.run(world.twin, made["uid"], predict=lambda *a, **k: None, budget_s=60.0)
        with pytest.raises(ScenarioError):
            scenarios.compare(world.twin, made["uid"], targets=["Valve.status"])
        scenarios.run(world.twin, made["uid"], predict=predictor, budget_s=60.0)
        with pytest.raises(ScenarioError):
            scenarios.run(world.twin, made["uid"], predict=predictor, budget_s=60.0)

    warnings = logged(caplog, "WARNING")
    assert [message for message, _ in warnings] == [
        "scenario refused: the request cannot be met",
        "scenario refused: a control is changed and a model does not hold under it",
        "no such scenario",
        "comparison refused",
        "scenario run refused",
    ]
    # Of a value that was refused, the log says which member it was and no more.
    assert warnings[0][1]["problems"] == [{"member": "interventions[0].value"}]
    assert warnings[4][1] == {
        "scenario": made["uid"],
        "status": "done",
        "code": "scenario.already_run",
    }

    errors = logged(caplog, "ERROR")
    assert [message for message, _ in errors] == ["scenario run failed"]
    data = errors[0][1]
    assert (data["scenario"], data["code"], data["versions_written"]) == (
        made["uid"],
        "scenario.bad_prediction",
        0,
    )
    # The failure is logged with its stack; the record keeps none.
    failed = [record for record in caplog.records if record.getMessage() == "scenario run failed"]
    assert failed[0].exc_info is not None
    assert "sesame" not in everything(caplog) and "7.31415" not in everything(caplog)
