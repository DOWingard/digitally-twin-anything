"""How long the computed twin takes to answer through its served API and its worker.
Run with `-m scale -s` to see what was measured beside the machine's load."""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest
from compute_testkit import World

pytestmark = pytest.mark.scale

LATEST_MS = 150.0
FORECAST_MS = 1500.0
QUESTIONS_MS = 100.0


def median_ms(call: Callable[[], Any], runs: int, what: str, threshold: float) -> float:
    """The median duration of `runs` calls after two that warm the path up."""
    for _ in range(2):
        assert call().status_code == 200
    measured = []
    for _ in range(runs):
        started = time.perf_counter()
        response = call()
        measured.append((time.perf_counter() - started) * 1000.0)
        assert response.status_code == 200, response.text
    found = statistics.median(measured)
    print(
        what,
        {
            "median_ms": round(found, 1),
            "min_ms": round(min(measured), 1),
            "max_ms": round(max(measured), 1),
            "runs": runs,
            "threshold_ms": threshold,
            "load": os.getloadavg(),
        },
        flush=True,
    )
    return found


def test_latest_answers_through_the_route_in_time(world: World, registered: dict[str, Any]) -> None:
    found = median_ms(lambda: world.post("/functions/tank_levels/run"), 20, "latest", LATEST_MS)

    assert found <= LATEST_MS


def test_a_forecast_answers_through_a_warm_worker_in_time(
    world: World, evaluated: dict[str, Any]
) -> None:
    found = median_ms(
        lambda: world.post("/functions/level_forecast/run"), 10, "forecast", FORECAST_MS
    )

    assert found <= FORECAST_MS


def test_the_questions_are_listed_in_time(world: World, registered: dict[str, Any]) -> None:
    found = median_ms(lambda: world.get("/questions"), 20, "questions", QUESTIONS_MS)

    assert found <= QUESTIONS_MS
