"""Scenarios through the API: made, run as a job by the real worker, compared, deleted;
and what is done about a run that a process left unfinished."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from compute_testkit import ARROW, INFLOW, LAST, OUTFLOW, Small, World, at, level, plant_code
from dta_runtime.api import create_app, registry
from dta_runtime.scenarios import records
from live_api_testkit import HEADERS
from live_api_testkit import records as logged
from starlette.testclient import TestClient

NAIVE = "twin_model:level/naive"
PLANT = "twin_model:whatif/plant"


def iso(steps: float) -> str:
    return at(steps).isoformat().replace("+00:00", "Z")


def closed(start: float) -> dict[str, Any]:
    return {"target": "Valve.status", "uid": "link:V1", "value": "closed", "start": iso(start)}


def test_a_scenario_is_made_run_as_a_job_compared_and_deleted(
    world: World, evaluated: dict[str, Any]
) -> None:
    base = world.head
    body = {
        "name": "valve V1 closed",
        "description": "what the tanks hold with V1 shut",
        "interventions": [closed(LAST + 2)],
        "horizon": "PT1H",
        "models": [PLANT],
    }

    made = world.post("/scenarios", json=body)

    assert made.status_code == 201, made.text
    scenario = made.json()["data"]
    assert re.fullmatch(r"[0-9a-f]{12}", scenario["id"])
    assert scenario["uid"] == f"twin_scenario:{scenario['id']}"
    assert scenario["branch"] == f"scenario-{scenario['id']}" and scenario["created"] is True
    assert scenario["status"] == "created" and scenario["name"] == "valve V1 closed"
    assert scenario["base_version"] == base.id and scenario["base_t"] == iso(LAST)
    assert scenario["horizon"] == "PT1H" and scenario["step"] == "PT15M"
    assert scenario["models"] == [PLANT]
    assert scenario["interventions"][0]["uid"] == "link:V1"
    assert scenario["interventions"][0]["value"] == "closed"
    # The record is one version of the live line; the branch starts at the base.
    assert world.head.seq == base.seq + 1 and world.head.t == base.t
    assert scenario["branch"] in [found["name"] for found in world.ok("/history/branches")["data"]]

    # The same request at the same base is the same scenario.
    again = world.post("/scenarios", json=body, params={"at": base.id})
    assert again.status_code == 200 and again.json()["data"]["created"] is False
    assert again.json()["data"]["id"] == scenario["id"]
    plain = world.post(
        "/scenarios",
        json={"name": "as it is", "horizon": "PT1H", "models": [PLANT]},
        params={"at": base.id},
    )
    assert plain.status_code == 201, plain.text
    other = plain.json()["data"]

    listed = {found["id"]: found for found in world.ok("/scenarios")["data"]}
    assert {scenario["id"], other["id"]} <= set(listed)
    assert listed[scenario["id"]]["status"] == "created"
    one = world.ok(f"/scenarios/{scenario['id']}")["data"]
    assert one["exists"] is True and one["head"]["t"] == iso(LAST)
    assert world.ok(f"/scenarios/{scenario['uid']}")["data"]["id"] == scenario["id"]

    started = world.post(f"/scenarios/{scenario['id']}/run", json={})
    assert started.status_code == 202, started.text
    job = started.json()["job"]
    assert job["lane"] == "compute" and job["kind"] == "scenario"
    assert job["params"]["scenario"] == scenario["uid"]
    result = world.done(started)
    assert result["status"] == "done" and result["versions"] == 4
    assert world.ok(f"/scenarios/{scenario['id']}")["data"]["head"]["t"] == iso(LAST + 4)
    world.done(world.post(f"/scenarios/{other['uid']}/run"))
    world.refused(world.post(f"/scenarios/{scenario['id']}/run"), 409, "scenario.already_run")

    # What was assumed and what was predicted are facts of the branch, of their own kinds.
    store = world.twin.store
    valve = store.get("link:V1", branch=scenario["branch"])
    assert valve.values["status"] == "closed"
    assert valve.provenance_of("status").kind == "assumed"
    tank = store.get("node:T1", branch=scenario["branch"])
    assert tank.provenance_of("level").kind == "simulated"
    assert tank.provenance_of("level").source == PLANT
    assert store.get("node:T1").values["level"] == level("T1", LAST)

    # Beside the same model without the intervention, closing V1 is what T1 lacks.
    compared = world.ok(
        f"/scenarios/{scenario['id']}/compare", targets="Tank.level", against=other["id"]
    )
    rows = {(row["uid"], row["t"]): row for row in compared["data"]}
    assert len(rows) == 10 and compared["next_cursor"] is None
    gain = INFLOW - OUTFLOW
    assert rows[("node:T1", iso(LAST))]["difference"] == 0
    assert rows[("node:T1", iso(LAST + 1))]["difference"] == pytest.approx(0.0)
    assert rows[("node:T1", iso(LAST + 2))]["difference"] == pytest.approx(-INFLOW)
    last = rows[("node:T1", iso(LAST + 4))]
    assert last["difference"] == pytest.approx(-3 * INFLOW) and last["target"] == "Tank.level"
    assert last["scenario"] == pytest.approx(level("T1", LAST) + gain - 3 * OUTFLOW)
    assert last["against"] == pytest.approx(level("T1", LAST) + 4 * gain)
    assert all(rows[("node:T2", iso(LAST + j))]["difference"] == 0 for j in range(5))
    # Beside the live line, which holds its last values.
    live = world.ok(f"/scenarios/{scenario['id']}/compare", targets="Tank.level", uids="node:T2")
    assert [row["uid"] for row in live["data"]] == ["node:T2"] * 5
    assert live["data"][0]["against"] == level("T2", LAST) and live["data"][0]["difference"] == 0
    assert live["data"][-1]["scenario"] == pytest.approx(level("T2", LAST) + 4 * gain)
    arrow = world.get(f"/scenarios/{scenario['id']}/compare", targets="Tank.level", format="arrow")
    assert arrow.status_code == 200 and arrow.headers["content-type"] == ARROW
    table = pa.ipc.open_stream(io.BytesIO(arrow.content)).read_all()
    assert table.column_names == ["target", "uid", "t", "scenario", "against", "difference"]
    refused = world.refused(
        world.get(f"/scenarios/{scenario['id']}/compare", targets="Tank.nothing"),
        400,
        "scenario.bad_request",
    )
    assert refused["errors"]
    world.refused(world.get(f"/scenarios/{scenario['id']}/compare"), 400, "api.bad_request")

    ran = world.head
    gone = world.delete(f"/scenarios/{scenario['id']}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["data"]["status"] == "deleted" and gone.json()["data"]["exists"] is False
    assert scenario["id"] not in [found["id"] for found in world.ok("/scenarios")["data"]]
    kept = world.ok("/scenarios", include_deleted="true")["data"]
    assert scenario["id"] in [found["id"] for found in kept]
    assert world.ok(f"/scenarios/{scenario['id']}")["data"]["status"] == "deleted"
    world.refused(
        world.get(f"/scenarios/{scenario['id']}/compare", targets="Tank.level"),
        410,
        "scenario.deleted",
    )
    world.refused(world.post(f"/scenarios/{scenario['id']}/run"), 410, "scenario.deleted")
    assert world.delete(f"/scenarios/{scenario['id']}").json()["data"]["status"] == "deleted"
    # Before it was deleted, as the history has it.
    earlier = world.ok(f"/scenarios/{scenario['id']}", at=ran.id)["data"]
    assert earlier["status"] == "done"


def test_a_scenario_that_cannot_be_is_refused_and_nothing_is_written(
    world: World, evaluated: dict[str, Any]
) -> None:
    head = world.head
    body = {"name": "refused", "horizon": "PT1H", "models": [PLANT]}

    world.refused(
        world.post(
            "/scenarios", json={**body, "models": [NAIVE], "interventions": [closed(LAST + 1)]}
        ),
        409,
        "scenario.not_interventional",
    )
    unknown = world.refused(
        world.post("/scenarios", json={**body, "models": ["twin_model:level/nothing"]}),
        400,
        "scenario.bad_request",
    )
    assert unknown["errors"]
    lost = {**closed(LAST + 1), "uid": "link:nothing"}
    world.refused(
        world.post("/scenarios", json={**body, "interventions": [lost]}),
        400,
        "scenario.bad_request",
    )
    for wrong in ({"horizon": "PT1H", "models": [PLANT]}, {**body, "models": []}, {**body, "x": 1}):
        refused = world.refused(world.post("/scenarios", json=wrong), 400, "api.bad_request")
        assert refused["errors"]
    world.refused(world.post("/scenarios", content=b"not json"), 400, "api.bad_request")
    world.refused(
        world.post("/scenarios", json=body, params={"at": "#9999"}), 404, "store.unknown_version"
    )
    for path in ("/scenarios/0a1b2c3d4e5f", "/scenarios/0a1b2c3d4e5f/compare?targets=Tank.level"):
        world.refused(
            world.client.get(f"/api/v1{path}", headers=HEADERS), 404, "scenario.not_found"
        )
    world.refused(world.post("/scenarios/0a1b2c3d4e5f/run"), 404, "scenario.not_found")
    world.refused(world.delete("/scenarios/0a1b2c3d4e5f"), 404, "scenario.not_found")

    assert world.head.id == head.id


def test_a_scenario_left_running_by_a_process_that_ended_is_marked_failed(
    small: Callable[..., Small], log_lines: Path
) -> None:
    api = small(history=4)
    api.register(api.upload(plant_code()))
    state = api.upload(b"a fitted state")
    registry.commit(
        api.services,
        {
            "TwinArtifact": [
                registry.artifact_row(api.services, state, "application/octet-stream", "a state")
            ],
            "TwinModel": [
                {
                    "uid": "twin_model:demand/last",
                    "status": "active",
                    "fitted": f"twin_artifact:{state}",
                }
            ],
        },
        note="made active by the test",
    )
    made = api.post(
        "/scenarios",
        json={"name": "interrupted", "horizon": "PT30M", "models": ["twin_model:demand/last"]},
    )
    assert made.status_code == 201, made.text
    scenario = made.json()["data"]
    # No worker: a run is refused before a job is made.
    api.refused(api.post(f"/scenarios/{scenario['id']}/run"), 503, "worker.unavailable")
    records.set_status(api.twin, scenario["uid"], records.RUNNING)
    assert api.ok(f"/scenarios/{scenario['id']}")["data"]["status"] == "running"

    # The API starts again over the same twin, as after a crash.
    with TestClient(create_app(api.services.settings, api.twin)) as restarted:
        found = restarted.get(f"/api/v1/scenarios/{scenario['id']}", headers=HEADERS).json()["data"]
        assert found["status"] == "failed"
        assert found["error"].startswith("scenario.interrupted")
        gone = restarted.delete(f"/api/v1/scenarios/{scenario['id']}", headers=HEADERS)
        assert gone.status_code == 200 and gone.json()["data"]["status"] == "deleted"

    warned = [
        found
        for found in logged(log_lines)
        if found["context"] == "api.scenarios" and found["level"] == "warn"
    ]
    assert len(warned) == 1 and warned[0]["data"]["scenario"] == scenario["uid"]
