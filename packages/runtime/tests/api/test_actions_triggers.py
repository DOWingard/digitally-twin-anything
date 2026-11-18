"""Functions run on their triggers through the API, the schedule that names what the
scheduler asks for, the real scheduler following it, and an API that refuses to start
without a variable an executor needs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from actions_api_testkit import T0, Acting, acting_document, at
from actions_testkit import manifest_of, ontology_text
from api_testkit import TOKEN
from compute_testkit import runtime_settings
from dta_core.store import StoreSettings
from dta_runtime.api import create_app
from dta_runtime.scheduler import Scheduler
from dta_runtime.settings import SchedulerSettings
from dta_runtime.twin import Bundle, install_twin
from fidelity_api_testkit import Clock
from live_testkit import Folder
from starlette.testclient import TestClient

MakeActing = Callable[..., Acting]
RUN = "TwinFunctionRun"


def runs(world: Acting, function: str) -> list[dict[str, Any]]:
    """The records of a function's runs, oldest first."""
    found = [
        row
        for row in world.store_rows(RUN).values()
        if row["function"] == f"twin_function:{function}"
    ]
    return sorted(found, key=lambda row: (row["started_at"], row["uid"]))


def test_a_keyed_run_is_a_job_and_a_run_on_its_interval_is_recorded(acting: MakeActing):
    world = acting()
    path = "/functions/quarter/run"
    asked = world.post(path, json={}, headers={"Idempotency-Key": "function:quarter:one"})
    assert asked.status_code == 202, asked.text
    job = world.wait(asked)
    assert (job["state"], job["kind"]) == ("succeeded", "function"), job
    result = job["result"]
    assert (result["value"], result["trigger"], result["function"]) == (4, "interval", "quarter")
    assert result["wrote"] is None
    (run,) = runs(world, "quarter")
    assert (run["trigger"], run["status"], run["rows_written"]) == ("interval", "succeeded", 0)
    assert run["at_version"] == result["version"]["id"]

    again = world.post(path, json={}, headers={"Idempotency-Key": "function:quarter:one"})
    assert again.status_code == 200 and again.json()["job"]["id"] == job["id"]
    # Without a key the run answers at once, and only answers.
    direct = world.post(path, json={})
    assert direct.status_code == 200, direct.text
    assert direct.json()["value"] == 4
    assert len(runs(world, "quarter")) == 1

    # A keyed run of a function run on request is on request: answered, not recorded.
    policy = world.done(
        world.post("/functions/pump_policy/run", json={}, headers={"Idempotency-Key": "p-1"})
    )
    assert policy["trigger"] == "on_request"
    assert policy["value"]["notes"] == ["the valve stands at 5"]
    assert runs(world, "pump_policy") == []
    world.refused(
        world.post("/functions/nothing/run", json={}, headers={"Idempotency-Key": "n-1"}),
        404,
        "function.not_found",
    )


def test_a_sync_that_wrote_runs_the_functions_on_commit_and_records_each_run(
    acting: MakeActing,
):
    world = acting()
    world.telemetry.append("speeds", [("PMP-10", at(2), 0.9)])
    world.clock.set(at(2))
    synced = world.done(world.post("/sync/runs", headers={"Idempotency-Key": "sync:test"}))
    assert synced["versions_written"] >= 1, synced
    head = synced["head"]["id"]
    started = {found["function"]: found for found in synced["functions_started"]}
    assert set(started) == {"junction_pressures", "stuck_gauge"}
    for name, found in started.items():
        assert found["key"] == f"commit:{name}:{head}"
        assert found["created"] is True

    wrote = world.job(started["junction_pressures"]["job"])
    assert wrote["state"] == "succeeded", wrote
    assert wrote["result"]["trigger"] == "on_commit"
    assert wrote["result"]["version"]["id"] == head
    assert wrote["result"]["rows_written"] == 2 and wrote["result"]["wrote"]
    failed = world.job(started["stuck_gauge"]["job"])
    assert failed["state"] == "failed", failed

    (pressures,) = runs(world, "junction_pressures")
    assert (pressures["trigger"], pressures["status"]) == ("on_commit", "succeeded")
    assert (pressures["rows_written"], pressures["at_version"]) == (2, head)
    (stuck,) = runs(world, "stuck_gauge")
    assert (stuck["trigger"], stuck["status"], stuck["rows_written"]) == ("on_commit", "failed", 0)
    assert stuck["error"] and stuck["at_version"] == head
    pressure = world.ok("/objects/Junction/node:J1")["data"]["values"]["pressure"]
    assert pressure == pytest.approx(4.0)


