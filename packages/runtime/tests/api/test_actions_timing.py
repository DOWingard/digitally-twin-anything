"""How long the API takes to judge and record a proposal it does not execute, to list a
first page of a thousand records and to list its control loops, measured over HTTP
against limits set before they were measured. One request first warms the route; three
are measured.

Each test prints one `TIMING` line with what it measured (seen with `pytest -s`)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from actions_api_testkit import VALVE, Acting
from actions_testkit import register_rows

MakeActing = Callable[..., Acting]
PROPOSAL_LIMIT_MS = 300.0
RECORDS_LIMIT_MS = 300.0
LOOPS_LIMIT_MS = 100.0
RUNS = 3
RECORDS = 1_000


def report(name: str, took: list[float], limit_ms: float, **more: Any) -> None:
    print(
        "TIMING "
        + json.dumps(
            {"name": name, "runs_ms": [round(value, 1) for value in took], "limit_ms": limit_ms}
            | more
        )
    )


def timed_ms(call: Callable[[], Any]) -> float:
    started = time.perf_counter()
    call()
    return (time.perf_counter() - started) * 1000.0


def test_a_proposal_that_is_not_executed_is_answered_in_time(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)

    def propose(index: int) -> None:
        made = world.proposed("set_valve", VALVE, 10.0 + index)
        assert made["status"] == "proposed"

    propose(0)
    took = [timed_ms(lambda index=index: propose(index)) for index in range(1, RUNS + 1)]
    report("propose, not executed", took, PROPOSAL_LIMIT_MS)
    assert max(took) <= PROPOSAL_LIMIT_MS


def test_a_first_page_of_a_thousand_records_is_listed_in_time(acting: MakeActing):
    world = acting()
    world.into("healthy", 2)
    made = world.proposed("set_valve", VALVE, 20.0)
    stored = world.twin.store.get(made["uid"]).values
    copies = [
        {**stored, "uid": f"twin_action_record:timing{index:04d}"} for index in range(1, RECORDS)
    ]
    register_rows(world.twin, {"TwinActionRecord": copies})

    def first_page() -> None:
        page = world.ok("/actions/records")
        assert page["data"] and page["next_cursor"] is not None

    first_page()
    took = [timed_ms(first_page) for _ in range(RUNS)]
    count = world.twin.store.table("TwinActionRecord").num_rows
    report("first page of the records", took, RECORDS_LIMIT_MS, records=count)
    assert count == RECORDS
    assert max(took) <= RECORDS_LIMIT_MS


def test_the_control_loops_are_listed_in_time(acting: MakeActing):
    world = acting()

    def listed() -> None:
        assert len(world.ok("/control/loops")["data"]) == 3

    listed()
    took = [timed_ms(listed) for _ in range(RUNS)]
    report("control loops", took, LOOPS_LIMIT_MS)
    assert max(took) <= LOOPS_LIMIT_MS
