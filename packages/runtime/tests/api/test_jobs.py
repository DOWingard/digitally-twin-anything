"""Background jobs: listed and read through `/lifecycle/jobs`, run one at a time, the last 200
kept, and stopped with the API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from dta_core.errors import DtaError
from dta_runtime.api.jobs import JobRunner, JobsClosed, Outcome, Work
from live_api_testkit import LiveApi, plant, problem_code, records, wait_for_job

MakeLive = Callable[..., LiveApi]
JOBS = "/api/v1/lifecycle/jobs"
LEAK = "row 17 held s3cr3t-17"


def quick(n: int) -> Work:
    return lambda: Outcome(result={"n": n})


def held(gate: threading.Event) -> Work:
    def work() -> Outcome:
        assert gate.wait(60), "the test never let the job go"
        return Outcome(result={"held": True})

    return work


def failing(error: Exception) -> Work:
    def work() -> Outcome:
        raise error

    return work


def finished(runner: JobRunner, job_id: str) -> dict:
    return wait_for_job(lambda: runner.get(job_id))


def until_running(runner: JobRunner, job_id: str) -> None:
    pause = threading.Event()
    for _ in range(3000):
        if runner.get(job_id)["state"] == "running":
            return
        pause.wait(0.01)
    raise AssertionError("the job never started")


def test_jobs_are_listed_newest_first_and_filtered(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry", "scada")
    live = live_api(sources)
    sync = live.run({})
    runner = live.services.jobs
    broken = runner.submit("probe", {"n": 1}, failing(DtaError("no", code="probe.refused")))
    fine = runner.submit("probe", {"n": 2}, quick(2))
    finished(runner, fine.id)

    listed = live.ok(JOBS)["data"]

    assert [job["id"] for job in listed] == [fine.id, broken.id, sync["id"]]
    assert [job["sequence"] for job in listed] == sorted(
        (job["sequence"] for job in listed), reverse=True
    )
    assert listed[2] == sync
    assert [job["id"] for job in live.ok(JOBS, params={"kind": "sync"})["data"]] == [sync["id"]]
    failed = live.ok(JOBS, params={"state": "failed"})["data"]
    assert [(job["id"], job["error"]) for job in failed] == [
        (broken.id, {"code": "probe.refused", "message": "no"})
    ]
    both = live.ok(JOBS, params={"kind": "probe", "state": "succeeded"})["data"]
    assert [(job["id"], job["result"]) for job in both] == [(fine.id, {"n": 2})]
    assert [job["id"] for job in live.ok(JOBS, params={"limit": 1})["data"]] == [fine.id]
    assert live.ok(JOBS, params={"state": "running"})["data"] == []

    one = live.ok(f"{JOBS}/{fine.id}")
    assert one == {"data": listed[0]}
    missing = live.get(f"{JOBS}/{'0' * 32}")
    assert (missing.status_code, problem_code(missing)) == (404, "job.not_found")
    for params in ({"limit": 0}, {"limit": 201}, {"state": "lost"}):
        assert live.get(JOBS, params=params).status_code == 400, params


def test_past_200_jobs_the_oldest_finished_ones_are_dropped(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry")
    live = live_api(sources)
    runner = live.services.jobs
    gate = threading.Event()
    blocker = runner.submit("probe", {"held": True}, held(gate))
    queued = [runner.submit("probe", {"n": n}, quick(n)) for n in range(205)]

    # Nothing has finished: every job is kept, beyond 200.
    assert runner.get(blocker.id)["state"] == "running"
    assert all(runner.get(job.id) is not None for job in queued)

    gate.set()
    finished(runner, queued[-1].id)

    listed = live.ok(JOBS, params={"limit": 200})["data"]
    assert len(listed) == 200
    assert [job["id"] for job in listed] == [job.id for job in reversed(queued[5:])]
    for dropped in [blocker, *queued[:5]]:
        response = live.get(f"{JOBS}/{dropped.id}")
        assert (response.status_code, problem_code(response)) == (404, "job.not_found")
    assert live.ok(f"{JOBS}/{queued[5].id}")["data"]["result"] == {"n": 5}


def test_what_goes_wrong_in_a_job_is_kept_as_a_code_and_a_message(log_lines: Path):
    runner = JobRunner()
    runner.start()
    try:
        known = runner.submit("probe", {}, failing(DtaError("the store refused", code="store.x")))
        odd = runner.submit("probe", {}, failing(ValueError(LEAK)))
        told = runner.submit(
            "probe", {}, lambda: Outcome(result={"r": 1}, error={"code": "c", "message": "m"})
        )
        assert finished(runner, known.id)["error"] == {
            "code": "store.x",
            "message": "the store refused",
        }
        unexpected = finished(runner, odd.id)
        assert (unexpected["state"], unexpected["result"]) == ("failed", None)
        assert unexpected["error"] == {
            "code": "job.unexpected",
            "message": "the job failed unexpectedly: ValueError",
        }
        reported = finished(runner, told.id)
        assert (reported["state"], reported["result"], reported["error"]) == (
            "failed",
            {"r": 1},
            {"code": "c", "message": "m"},
        )
    finally:
        runner.shutdown()

    lines = records(log_lines)
    by_message = {line["message"]: line for line in lines if line["context"] == "api.jobs"}
    assert by_message["job failed"]["level"] == "error"
    assert "Traceback" in by_message["job failed"]["stack"]
    assert by_message["job failed unexpectedly"]["level"] == "error"
    assert "raise error" in by_message["job failed unexpectedly"]["data"]["frames"]
    assert by_message["job failed: its work reported a failure"]["level"] == "warn"
    # The text of an unexpected error is only ever logged at debug.
    assert all(line["level"] == "debug" for line in lines if LEAK in str(line))
    assert any(LEAK in str(line) for line in lines)


def test_stopping_cancels_what_is_queued_and_waits_for_the_job_in_progress(log_lines: Path):
    runner = JobRunner(shutdown_wait_s=30)
    runner.start()
    gate = threading.Event()
    running = runner.submit("probe", {}, held(gate))
    until_running(runner, running.id)
    waiting = [runner.submit("probe", {"n": n}, quick(n)) for n in range(3)]
    threading.Timer(0.3, gate.set).start()

    runner.shutdown()

    assert runner.get(running.id)["state"] == "succeeded"
    for job in waiting:
        found = runner.get(job.id)
        assert (found["state"], found["error"]["code"]) == ("failed", "job.cancelled")
    with pytest.raises(JobsClosed) as refused:
        runner.submit("probe", {}, quick(9))
    assert refused.value.code == "job.closed"
    messages = [line["message"] for line in records(log_lines)]
    assert "queued jobs cancelled: the API is stopping" in messages
    assert "gave up waiting for the job in progress; it goes on in its thread" not in messages


def test_a_job_that_outlasts_the_wait_is_left_to_its_thread(log_lines: Path):
    runner = JobRunner(shutdown_wait_s=0.2)
    runner.start()
    gate = threading.Event()
    running = runner.submit("probe", {}, held(gate))
    try:
        until_running(runner, running.id)
        runner.shutdown()
        gave_up = [
            line for line in records(log_lines) if line["message"].startswith("gave up waiting")
        ]
        assert len(gave_up) == 1 and gave_up[0]["level"] == "warn"
        assert runner.get(running.id)["state"] == "running"
    finally:
        gate.set()
    assert finished(runner, running.id)["state"] == "succeeded"
