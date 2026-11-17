"""The limits the worker enforces from outside the code that runs: budget, memory,
failures of every kind, and the pools' capacity."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dta_runtime.worker.limits import oom_kills
from worker_testkit import (
    TwinApi,
    Worker,
    fit_body,
    function_body,
    predict_body,
    problem_of,
    twin_code,
    wait_until,
)

GIB = 1024**3


@pytest.fixture(scope="module")
def code(twin_api: TwinApi) -> str:
    return twin_api.upload(twin_code())


def own(twin_api: TwinApi, code: str, entry: str, budget_s: float = 30, **args: object) -> dict:
    head = twin_api.water.head
    return function_body(
        code, f"twin_code:{entry}", head.id, branch=head.branch, budget_s=budget_s, args=args
    )


def pids(worker: Worker, body_of: object, count: int) -> set[int]:
    return {int(worker.ok(body_of())["notes"][0]) for _ in range(count)}  # type: ignore[operator]


def test_a_pool_run_past_its_budget_is_stopped_and_its_process_replaced(worker: Worker):
    before = pids(worker, lambda: predict_body("pid"), 6)
    started = time.monotonic()
    document = problem_of(
        worker.run(predict_body("sleep", budget_s=1, s=30)), 504, "function.timeout"
    )
    took = time.monotonic() - started
    assert took < 1 + 2, took
    assert "budget" in document["detail"]
    # The next run in the pool succeeds, and the pool has its processes again, one new.
    assert worker.ok(predict_body())["series"] == 1
    pool = worker.pools.interactive
    wait_until(lambda: pool._idle.qsize() == pool.size, what="the replacement process")
    after = pids(worker, lambda: predict_body("pid"), 6)
    assert len(after - before) == 1 and len(after) == pool.size


def test_a_run_of_own_code_past_its_budget_is_stopped(worker: Worker, twin_api: TwinApi, code: str):
    started = time.monotonic()
    response = worker.run(own(twin_api, code, "sleepy", budget_s=1, s=30))
    took = time.monotonic() - started
    problem_of(response, 504, "function.timeout")
    assert took < 1 + 2, took
    assert worker.ok(own(twin_api, code, "noop"))["value"] is None


def test_a_pool_run_past_its_memory_is_answered_and_the_next_succeeds(worker: Worker):
    limit = worker.pools.memory_bytes
    assert limit == GIB
    document = problem_of(
        worker.run(predict_body("allocate", bytes=2 * limit)), 500, "function.memory"
    )
    assert "1024 MiB" in document["detail"]
    assert worker.ok(predict_body("allocate", bytes=64 * 1024 * 1024))["notes"] == [
        str(64 * 1024 * 1024)
    ]


def test_a_run_of_own_code_past_its_memory_is_answered_and_the_next_succeeds(
    worker: Worker, twin_api: TwinApi, code: str
):
    problem_of(worker.run(own(twin_api, code, "hungry", size=2 * GIB)), 500, "function.memory")
    assert worker.ok(own(twin_api, code, "hungry", size=1024))["value"] == 1024


def test_code_that_raises_is_answered_with_its_type_message_and_frames(
    worker: Worker, twin_api: TwinApi, code: str
):
    document = problem_of(worker.run(predict_body("raise")), 500, "function.failed")
    assert document["detail"] == "ValueError: the pump is on fire"
    assert "worker_handlers.py" in document["frames"][-1]
    assert 'raise ValueError("the pump is on fire")' in document["frames"][-1]
    assert not any("dta_runtime/worker/run.py" in frame for frame in document["frames"])
    document = problem_of(worker.run(own(twin_api, code, "broken")), 500, "function.failed")
    assert document["detail"] == "RuntimeError: valve stuck"
    assert "twin_code.py" in document["frames"][-1] and "broken" in document["frames"][-1]
    # Nothing else of the process.
    assert set(document) == {"type", "title", "status", "code", "detail", "trace_id", "frames"}


def test_a_process_that_dies_is_answered_with_what_is_known(
    worker: Worker, twin_api: TwinApi, code: str
):
    document = problem_of(worker.run(predict_body("die", signal="SIGSEGV")), 500, "function.failed")
    assert "SIGSEGV" in document["detail"] and document["frames"] == []
    assert worker.ok(predict_body())["series"] == 1
    document = problem_of(worker.run(predict_body("exit")), 500, "function.failed")
    assert document["detail"] == "SystemExit: 3"
    assert worker.ok(predict_body())["series"] == 1
    # Out of CPU time: the kernel's SIGXCPU.
    problem_of(worker.run(own(twin_api, code, "die", name="SIGXCPU")), 504, "function.timeout")
    # A SIGKILL the worker did not send is the kernel's for memory, unless the cgroup
    # says the kernel killed nothing for memory.
    killed = worker.run(own(twin_api, code, "die", name="SIGKILL"))
    if oom_kills() is None:
        problem_of(killed, 500, "function.memory")
    else:
        assert "SIGKILL" in problem_of(killed, 500, "function.failed")["detail"]


def test_a_handler_that_returns_no_result_fails(start_worker: object):
    handlers = {
        "function": "worker_handlers:function",
        "fit": "worker_handlers:fit",
        "backtest": "worker_handlers:fit",
        "predict": "worker_handlers:not_a_result",
    }
    worker = start_worker(handlers=handlers, cpus=1, memory_mb=2048)  # type: ignore[operator]
    document = problem_of(worker.run(predict_body()), 500, "function.failed")
    assert "dict, not a result" in document["detail"]


def test_the_batch_pool_does_not_hold_up_interactive_runs_and_a_full_pool_says_so(
    worker: Worker, tmp_path: Path
):
    reached, release = tmp_path / "reached", tmp_path / "release"
    batch = worker.pools.batch
    with ThreadPoolExecutor(max_workers=2) as running:
        held = running.submit(
            worker.run, fit_body("hold", reached=str(reached), release=str(release))
        )
        try:
            wait_until(reached.exists, what="the held batch run")
            queued = running.submit(worker.run, fit_body())
            wait_until(lambda: batch.waiting == 1, what="a batch run waiting")
            # The batch pool has its one process busy and one run waiting: it is full.
            started = time.monotonic()
            refused = worker.run(fit_body())
            assert time.monotonic() - started < 1.0
            document = problem_of(refused, 503, "worker.busy")
            assert int(refused.headers["retry-after"]) >= 1
            assert "batch" in document["detail"]
            # Interactive runs are answered all the while.
            for _ in range(3):
                assert worker.ok(predict_body())["series"] == 1
            assert not held.done() and not queued.done()
        finally:
            release.touch()
        assert held.result().status_code == 200
        assert queued.result().status_code == 200
    assert batch.running == 0 and batch.waiting == 0


def test_a_full_interactive_pool_says_when_to_come_back(worker: Worker, tmp_path: Path):
    pool = worker.pools.interactive
    releases = []
    with ThreadPoolExecutor(max_workers=2 * pool.size) as running:
        held = []
        try:
            for number in range(pool.size):
                reached, release = tmp_path / f"reached{number}", tmp_path / f"release{number}"
                releases.append(release)
                held.append(
                    running.submit(
                        worker.run,
                        predict_body(
                            "hold", budget_s=20, reached=str(reached), release=str(release)
                        ),
                    )
                )
                wait_until(reached.exists, what="a held interactive run")
            waiting = [running.submit(worker.run, predict_body()) for _ in range(pool.size)]
            wait_until(lambda: pool.waiting == pool.size, what="interactive runs waiting")
            refused = worker.run(predict_body())
            problem_of(refused, 503, "worker.busy")
            # The held runs end within their budget of 20 s: so much at most to wait.
            assert 1 <= int(refused.headers["retry-after"]) <= 20
        finally:
            for release in releases:
                release.touch()
        assert all(found.result().status_code == 200 for found in held + waiting)
