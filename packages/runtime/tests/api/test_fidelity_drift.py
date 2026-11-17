"""Drift, end to end and with no human: the twin is driven only by asking for checks at
successive boundaries, as the scheduler would, and finds, corrects or gives up on its
own."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fidelity_api_testkit import (
    BUILT,
    HOLD,
    QUESTION,
    SPEC,
    TOLERANCE,
    WEBHOOK_ENV,
    Checked,
    alternating,
    at,
    iso,
    shift,
    story,
)
from webhook_testkit import webhook

SEASONAL = "twin_model:level/seasonal"


def actions(incident: dict[str, Any]) -> list[str]:
    return [step["action"] for step in incident["steps"]]


def breach_of(world: Checked) -> dict[str, Any]:
    (found,) = world.incidents(kind="breach")
    return world.ok(f"/fidelity/incidents/{found['uid']}")["data"]


def test_a_level_shift_is_found_relearned_by_recalibration_and_resolved(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=shift(0.55))
    steps = world.run(BUILT + 1, BUILT + 8)
    told = story(steps)

    opened_at = next(step for step, (job, _) in steps.items() if job["result"]["opened"])
    assert opened_at == BUILT + 4, told
    job, corrections = steps[opened_at]
    assert job["result"]["state"] == "correcting"
    (correction,) = corrections
    assert correction["kind"] == "correction" and correction["state"] == "succeeded"
    result = correction["result"]
    assert result["rung"] == "recalibrate" and result["accepted"] is True, result
    assert result["model"] == HOLD and result["fitted_to"] == iso(at(opened_at))
    assert result["reported"] is True
    # It re-fitted on the recent frame: what the backtest walked is the last window.
    frame = result["frame"]
    assert frame["to"] == iso(at(opened_at))

    incident = breach_of(world)
    assert incident["status"] == "resolved" and incident["subject"] == QUESTION
    assert actions(incident) == ["opened", "request", "result", "held"], told
    assert world.fidelity()["state"] == "healthy"
    model = world.models()[HOLD]
    assert model["status"] == "active" and model["fitted_to"] == at(opened_at)
    # Origins after the correction are within the tolerance.
    rows = world.ok(f"/fidelity/questions/{QUESTION}/errors")["data"]
    after = [row for row in rows if row["origin"] >= iso(at(opened_at))]
    assert len(after) >= 3 and all(row["error"] <= TOLERANCE for row in after)
    # The backtest of the correction says it was the recent evaluation.
    backtests = world.store_rows("TwinBacktest")
    recent = [row for row in backtests.values() if json.loads(row["scores"]).get("scope")]
    assert recent and all(json.loads(row["scores"])["scope"] == "recent" for row in recent)
    question = world.store_rows("TwinQuestion")[f"twin_question:{QUESTION}"]
    assert question["model"] == HOLD and question["status"] == "validated"


def register_seasonal(world: Checked) -> None:
    """A candidate that repeats the value of two steps before: what alternates, it learns."""
    response = world.post(
        "/models",
        json={
            "spec": SPEC,
            "candidate": "seasonal",
            "family": "seasonal_naive",
            "params": {"season_length": 2},
            "target": "Tank.level",
            "question": QUESTION,
        },
    )
    assert response.status_code == 201, response.text


def test_a_change_the_active_family_cannot_learn_is_corrected_by_another_candidate(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=alternating(0.55))
    register_seasonal(world)
    steps = world.run(BUILT + 1, BUILT + 16)
    told = story(steps)

    incident = breach_of(world)
    assert incident["status"] == "resolved", told
    requested = [step for step in incident["steps"] if step["action"] == "request"]
    assert [step.get("rung") for step in requested][-1] == "reselect", incident["steps"]
    models = world.models()
    assert models[SEASONAL]["status"] == "active", told
    assert models[HOLD]["status"] != "active"
    question = world.store_rows("TwinQuestion")[f"twin_question:{QUESTION}"]
    assert question["model"] == SEASONAL
    reselected = next(
        found["result"]
        for _, corrections in steps.values()
        for found in corrections
        if found["result"]["rung"] == "reselect"
    )
    assert reselected["accepted"] is True and reselected["model"] == SEASONAL
    rows = world.ok(f"/fidelity/questions/{QUESTION}/errors")["data"]
    after = [row for row in rows if row["model"] == SEASONAL]
    assert len(after) >= 3 and all(row["error"] <= TOLERANCE for row in after)
    assert world.fidelity()["state"] == "healthy"


def test_a_change_no_candidate_can_learn_asks_for_a_remodel_then_fails(
    checked: Callable[..., Checked], monkeypatch: pytest.MonkeyPatch, log_lines: Path
) -> None:
    with webhook() as receiver:
        monkeypatch.setenv(WEBHOOK_ENV, receiver.url)
        world = checked(
            drift=alternating(0.55),
            notification={"kind": "webhook", "webhook_env": WEBHOOK_ENV},
        )
        steps = world.run(BUILT + 1, BUILT + 16)
        told = story(steps)

        incident = breach_of(world)
        assert incident["status"] == "failed", told
        assert incident["requested_at"] is not None
        health = world.ok("/meta/health")
        assert health["fidelity"] == "failed"
        assert health["fidelity_detail"]["incidents"] == {"open": 0, "failed": 1}
        assert world.fidelity()["state"] == "failed"

        listing = world.ok("/lifecycle/notifications")
        assert listing["kind"] == "webhook"
        sent = list(reversed(listing["data"]))
        received = receiver.wait_for(len(sent))
        assert [event["id"] for event in received] == [found["id"] for found in sent]
        assert all(found["status"] == "delivered" for found in sent)

    kinds = [
        (event["type"], event["data"].get("action") or event["data"].get("to"))
        for event in received
    ]
    assert kinds[0] == ("twin.fidelity.state", "healthy"), kinds
    assert kinds[1:3] == [
        ("twin.fidelity.incident", "opened"),
        ("twin.fidelity.state", "correcting"),
    ], kinds
    remodel = kinds.index(("twin.fidelity.remodel_requested", None))
    assert kinds[remodel - 1] == ("twin.fidelity.incident", "changed")
    assert kinds[-2:] == [
        ("twin.fidelity.incident", "changed"),
        ("twin.fidelity.state", "failed"),
    ], kinds
    statuses = [
        event["data"]["status"] for event in received if event["type"] == "twin.fidelity.incident"
    ]
    assert statuses[0] == "open" and statuses[-1] == "failed"
    assert "remodel_requested" in statuses
    # Every event's time is the data time it happened at, in order.
    times = [event["time"] for event in received]
    assert times == sorted(times)
    # The webhook's address is a secret: no log line holds it.
    logged = log_lines.read_text(encoding="utf-8")
    assert "notification delivered" in logged
    assert receiver.url not in logged and f"127.0.0.1:{receiver.port}" not in logged
