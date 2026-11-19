"""What an operator does: a correction by hand, a retry, a resolve, a signal cleared; and
what the sync does with a suspect signal and tells of a failed run."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dta_core.sources import SourceUnavailable
from fidelity_api_testkit import (
    BUILT,
    HOLD,
    OPENS,
    QUESTION,
    Checked,
    alternating,
    at,
    breach,
    held_check,
    iso,
    shift,
    story,
)
from live_api_testkit import LiveApi, plant, records

CORRECTIONS = "/fidelity/corrections"
T1_LEVEL = "Tank.level"


def test_a_correction_by_hand_refits_the_active_model_and_touches_no_incident(
    checked: Callable[..., Checked],
) -> None:
    world = checked()
    world.run(BUILT + 1, BUILT + 2)
    body = {"question": QUESTION, "rung": "recalibrate"}
    response = world.post(CORRECTIONS, json=body, headers={"Idempotency-Key": "hand-1"})
    assert response.status_code == 202, response.text
    job = world.done(response)
    assert job["accepted"] is True and job["model"] == HOLD
    assert job["fitted_to"] == iso(at(BUILT + 2)) and job["reported"] is True
    assert job["incident_status"] is None
    again = world.post(CORRECTIONS, json=body, headers={"Idempotency-Key": "hand-1"})
    assert again.status_code == 200
    assert again.json()["job"]["id"] == response.json()["job"]["id"]
    assert world.models()[HOLD]["fitted_to"] == at(BUILT + 2)
    assert world.incidents() == []
    assert world.fidelity()["state"] == "healthy"

    nothing = world.post(CORRECTIONS, json={"question": "nothing", "rung": "recalibrate"})
    world.refused(nothing, 404, "fidelity.not_found")
    wrong = world.post(CORRECTIONS, json={"question": QUESTION, "rung": "remodel"})
    world.refused(wrong, 400, "api.bad_request")


def test_the_correction_an_incident_waits_for_asked_for_by_hand_is_that_one(
    checked: Callable[..., Checked],
) -> None:
    world = checked(drift=shift(0.55))
    world.run(BUILT + 1, OPENS - 1)
    checked_job, gate = held_check(world)
    (asked,) = checked_job["result"]["corrections"]
    try:
        other = world.post(
            CORRECTIONS,
            json={"question": QUESTION, "rung": "reselect"},
            headers={"Idempotency-Key": asked["key"]},
        )
        world.refused(other, 409, "fidelity.conflict")
        same = world.post(
            CORRECTIONS,
            json={"question": QUESTION, "rung": "recalibrate"},
            headers={"Idempotency-Key": asked["key"]},
        )
        assert same.status_code == 200, same.text
        assert same.json()["job"]["id"] == asked["job"]
    finally:
        gate.set()
    assert world.job(asked["job"])["result"]["accepted"] is True


def test_an_operator_retries_a_failed_incident(
    checked: Callable[..., Checked],
) -> None:
    # Neither another candidate nor a re-model may be tried: one cycle at rung 1, then failed.
    world = checked(drift=alternating(0.55), correction={"reselect": False, "agent_remodel": False})
    steps = world.run(BUILT + 1, BUILT + 10)
    incident = breach(world)
    assert incident["status"] == "failed", story(steps)
    uid = incident["uid"]

    world.refused(world.post(f"/fidelity/incidents/{uid}/retry", json={}), 400, "api.bad_request")
    missing = world.post("/fidelity/incidents/twin_incident:none/retry", json={"by": "ops"})
    world.refused(missing, 404, "fidelity.not_found")
    retried = world.post(
        f"/fidelity/incidents/{uid}/retry", json={"by": "ops", "note": "valve serviced"}
    )
    assert retried.status_code == 200, retried.text
    document = retried.json()
    assert document["data"]["status"] == "open" and document["data"]["cycle"] == 1
    assert document["data"]["steps"][-1]["action"] == "retry"
    assert document["data"]["steps"][-1]["by"] == "ops"
    assert document["version"] is not None
    assert [event["type"] for event in document["events"]] == [
        "twin.fidelity.incident",
        "twin.fidelity.state",
    ]
    assert world.ok("/meta/health")["fidelity"] == "correcting"

    # The next check asks for rung 1 again, under a key not used before.
    checked_again, corrections = world.step(BUILT + 11)
    (asked,) = checked_again["result"]["corrections"]
    assert asked["rung"] == "recalibrate"
    used = {step.get("key") for step in incident["steps"] if step.get("key")}
    assert asked["key"] not in used

    # A breach is not resolved by hand: it resolves when the model holds again.
    refused = world.post(f"/fidelity/incidents/{uid}/resolve", json={"by": "ops"})
    world.refused(refused, 400, "fidelity.bad_request")
    # What the retry told is among what the notifier sent, after the failure.
    notified = [
        (found["type"], (found["data"] or {}).get("to") or (found["data"] or {}).get("status"))
        for found in reversed(world.ok("/lifecycle/notifications")["data"])
    ]
    failed = notified.index(("twin.fidelity.state", "failed"))
    assert ("twin.fidelity.state", "correcting") in notified[failed + 1 :], notified


# Both tanks stick from step 25 (T1 at 5.3, T2 at 3.3) and read anew once repaired.
STUCK = {"T1": 5.0, "T2": 3.0}
REPAIRED = BUILT + 9


def stuck(key: str, step: int) -> float:
    if step <= BUILT:
        return STUCK[key] + 0.1 * (step % 2)
    return STUCK[key] + (0.3 if step < REPAIRED else 0.4)


def test_stuck_signals_are_flagged_withheld_by_sync_and_written_again_once_cleared(
    checked: Callable[..., Checked], log_lines: Path
) -> None:
    world = checked(levels=stuck, synced=True, window_steps=24, reads=[T1_LEVEL])
    steps = world.run(BUILT + 1, BUILT + 6)
    told = story(steps)
    # Changed in each of the 24 intervals up to step 25, never otherwise: p0 = 1/26, and
    # (1/26)^5 = 8.4e-8 is the first run below 1e-7.
    flagged_at = next(step for step, (job, _) in steps.items() if job["result"]["flagged"])
    assert flagged_at == BUILT + 6, told

    signals = {
        found["object"]: found for found in world.ok("/fidelity/signals", status="suspect")["data"]
    }
    assert sorted(signals) == ["node:T1", "node:T2"], told
    # The hold model, code of the twin's own, declares it reads the levels: it forecasts
    # them once, from the last origin at which no level was frozen, before the run that
    # began at BUILT + 1. Its horizon of 2 steps has passed by the flag: the values are
    # unknown at once, and what reads them is refused, naming the signals.
    for key, found in signals.items():
        assert (found["property"], found["reason"]) == (T1_LEVEL, "stuck")
        assert found["value_at_flag"] == STUCK[key.split(":")[1]] + 0.3
        assert (found["value"], found["unknown"]["why"]) == ("unknown", "horizon_passed")
        assert found["unknown"]["questions"] == [QUESTION]
        assert found["detail"]["inference"]["chosen"]["why"] == "reads_it_longest_horizon"
        assert found["detail"]["test"]["n"] == 5
    events = steps[flagged_at][0]["result"]["events"]
    assert events.count("twin.fidelity.signal_unknown") == 2, events
    briefs = world.fidelity()["suspect_signals"]
    assert {brief["uid"] for brief in briefs} == {found["uid"] for found in signals.values()}
    assert {brief["value"] for brief in briefs} == {"unknown"}
    opened = {found["subject"]: found for found in world.incidents(kind="signal")}
    assert set(opened) == {found["uid"] for found in signals.values()}
    assert world.ok("/meta/health")["fidelity_detail"]["suspect_signals"] == 2
    tanks = world.store_rows("Tank")
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (None, None)
    problem = world.refused(
        world.post(f"/models/{HOLD}/predict", json={"horizon": 2}), 409, "fidelity.input_unknown"
    )
    assert sorted(found["object"] for found in problem["signals"]) == ["node:T1", "node:T2"]
    assert {found["why"] for found in problem["signals"]} == {"horizon_passed"}

    # The next run leaves the readings out of the graph and keeps them on the signals.
    world.step(BUILT + 7)
    kept = {
        found["object"]: found for found in world.ok("/fidelity/signals", status="suspect")["data"]
    }
    assert (kept["node:T1"]["reading"], kept["node:T1"]["reading_at"]) == (
        5.3,
        iso(at(BUILT + 7)),
    )
    assert kept["node:T2"]["reading"] == 3.3
    tanks = world.store_rows("Tank")
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (None, None)
    world.step(BUILT + 8)
    assert world.fidelity()["state"] == "correcting"

    # One signal cleared, the other's incident resolved, which clears its signal with it.
    first, second = signals["node:T1"], signals["node:T2"]
    cleared = world.post(
        f"/fidelity/signals/{first['uid']}/clear", json={"by": "ops", "note": "sensor replaced"}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["status"] == "ok" and cleared.json()["version"] is not None
    again = world.post(f"/fidelity/signals/{first['uid']}/clear", json={"by": "ops"})
    world.refused(again, 409, "fidelity.conflict")
    incident = opened[second["uid"]]["uid"]
    resolved = world.post(
        f"/fidelity/incidents/{incident}/resolve", json={"by": "ops", "note": "cable fixed"}
    )
    assert resolved.status_code == 200, resolved.text
    document = resolved.json()
    assert document["data"]["status"] == "resolved"
    assert document["data"]["steps"][-1]["action"] == "resolve"
    assert document["data"]["steps"][-1]["by"] == "ops"
    assert "twin.fidelity.incident" in [event["type"] for event in document["events"]]
    world.refused(
        world.post(f"/fidelity/incidents/{incident}/resolve", json={"by": "ops"}),
        409,
        "fidelity.conflict",
    )
    assert world.incidents(kind="signal", status="unresolved") == []
    assert world.ok("/fidelity/signals", status="suspect")["data"] == []
    assert world.fidelity()["state"] == "healthy"

    # Cleared, their readings are written again.
    world.step(REPAIRED)
    tanks = world.store_rows("Tank")
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (5.4, 3.4)

    reads = [
        line["data"]["elapsed_ms"]
        for line in records(log_lines)
        if line["message"] == "suspect signals read"
    ]
    assert reads and all(elapsed <= 20.0 for elapsed in reads), reads


def test_a_failed_sync_run_is_told_once(live_api: Callable[..., LiveApi], plant_dir: Path) -> None:
    sources, _ = plant(plant_dir, "registry", "scada")
    live = live_api(sources)
    live.plant.break_("scada", SourceUnavailable("the source did not answer in time"))

    done = live.run({})

    report: dict[str, Any] = done["result"]
    assert report["status"] == "partial"
    listing = live.ok("/api/v1/lifecycle/notifications")
    assert listing["kind"] == "log"
    (told,) = [found for found in listing["data"] if found["type"] == "twin.sync.failed"]
    assert told["status"] == "logged" and told["subject"] == report["run"]
    assert told["data"]["status"] == "partial"
    assert list(told["data"]["sources"]) == ["scada"]
    live.plant.mend()
    assert live.run({})["result"]["status"] == "succeeded"
    listing = live.ok("/api/v1/lifecycle/notifications")["data"]
    assert [found["type"] for found in listing].count("twin.sync.failed") == 1
