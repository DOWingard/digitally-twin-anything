"""The fidelity check as a job of the API: what it scores and writes, how it is read back,
and when it is refused."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest
from fidelity_api_testkit import BUILT, CHECK_PATH, HOLD, QUESTION, Checked, at, check_key, iso
from live_api_testkit import HEADERS

ARROW = "application/vnd.apache.arrow.stream"


def test_checks_at_successive_boundaries_score_and_the_twin_becomes_healthy(
    checked: Callable[..., Checked],
) -> None:
    world = checked()
    before = world.fidelity()
    assert before["state"] == "unknown" and before["check"] is None

    steps = world.run(BUILT + 1, BUILT + 5)

    first, corrections = steps[BUILT + 1]
    assert corrections == []
    result = first["result"]
    assert first["kind"] == "fidelity" and first["idempotency_key"] == check_key(BUILT + 1)
    assert result["at"] == iso(at(BUILT + 1)) and result["replayed"] is False
    assert (result["state_before"], result["state"]) == ("unknown", "healthy")
    assert result["events"] == ["twin.fidelity.state"]
    assert all(steps[step][0]["result"]["events"] == [] for step in range(BUILT + 2, BUILT + 6))

    found = world.fidelity()
    assert found["state"] == "healthy" and found["since"] == iso(at(BUILT + 1))
    assert found["check"]["at"] == iso(at(BUILT + 5))
    (question,) = found["questions"]
    assert question["id"] == QUESTION and question["model"] == HOLD
    assert question["scored"] is True and question["origin"] == iso(at(BUILT + 3))
    assert question["error"] == pytest.approx(0.0, abs=1e-9)
    assert question["window_n"] >= 3 and question["breach"] is False
    assert question["tolerance"] == 0.5 and question["metric"] == "mae"
    assert found["incidents"] == [] and found["suspect_signals"] == []

    listed = world.ok("/fidelity/checks", limit=2)
    assert [entry["at"] for entry in listed["data"]] == [iso(at(BUILT + 5)), iso(at(BUILT + 4))]
    assert listed["next_cursor"] is not None
    rest = world.ok("/fidelity/checks", limit=10, cursor=listed["next_cursor"])["data"]
    assert [entry["at"] for entry in rest] == [
        iso(at(step)) for step in range(BUILT + 3, BUILT, -1)
    ]
    one = world.ok(f"/fidelity/checks/{found['check']['uid']}")["data"]
    assert one["uid"] == found["check"]["uid"] and one["state"] == "healthy"
    assert one["questions"][QUESTION]["origin"] == iso(at(BUILT + 3))
    assert one["trace_id"] == steps[BUILT + 5][0]["trace_id"]
    missing = world.get("/fidelity/checks/twin_fidelity_check:20200101T000000Z")
    world.refused(missing, 404, "fidelity.not_found")

    errors = world.ok(f"/fidelity/questions/{QUESTION}/errors")
    rows = errors["data"]
    # Origins from the end of the model's data on, each scored once its horizon has passed.
    assert [row["origin"] for row in rows] == [iso(at(step)) for step in range(BUILT, BUILT + 4)]
    assert all(row["error"] == pytest.approx(0.0, abs=1e-9) for row in rows)
    assert all(row["in_window"] and row["model"] == HOLD for row in rows)
    first_check = world.ok("/fidelity/checks", limit=10)["data"][-1]
    assert first_check["questions"][QUESTION]["scored"] is False
    response = world.client.get(
        f"/api/v1/fidelity/questions/{QUESTION}/errors", headers={**HEADERS, "Accept": ARROW}
    )
    assert response.status_code == 200 and response.headers["content-type"] == ARROW
    table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
    assert table.num_rows == len(rows) and "error" in table.column_names
    world.refused(world.get("/fidelity/questions/nothing/errors"), 404, "fidelity.not_found")

    health = world.ok("/meta/health")
    assert health["fidelity"] == "healthy"
    detail = health["fidelity_detail"]
    assert detail["check"]["at"] == iso(at(BUILT + 5)) and detail["since"] == iso(at(BUILT + 1))
    assert detail["incidents"] == {"open": 0, "failed": 0} and detail["suspect_signals"] == 0


def test_a_check_is_made_once_per_boundary(checked: Callable[..., Checked]) -> None:
    world = checked()
    world.write(BUILT + 1)
    first = world.ask(BUILT + 1)
    done = world.wait(first)
    # The same key: the same job.
    again = world.ask(BUILT + 1)
    assert again.status_code == 200 and again.json()["job"]["id"] == done["id"]
    # Another key at the same boundary: a job that answers from the check's record.
    other = world.wait(world.ask(BUILT + 1, key="by-hand-1"))
    assert other["id"] != done["id"]
    assert other["result"]["replayed"] is True and other["result"]["events"] == []
    assert other["result"]["check"] == done["result"]["check"]
    record: dict[str, Any] = other["result"]["record"]
    assert record["uid"] == done["result"]["check"]
    # The clock within the boundary's interval names the same boundary.
    world.clock.set(at(BUILT + 1.5))
    late = world.wait(world.post(CHECK_PATH, headers={"Idempotency-Key": "by-hand-2"}))
    assert late["result"]["at"] == iso(at(BUILT + 1)) and late["result"]["replayed"] is True
    assert len(world.ok("/fidelity/checks")["data"]) == 1


def test_a_check_is_refused_without_a_clock_and_on_a_static_twin(
    checked: Callable[..., Checked], api: Any
) -> None:
    world = checked(evaluate=False)
    world.clock.stopped = True
    refused = world.post(CHECK_PATH, headers={"Idempotency-Key": "k"})
    world.refused(refused, 503, "clock.unavailable")
    world.clock.stopped = False
    # A static twin has no checks.
    static = api.post(f"/api/v1{CHECK_PATH}", headers={"Idempotency-Key": "k"})
    assert static.status_code == 409 and static.json()["code"] == "fidelity.not_live"
    assert api.ok("/api/v1/fidelity")["state"] == "unknown"
