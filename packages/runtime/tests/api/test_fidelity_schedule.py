"""The schedule the API serves, and the scheduler following it against the real API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from api_testkit import TOKEN
from dta_runtime.api.schedule import ScheduleEntry
from dta_runtime.scheduler import Scheduler
from dta_runtime.settings import SchedulerSettings
from fidelity_api_testkit import BUILT, Checked, at, held, iso

SYNC = {
    "id": "sync",
    "kind": "sync",
    "interval": "PT15M",
    "method": "POST",
    "path": "/api/v1/sync/runs",
    "key": "sync:{boundary}",
    "body": {},
}
CHECK = {
    "id": "fidelity",
    "kind": "fidelity",
    "interval": "PT15M",
    "method": "POST",
    "path": "/api/v1/fidelity/checks",
    "key": "fidelity:{boundary}",
    "body": {},
}


def steady(key: str, step: int) -> float:
    return {"T1": 5.0, "T2": 3.0}[key] + 0.1 * (step % 2)


def test_the_schedule_lists_the_sync_and_the_check_of_a_live_twin(
    checked: Callable[..., Checked], api: Any
) -> None:
    world = checked(evaluate=False)
    document = world.ok("/lifecycle/schedule")
    assert document["now"] == iso(at(BUILT))
    assert document["entries"] == [SYNC, CHECK]
    # A static twin has nothing to be asked for.
    assert api.ok("/api/v1/lifecycle/schedule")["entries"] == []


def nowhere(services: Any, state: Any) -> list[ScheduleEntry]:
    """An entry whose path the API does not have: asked for, refused, and left be."""
    return [
        ScheduleEntry(
            id="nowhere",
            kind="probe",
            interval=timedelta(minutes=15),
            path="/api/v1/nowhere",
            key="nowhere:{boundary}",
        )
    ]


def broken(services: Any, state: Any) -> list[ScheduleEntry]:
    raise RuntimeError("this provider cannot say what is due")


def test_the_scheduler_asks_for_each_entry_once_per_boundary_and_the_check_after_the_sync(
    checked: Callable[..., Checked], tmp_path: Path
) -> None:
    world = checked(levels=steady, synced=True, evaluate=False)
    world.services.schedule.extend([nowhere, broken])
    scheduler = Scheduler(
        SchedulerSettings(api_url=world.url, api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05)
    )
    try:
        # The sync of BUILT ran under the scheduler's key: it is handed back and counts.
        first = scheduler.tick()
        assert first["started"] == ["sync"], first
        # The check is asked for once the sync has ended; the entry the API refuses (404)
        # holds up nothing and is not asked for again before its next boundary.
        second = scheduler.tick()
        assert second["started"] == ["fidelity"] and second["waiting"] == {}, second
        assert [found["at"] for found in world_checks(world, 1)] == [iso(at(BUILT))]
        assert scheduler.tick()["reason"] == "not_due"

        # The next boundary: its sync is held behind a job of the test's in the sync lane.
        rows = [(key, at(BUILT + 1), steady(key, BUILT + 1)) for key in ("T1", "T2")]
        world.scada.append("levels", rows)
        world.clock.set(at(BUILT + 1))
        gate = threading.Event()
        world.services.jobs.submit("probe", {}, held(gate), lane="sync")
        try:
            asked = scheduler.tick()
            assert asked["started"] == ["sync"], asked
            waiting = scheduler.tick()
            assert waiting["started"] is None
            assert waiting["waiting"] == {
                "fidelity": "sync.not_ended",
                "nowhere": "sync.not_ended",
            }
            assert len(world.ok("/fidelity/checks")["data"]) == 1
        finally:
            gate.set()
        sync_job = world.services.jobs.active("sync")
        if sync_job is not None:
            assert sync_job.done.wait(60)
        after = scheduler.tick()
        assert after["started"] == ["fidelity"], after
        assert world_checks(world, 2)[0]["at"] == iso(at(BUILT + 1))
        assert scheduler.tick()["reason"] == "not_due"
    finally:
        scheduler.close()
    # One run per boundary: the BUILT steps the test synced, and the scheduler's one.
    assert len(world.ok("/sync/runs", limit=100)["data"]) == BUILT + 1


def world_checks(world: Checked, count: int) -> list[dict[str, Any]]:
    """The checks once `count` are written, newest first."""
    import time

    deadline = time.monotonic() + 120.0
    while True:
        found = world.ok("/fidelity/checks")["data"]
        if len(found) >= count:
            return found
        assert time.monotonic() < deadline, f"{len(found)} of {count} checks were written"
        time.sleep(0.05)