def entry(entry_id: str, kind: str, path: str, key: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "kind": kind,
        "interval": "PT15M",
        "method": "POST",
        "path": f"/api/v1{path}",
        "key": key,
        "body": {},
    }


def test_the_scheduler_asks_for_reconciliation_the_enabled_loops_and_interval_functions(
    acting: MakeActing, tmp_path: Path
):
    world = acting()
    entries = world.ok("/lifecycle/schedule")["entries"]
    assert [found["id"] for found in entries] == [
        "sync",
        "fidelity",
        "reconcile",
        "function:quarter",
    ]
    world.post("/control/loops/pumping/enable", json={"by": "operator@plant"})
    entries = world.ok("/lifecycle/schedule")["entries"]
    assert entries[2:] == [
        entry("reconcile", "reconcile", "/actions/reconcile", "reconcile:{boundary}"),
        entry("loop:pumping", "loop", "/control/loops/pumping/run", "loop:pumping:{boundary}"),
        entry(
            "function:quarter", "function", "/functions/quarter/run", "function:quarter:{boundary}"
        ),
    ]

    world.telemetry.append("speeds", [("PMP-10", at(2), 0.9)])
    world.clock.set(at(2))
    boundary = at(2).isoformat()
    scheduler = Scheduler(
        SchedulerSettings(api_url=world.url, api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05)
    )
    try:
        first = scheduler.tick()
        assert first["started"] == ["sync"], first
        sync_job = world.services.jobs.active("sync")
        if sync_job is not None:
            assert sync_job.done.wait(120)
        second = scheduler.tick()
        assert second["started"] == ["fidelity", "reconcile", "loop:pumping", "function:quarter"]
        fired = {found["id"]: found for found in second["fired"]}
        assert fired["reconcile"]["key"] == f"reconcile:{boundary}"
        # Reconciliation is answered at once; the others are jobs.
        assert fired["reconcile"]["job"] is None
        looped = world.job(fired["loop:pumping"]["job"])
        assert looped["state"] == "succeeded", looped
        assert looped["result"]["key"] == f"loop:pumping:{boundary}"
        assert world.ok("/control/loops/pumping")["data"]["last_run"]["key"] == (
            f"loop:pumping:{boundary}"
        )
        quarter = world.job(fired["function:quarter"]["job"])
        assert quarter["state"] == "succeeded", quarter
        (run,) = runs(world, "quarter")
        assert (run["trigger"], run["status"]) == ("interval", "succeeded")
        assert scheduler.tick()["reason"] == "not_due"
    finally:
        scheduler.close()
    # The scheduler's sync wrote: the functions on a commit ran after it.
    assert len(runs(world, "junction_pressures")) == 1


def test_a_variable_an_executor_needs_that_is_missing_stops_the_start(
    new_settings: Callable[[], StoreSettings],
    water_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    text = ontology_text(water_text)
    document = acting_document(text, Folder(tmp_path / "telemetry"), webhook=False)
    twin = install_twin(
        new_settings(), Bundle(manifest=manifest_of(document), ontology_text=text), t=T0
    ).twin
    try:
        monkeypatch.setenv("PLANT_URL", "http://127.0.0.1:18999")
        monkeypatch.delenv("PLANT_TOKEN", raising=False)
        app = create_app(
            runtime_settings(twin.store.settings, tmp_path, None), twin, clock=Clock(at(1))
        )
        with pytest.raises(Exception) as caught:
            with TestClient(app):
                pass
        assert "PLANT_TOKEN" in str(caught.value)
        assert getattr(caught.value, "code", "").startswith("config."), caught.value

        # With the add-on off no variable is needed, and nothing is executed.
        document["contract"]["addons"] = {"executors": False}
        document["twin"]["revision"] = 2
        install_twin(
            twin.store.settings, Bundle(manifest=manifest_of(document), ontology_text=text)
        ).twin.close()
        app = create_app(
            runtime_settings(twin.store.settings, tmp_path, None), twin, clock=Clock(at(1))
        )
        with TestClient(app) as client:
            health = client.get("/healthz")
            assert health.status_code == 200
            assert app.state.services.executor is None
    finally:
        twin.close()
