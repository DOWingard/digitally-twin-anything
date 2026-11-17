"""Corrections that do not come to a better model: one the worker refuses, one that runs
out of time, and one that dies with the API's process. Each reports back, or is started
again under its key, and the models stay as they were."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from dta_runtime import fidelity
from fidelity_api_testkit import (
    BUILT,
    HOLD,
    OPENS,
    Checked,
    at,
    breach,
    held_check,
    shift,
    story,
)


def test_a_correction_the_worker_refuses_reports_back_and_changes_no_model(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=shift(0.55))
    world.run(BUILT + 1, OPENS - 1)
    checked_job, gate = held_check(world)
    (asked,) = checked_job["result"]["corrections"]
    world.stop_worker()
    gate.set()
    job = world.job(asked["job"])

    assert job["state"] == "succeeded", job
    result = job["result"]
    assert result["accepted"] is False and result["reason_code"] == "worker_refused", result
    assert result["reported"] is True and result["key"] == asked["key"]
    incident = breach(world)
    assert incident["detail"].get("pending") is None
    last = incident["steps"][-1]
    assert last["action"] == "result" and last.get("accepted") is False, incident["steps"]
    model = world.models()[HOLD]
    assert model["status"] == "active" and model["fitted_to"] == at(BUILT)


def test_a_correction_that_runs_out_of_time_reports_back_and_changes_no_model(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=shift(0.55), correction_budget_s=0.5)
    steps = world.run(BUILT + 1, OPENS)

    (job,) = steps[OPENS][1]
    assert job["state"] == "succeeded", story(steps)
    result = job["result"]
    assert result["accepted"] is False and result["reported"] is True, result
    assert result["reason_code"] == "timeout", result
    assert "time" in (result["reason"] or ""), result
    model = world.models()[HOLD]
    assert model["status"] == "active" and model["fitted_to"] == at(BUILT)
    assert breach(world)["detail"].get("pending") is None


def test_a_correction_that_died_with_the_process_is_started_again_by_the_next_check(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=shift(0.55))
    world.run(BUILT + 1, OPENS - 1)
    checked_job, gate = held_check(world)
    (asked,) = checked_job["result"]["corrections"]
    jobs = world.services.jobs

    # The process ends: the queued correction is lost with it.
    stopping = threading.Thread(target=world.stop_api)
    stopping.start()
    deadline = time.monotonic() + 30.0
    while jobs.get(asked["job"])["state"] != "failed":
        assert time.monotonic() < deadline, "the queued correction was not cancelled"
        time.sleep(0.02)
    gate.set()
    stopping.join(timeout=60.0)
    assert not stopping.is_alive()
    # The incident still waits for it: read past the API, which is not there.
    (waiting,) = fidelity.incidents(world.twin, kind="breach")
    assert waiting["detail"]["pending"]["key"] == asked["key"]

    world.start_api()
    checked_again, corrections = world.step(OPENS + 1)
    (again,) = checked_again["result"]["corrections"]
    assert again["key"] == asked["key"] and again["created"] is True
    (job,) = corrections
    assert job["idempotency_key"] == asked["key"] and job["state"] == "succeeded"
    assert job["result"]["accepted"] is True and job["result"]["reported"] is True
    incident = breach(world)
    assert incident["detail"].get("pending") is None
    assert world.models()[HOLD]["fitted_to"] == at(OPENS + 1)
