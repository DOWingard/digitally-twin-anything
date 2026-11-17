"""How long the fidelity reads and writes take, against thresholds fixed before they were
measured: `GET /fidelity` within 50 ms; a check job on the test twin, its predictor
answering at once, within 2 s; the read of the suspect signals a sync run makes, with
none suspect, within 20 ms. Each is measured three times."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
from fidelity_api_testkit import BUILT, Checked
from live_api_testkit import records

GET_FIDELITY_MS = 50.0
CHECK_JOB_MS = 2000.0
SUSPECTS_READ_MS = 20.0
TIME = pa.timestamp("us", tz="UTC")
LEVELS = {"node:T1": 5.0, "node:T2": 3.0}


def steady(key: str, step: int) -> float:
    return {"T1": 5.0, "T2": 3.0}[key] + 0.1 * (step % 2)


def instant(services: Any, known: Any) -> Callable[..., pa.Table]:
    """A predictor that answers at once: what the fitted model would say."""

    def predict(
        model: Mapping[str, Any],
        *,
        origin: Any,
        horizon: int,
        step: timedelta,
        interventions: Sequence[Any],
        deadline_s: float,
    ) -> pa.Table:
        uids, times, means = [], [], []
        for uid, value in LEVELS.items():
            for ahead in range(1, horizon + 1):
                uids.append(uid)
                times.append(origin.t + ahead * step)
                means.append(value)
        return pa.table(
            {
                "unique_id": pa.array(uids, pa.string()),
                "ds": pa.array(times, TIME),
                "mean": pa.array(means, pa.float64()),
                "lo_80": pa.array([value - 0.25 for value in means], pa.float64()),
                "hi_80": pa.array([value + 0.25 for value in means], pa.float64()),
            }
        )

    return predict


def test_the_fidelity_reads_and_the_check_job_are_within_their_thresholds(
    checked: Callable[..., Checked], log_lines: Path
) -> None:
    world = checked(levels=steady, synced=True)
    world.services.check_predictor = instant
    world.run(BUILT + 1, BUILT + 2)

    jobs = []
    for step in range(BUILT + 3, BUILT + 6):
        job, _ = world.step(step)
        assert job["result"]["state"] == "healthy"
        jobs.append(float(job["duration_ms"]))

    world.fidelity()
    reads = []
    for _ in range(3):
        started = time.perf_counter()
        world.fidelity()
        reads.append(round((time.perf_counter() - started) * 1000.0, 1))

    suspects = [
        float(line["data"]["elapsed_ms"])
        for line in records(log_lines)
        if line["message"] == "suspect signals read"
    ][-3:]
    measured = {"get_fidelity_ms": reads, "check_job_ms": jobs, "suspects_read_ms": suspects}
    print("TIMINGS " + json.dumps(measured))
    assert len(suspects) == 3, measured
    assert max(reads) <= GET_FIDELITY_MS, measured
    assert max(jobs) <= CHECK_JOB_MS, measured
    assert max(suspects) <= SUSPECTS_READ_MS, measured
