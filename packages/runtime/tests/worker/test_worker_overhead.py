"""What the worker costs a run that does nothing, from request to answer.

Thresholds fixed before measuring: a run of own code (a fresh process) at most 500 ms,
median of 10; a run of shipped code in a warm pool process at most 50 ms, median of 20.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from statistics import median
from typing import Any

from worker_testkit import TwinApi, Worker, function_body, predict_body, twin_code

OWN_CODE_MS = 500.0
SHIPPED_MS = 50.0


def _timed(run: Callable[[], Any], times: int) -> list[float]:
    found = []
    for _ in range(times):
        started = time.perf_counter()
        run()
        found.append((time.perf_counter() - started) * 1000.0)
    return found


def test_a_run_of_shipped_code_in_a_warm_process_costs_little(worker: Worker):
    # Every process of the pool has imported the handler and reached the API.
    for _ in range(4 * worker.pools.interactive.size):
        worker.ok(predict_body())
    found = _timed(lambda: worker.ok(predict_body()), 20)
    print(f"shipped code, warm process: median {median(found):.1f} ms of {sorted(found)}")
    assert median(found) <= SHIPPED_MS


def test_a_run_of_own_code_in_a_fresh_process_costs_little(worker: Worker, twin_api: TwinApi):
    head = twin_api.water.head
    code = twin_api.upload(twin_code())
    # The archive is fetched and unpacked once; every run after uses it as it is.
    worker.ok(function_body(code, "twin_code:noop", head.id, branch=head.branch))

    def run() -> None:
        worker.ok(function_body(code, "twin_code:noop", head.id, branch=head.branch))

    found = _timed(run, 10)
    print(f"own code, fresh process: median {median(found):.1f} ms of {sorted(found)}")
    assert median(found) <= OWN_CODE_MS
