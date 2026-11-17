"""The API of a live twin served over HTTP by uvicorn: driven by the real scheduler, and timed.

The thresholds were fixed before anything was measured: `/meta/health`, polled about once a
second, answers within 30 ms at the 95th percentile of 200 requests in a row once its
cache is warm; `POST /sync/runs` answers within 200 ms whatever the run takes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from api_testkit import TOKEN
from dta_runtime.scheduler import NOT_DUE, Scheduler
from dta_runtime.settings import SchedulerSettings
from live_api_testkit import LiveApi, percentile, plant
from live_testkit import at

MakeLive = Callable[..., LiveApi]
HEALTH = "/api/v1/meta/health"
RUNS = "/api/v1/sync/runs"
HEALTH_P95_MS = 30.0
POST_MS = 200.0
REQUESTS = 200


def scheduler_of(live: LiveApi, logs: Path) -> Scheduler:
    settings = SchedulerSettings(
        api_url=str(live.client.base_url).rstrip("/"),
        api_token=TOKEN,
        logs_dir=logs,
        poll_s=1.0,
    )
    return Scheduler(settings)


def test_the_scheduler_starts_one_run_per_interval(
    live_api: MakeLive, plant_dir: Path, tmp_path: Path
):
    sources, folders = plant(plant_dir, "registry", "scada")
    live = live_api(sources, served=True)
    scheduler = scheduler_of(live, tmp_path / "scheduler")
    try:
        # The clock shows 00:16: the interval that ended at 00:15 is due. The API's
        # schedule holds the sync alone: the twin sets no fidelity checks.
        first = scheduler.tick()
        assert first["started"] == ["sync"], first
        (fired,) = first["fired"]
        assert fired["due"] == "2026-01-05T00:15:00+00:00"
        assert live.wait(fired["job"])["state"] == "succeeded"
        idle = scheduler.tick()
        assert (idle["started"], idle["reason"]) == (None, NOT_DUE)
        # A scheduler started again asks for the same interval and is given its job.
        restarted = scheduler_of(live, tmp_path / "scheduler")
        try:
            again = restarted.tick()
        finally:
            restarted.close()
        assert again["started"] == ["sync"] and again["fired"][0]["job"] == fired["job"]

        folders["scada"].append("levels", [("T1", at(20), 3.0)])
        live.clock.set(at(31))
        second = scheduler.tick()
        assert second["started"] == ["sync"], second
        (later,) = second["fired"]
        assert later["due"] == "2026-01-05T00:30:00+00:00" and later["job"] != fired["job"]
        assert live.wait(later["job"])["state"] == "succeeded"
        live.clock.set(at(44))
        assert scheduler.tick()["started"] is None
    finally:
        scheduler.close()

    runs = live.runs()
    assert [run["clock_at"] for run in runs] == ["2026-01-05T00:31:00Z", "2026-01-05T00:16:00Z"]
    assert {run["status"] for run in runs} == {"succeeded"}
    keys = [job["idempotency_key"] for job in live.ok("/api/v1/lifecycle/jobs")["data"]]
    assert keys == ["sync:2026-01-05T00:30:00+00:00", "sync:2026-01-05T00:15:00+00:00"]
    tank = live.ok("/api/v1/object/node:T1")["data"]["values"]
    assert tank["level"] == 3.0


def test_health_answers_within_its_threshold_once_warm(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir)
    live = live_api(sources, served=True)
    live.run({})
    for _ in range(20):
        assert live.get(HEALTH).status_code == 200

    durations = []
    for _ in range(REQUESTS):
        started = time.perf_counter()
        response = live.get(HEALTH)
        durations.append((time.perf_counter() - started) * 1000.0)
        assert response.status_code == 200
    assert response.json()["sync"]["fresh"] is True

    p50, p95 = percentile(durations, 0.5), percentile(durations, 0.95)
    print(
        f"\nGET {HEALTH}, {REQUESTS} in a row over HTTP: p50 {p50:.2f} ms, p95 {p95:.2f} ms, "
        f"max {max(durations):.2f} ms"
    )
    assert p95 < HEALTH_P95_MS


def test_a_post_answers_at_once_whatever_the_run_takes(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir)
    live = live_api(sources, served=True)
    gate = live.plant.hold("lab")
    # What is measured is that a POST does not wait for the run, not what the first
    # request of a process costs (its connection, the first read of the store).
    assert live.get(HEALTH).status_code == 200

    started = time.perf_counter()
    made = live.post(RUNS, json={})
    made_ms = (time.perf_counter() - started) * 1000.0
    assert made.status_code == 202
    assert gate.reached.wait(60)
    started = time.perf_counter()
    same = live.post(RUNS, json={}, headers={"Idempotency-Key": "again"})
    same_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    busy = live.post(RUNS, json={"sources": ["lab"]})
    busy_ms = (time.perf_counter() - started) * 1000.0
    assert (same.status_code, busy.status_code) == (200, 409)
    gate.open()
    assert live.wait(made.json()["job"]["id"])["state"] == "succeeded"

    print(
        f"\nPOST {RUNS} over HTTP while the run is held: new {made_ms:.1f} ms, "
        f"same {same_ms:.1f} ms, busy {busy_ms:.1f} ms"
    )
    assert max(made_ms, same_ms, busy_ms) < POST_MS
