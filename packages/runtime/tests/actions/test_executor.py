"""The HTTP executor against a local plant: what arrives, what is kept, what is never
logged or stored, and how it fails."""

from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from typing import Any

import pytest
from actions_testkit import (
    LATE_PORT,
    PUMP,
    REFUSING_PORT,
    TOKEN,
    VALVE,
    Plant,
    World,
    at,
    capture,
    environ,
    executor,
    manifest_of,
    plant_on_first_retry,
    register,
    stored_text,
)
from dta_core.errors import DtaError
from dta_runtime import actions
from dta_runtime.actions import ActionError, HttpExecutor

NOW = at(2)


@pytest.fixture(scope="module")
def http(world: World, plant: Plant) -> Iterator[HttpExecutor]:
    found = executor(world.manifest)
    yield found
    found.close()


def send(world: World, sender: Any, action: str = "set_pump", **more: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "object": PUMP,
        "params": {"value": True},
        "reason": "executor test",
        "source": "tester",
        "state": "healthy",
        "now": NOW,
        "executor": sender,
    }
    options.update(more)
    return actions.propose(world.twin, action, **options)


def test_what_arrives_at_the_plant_is_the_filled_template(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    made = send(world, http)
    assert made["status"] == "executed"
    (received,) = fresh_plant.received
    assert received.method == "POST"
    assert received.path == "/api/v1/controls/PMP-10"
    assert received.query == {"token": [TOKEN]}
    assert received.headers["X-Api-Key"] == TOKEN
    assert received.headers["X-Object"] == PUMP
    assert received.headers["Idempotency-Key"] == made["id"]
    assert received.headers["Content-Type"] == "application/json"
    # The mapped value, the object's id, and a parameter inside a text.
    assert received.body == {"status": "on", "asset": "PMP-10", "note": f"set true on {PUMP}"}


def test_a_body_text_of_one_parameter_sends_it_with_its_own_type(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    made = send(world, http, "set_valve", object=VALVE, params={"value": 12.5})
    approved = actions.approve(
        world.twin, made["uid"], by="operator", note=None, state="healthy", now=NOW, executor=http
    )
    assert approved["status"] == "executed"
    (received,) = fresh_plant.received
    assert received.method == "PUT"
    assert received.path == "/api/v1/controls/VLV-1"
    assert received.query == {}
    assert received.body == {"setting": 12.5}
    assert received.headers["Idempotency-Key"] == made["id"]


def test_the_mapped_value_follows_the_parameter(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    send(world, http, params={"value": False})
    assert fresh_plant.received[0].body["status"] == "off"


def test_the_effective_time_is_the_answers_or_else_now(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    fresh_plant.effective_at = "2026-01-05T00:45:00+00:00"
    made = send(world, http)
    assert made["effective_at"] == at(3)
    assert made["execution"]["effective_at"] == "2026-01-05T00:45:00Z"
    assert made["execution"]["at"] == "2026-01-05T00:30:00Z"
    assert actions.record(world.twin, made["uid"])["effective_at"] == at(3)

    fresh_plant.effective_at = None
    assert send(world, http)["effective_at"] == NOW
    # A time without an offset is not taken as one.
    fresh_plant.effective_at = "2026-01-05T00:45:00"
    assert send(world, http)["effective_at"] == NOW


def test_what_is_kept_of_the_answer_is_its_status_duration_and_start(
    world: World, fresh_plant: Plant, http: HttpExecutor
):
    fresh_plant.answer = "x" * 5000
    made = send(world, http)
    execution = actions.record(world.twin, made["uid"])["execution"]
    assert execution["http_status"] == 200
    assert execution["duration_ms"] > 0
    assert execution["attempts"] == 1
    assert execution["excerpt"] == "x" * 2000
    assert execution["idempotency_key"] == made["id"]
    assert execution["error"] is None


def test_no_secret_is_logged_or_stored(world: World, fresh_plant: Plant, http: HttpExecutor):
    # A plant that echoes what it was sent.
    fresh_plant.answer = {"echo": f"token={TOKEN}", "url": f"http://plant/?token={TOKEN}"}
    with capture() as captured:
        made = send(world, http)
        fresh_plant.mode = "500"
        failed = send(world, http)
        refusing = executor(world.manifest, f"http://127.0.0.1:{REFUSING_PORT}")
        try:
            unreachable = send(world, refusing)
        finally:
            refusing.close()
    assert made["status"] == "executed"
    assert failed["status"] == "failed"
    assert unreachable["status"] == "failed"
    assert fresh_plant.received[0].query == {"token": [TOKEN]}
    logged = captured.text()
    # The library's own line for each request was written, with the secret masked.
    assert "HTTP Request" in logged
    assert TOKEN not in logged
    assert TOKEN not in stored_text(world)
    assert "***" in actions.record(world.twin, made["uid"])["execution"]["excerpt"]
    sent = [data for message, data in captured.messages("info") if message == "action sent"]
    assert sent[0] == {
        "action": "set_pump",
        "record": made["uid"],
        "method": "POST",
        "status": 200,
        "attempts": 1,
        "duration_ms": sent[0]["duration_ms"],
    }


def test_a_missing_variable_stops_the_executor_from_being_built(world: World):
    given = environ()
    del given["PLANT_TOKEN"]
    with capture() as captured, pytest.raises(DtaError) as caught:
        HttpExecutor.from_manifest(world.manifest, given)
    assert caught.value.code == "config.missing_env"
    assert caught.value.detail["missing"] == ["PLANT_TOKEN"]
    assert "PLANT_TOKEN" in caught.value.message
    given["PLANT_URL"] = ""
    with pytest.raises(DtaError) as caught:
        HttpExecutor.from_manifest(world.manifest, given)
    assert caught.value.detail["missing"] == ["PLANT_URL", "PLANT_TOKEN"]
    assert "PLANT_TOKEN" in captured.text()


def test_a_variable_a_template_uses_and_its_executor_does_not_list_is_refused(world: World):
    document = copy.deepcopy(world.document)
    document["actions"]["set_pump"]["executor"]["headers"]["X-Other"] = "${UNRELATED}"
    with pytest.raises(ActionError) as caught:
        HttpExecutor.from_manifest(manifest_of(document), environ())
    assert caught.value.code == "action.executor_invalid"
    assert caught.value.detail == {"action": "set_pump", "variables": ["UNRELATED"]}


def test_without_the_add_on_there_is_no_executor(world: World):
    document = copy.deepcopy(world.document)
    document["contract"]["addons"]["executors"] = False
    assert HttpExecutor.from_manifest(manifest_of(document), {}) is None


@pytest.mark.parametrize("mode", ["422", "500"])
def test_an_http_error_fails_the_action_and_is_not_sent_again(
    world: World, fresh_plant: Plant, http: HttpExecutor, mode: str
):
    fresh_plant.mode = mode
    made = send(world, http)
    assert made["status"] == "failed"
    assert made["execution"]["http_status"] == int(mode)
    assert made["execution"]["error"] == f"the system answered {mode}"
    assert made["execution"]["attempts"] == 1
    assert made["effective_at"] is None
    assert made["reconciliation"] is None
    assert len(fresh_plant.received) == 1


def test_a_refused_connection_is_tried_twice_more(world: World):
    refusing = executor(world.manifest, f"http://127.0.0.1:{REFUSING_PORT}", backoff_s=0.1)
    try:
        with capture() as captured:
            started = time.monotonic()
            made = send(world, refusing)
            took = time.monotonic() - started
    finally:
        refusing.close()
    assert made["status"] == "failed"
    assert made["execution"]["attempts"] == 3
    assert made["execution"]["http_status"] is None
    assert "no connection after 3 attempts" in made["execution"]["error"]
    retries = [data for message, data in captured.messages("warn") if "trying again" in message]
    assert [data["attempt"] for data in retries] == [1, 2]
    assert [data["backoff_s"] for data in retries] == [0.1, 0.2]
    assert took >= 0.3


def test_a_connection_refused_once_then_accepted_executes(world: World):
    late = executor(world.manifest, f"http://127.0.0.1:{LATE_PORT}", backoff_s=0.1)
    try:
        with plant_on_first_retry(LATE_PORT) as made_plants:
            made = send(world, late)
            received = list(made_plants[0].received)
    finally:
        late.close()
    assert made["status"] == "executed"
    assert made["execution"]["attempts"] == 2
    assert len(received) == 1
    assert received[0].headers["Idempotency-Key"] == made["id"]


def test_a_hang_ends_at_the_timeout(world: World, fresh_plant: Plant):
    fresh_plant.mode = "hang"
    fresh_plant.release.clear()
    quick = executor(world.manifest, timeout_s=1.0)
    try:
        started = time.monotonic()
        made = send(world, quick)
        took = time.monotonic() - started
    finally:
        fresh_plant.release.set()
        quick.close()
    assert made["status"] == "failed"
    assert made["execution"]["attempts"] == 1
    assert "no answer within 1 s" in made["execution"]["error"]
    assert 1.0 <= took < 5.0
    assert len(fresh_plant.received) == 1


def test_a_request_that_cannot_be_filled_in_is_not_sent(world: World, fresh_plant: Plant):
    document = copy.deepcopy(world.document)
    document["actions"]["set_pump"]["executor"]["body"]["status"]["map"] = {"true": "on"}
    narrower = manifest_of(document)
    register(world.twin, narrower)
    sender = executor(narrower)
    try:
        made = send(world, sender, params={"value": False})
    finally:
        sender.close()
        register(world.twin, world.manifest)
    assert made["status"] == "failed"
    assert made["execution"]["error"] == (
        "the request cannot be made: the map of value has no entry for false"
    )
    assert made["execution"]["attempts"] == 0
    assert fresh_plant.received == []
