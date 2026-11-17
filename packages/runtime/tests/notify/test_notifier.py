"""The notifier against a real webhook: what the receiver sees, the order, retries,
giving up, the listing, a full queue, closing, the log kind and the missing variable;
and the address of the webhook in no line of the log."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dta_core.errors import ConfigError
from dta_runtime.notify import (
    DELIVERED,
    DROPPED,
    EVENT_MEDIA,
    FAILED,
    LOGGED,
    UNSENT,
    Notifier,
    notifier_for,
)
from runtime_testkit import water_manifest
from webhook_testkit import own_port, webhook

ONTOLOGY = Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
T = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
FAST = (0.2, 0.2)
VARIABLE = "DTA_TEST_WEBHOOK"


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def manifest(notification: dict[str, Any] | None) -> Any:
    contract: dict[str, Any] = {"tier": "static"}
    if notification is not None:
        contract["notification"] = notification
    return water_manifest(ONTOLOGY.read_text(encoding="utf-8"), contract=contract)


def wait_until(condition: Any, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    pause = threading.Event()
    while not condition():
        assert time.monotonic() < deadline, "the condition was not met in time"
        pause.wait(0.02)


def notifier(url: str, **options: Any) -> Notifier:
    chosen = {"backoff_s": FAST, **options}
    return Notifier(twin_id="water", kind="webhook", address=url, variable=VARIABLE, **chosen)


def test_the_receiver_sees_a_cloudevent_in_structured_json(log_file: Path) -> None:
    with webhook() as receiver:
        sender = notifier(receiver.url)
        sender.start()
        try:
            notice = sender.emit(
                "twin.fidelity.state",
                "twin_fidelity:state",
                {"from": "healthy", "to": "correcting"},
                time=T,
            )
            [event] = receiver.wait_for(1)
        finally:
            sender.close()
    found = receiver.received[0]
    assert found.path == "/hooks/twin"
    assert found.headers["content-type"] == EVENT_MEDIA
    assert event == {
        "specversion": "1.0",
        "id": notice.event["id"],
        "source": "/twins/water",
        "type": "twin.fidelity.state",
        "subject": "twin_fidelity:state",
        "time": "2026-01-05T12:00:00Z",
        "datacontenttype": "application/json",
        "data": {"from": "healthy", "to": "correcting"},
    }
    assert len(event["id"]) == 32
    [listed] = sender.listing()
    assert listed["status"] == DELIVERED and listed["tries"] == 1 and listed["http_status"] == 204
    delivered = [line for line in lines(log_file) if line["message"] == "notification delivered"]
    assert delivered[0]["data"]["id"] == event["id"]
    assert delivered[0]["data"]["type"] == "twin.fidelity.state"
    assert delivered[0]["data"]["status"] == 204 and delivered[0]["data"]["attempt"] == 1


def test_events_arrive_in_the_order_they_were_emitted() -> None:
    with webhook() as receiver:
        sender = notifier(receiver.url)
        sender.start()
        try:
            for index in range(12):
                moment = T + timedelta(minutes=index)
                sender.emit("twin.test", f"s{index}", {"n": index}, time=moment)
            events = receiver.wait_for(12)
        finally:
            sender.close()
    assert [event["data"]["n"] for event in events] == list(range(12))
    assert [event["subject"] for event in events] == [f"s{index}" for index in range(12)]


def test_a_refusal_and_a_refused_connection_are_tried_again(log_file: Path) -> None:
    with webhook(statuses=[500]) as receiver:
        sender = notifier(receiver.url)
        sender.start()
        try:
            sender.emit("twin.test", "a", {}, time=T)
            receiver.wait_for(1)
        finally:
            sender.close()
    assert receiver.answered == [500, 204]
    [listed] = sender.listing()
    assert listed["status"] == DELIVERED and listed["tries"] == 2

    # Nothing listens yet: the first try's connection is refused.
    port = own_port()
    sender = notifier(f"http://127.0.0.1:{port}/hooks/twin", backoff_s=(1.0, 1.0))
    sender.start()
    try:
        notice = sender.emit("twin.test", "b", {}, time=T)
        wait_until(lambda: notice.tries >= 1 and notice.error is not None)
        with webhook(port=port) as late:
            late.wait_for(1)
            wait_until(lambda: notice.status == DELIVERED)
    finally:
        sender.close()
    assert notice.tries >= 2 and notice.http_status == 204
    retried = [line for line in lines(log_file) if "trying again" in line["message"]]
    assert {line["data"]["error"] for line in retried} >= {"HTTP 500", "ConnectError"}
    assert all(line["level"] == "warn" for line in retried)


def test_after_three_failed_tries_the_event_is_given_up(log_file: Path) -> None:
    with webhook(statuses=[500, 502, 503]) as receiver:
        sender = notifier(receiver.url)
        sender.start()
        try:
            notice = sender.emit("twin.test", "a", {"x": 1}, time=T)
            wait_until(lambda: notice.status == FAILED)
            # The next event is not held up by the one given up.
            sender.emit("twin.test", "b", {"x": 2}, time=T)
            receiver.wait_for(1)
        finally:
            sender.close()
    assert receiver.answered == [500, 502, 503, 204]
    assert notice.tries == 3 and notice.http_status == 503
    given_up = [line for line in lines(log_file) if line["message"] == "notification given up"]
    assert len(given_up) == 1 and given_up[0]["level"] == "error"
    assert given_up[0]["data"]["attempt"] == 3 and given_up[0]["data"]["status"] == 503
    assert [found["status"] for found in sender.listing()] == [DELIVERED, FAILED]


def test_the_listing_keeps_the_last_two_hundred_newest_first() -> None:
    sender = Notifier(twin_id="water", kind="log")
    sender.start()
    for index in range(205):
        sender.emit("twin.test", None, {"n": index}, time=T)
    listed = sender.listing()
    assert len(listed) == 200
    assert [found["data"]["n"] for found in listed[:2]] == [204, 203]
    assert listed[-1]["data"]["n"] == 5
    assert {found["status"] for found in listed} == {LOGGED}
    assert "subject" not in listed[0] or listed[0]["subject"] is None


def test_a_full_queue_drops_its_oldest_event(log_file: Path) -> None:
    # Not started: nothing is delivered, so the queue fills.
    sender = notifier("http://127.0.0.1:9/never", capacity=3)
    notices = [sender.emit("twin.test", str(index), {}, time=T) for index in range(5)]
    assert [notice.status for notice in notices] == [DROPPED, DROPPED, "queued", "queued", "queued"]
    assert sender.pending() == 3
    dropped = [line for line in lines(log_file) if "queue is full" in line["message"]]
    assert [line["data"]["id"] for line in dropped] == [
        notices[0].event["id"],
        notices[1].event["id"],
    ]
    sender.close(drain_s=0.1)
    assert {notice.status for notice in notices[2:]} == {UNSENT}


def test_closing_drains_for_at_most_its_time() -> None:
    port = own_port()
    sender = notifier(f"http://127.0.0.1:{port}/hooks/twin", backoff_s=(30.0, 30.0))
    sender.start()
    first = sender.emit("twin.test", "a", {}, time=T)
    second = sender.emit("twin.test", "b", {}, time=T)
    wait_until(lambda: first.tries >= 1 and first.error is not None)
    started = time.monotonic()
    sender.close(drain_s=1.0)
    assert time.monotonic() - started < 3.0
    assert first.status == FAILED and second.status == UNSENT


def test_the_log_kind_only_logs(log_file: Path) -> None:
    sender = notifier_for(manifest({"kind": "log"}), {})
    assert sender.kind == "log"
    sender.start()
    sender.emit("twin.fidelity.incident", "twin_incident:x", {"action": "opened"}, time=T)
    sender.close()
    [listed] = sender.listing()
    assert listed["status"] == LOGGED and listed["tries"] == 0
    logged = [line for line in lines(log_file) if line["message"] == "notification"]
    assert logged[0]["data"]["type"] == "twin.fidelity.incident"
    assert logged[0]["data"]["event_data"] == {"action": "opened"}
    # A manifest that asks for nothing is the same.
    assert notifier_for(manifest(None), {}).kind == "log"


def test_a_webhook_whose_variable_is_missing_is_refused_by_its_name(log_file: Path) -> None:
    wanted = manifest({"kind": "webhook", "webhook_env": VARIABLE})
    with pytest.raises(ConfigError) as refused:
        notifier_for(wanted, {})
    assert VARIABLE in refused.value.message and refused.value.code == "config.missing_env"
    with pytest.raises(ConfigError) as bad:
        notifier_for(wanted, {VARIABLE: "ftp://example.org/hook"})
    assert bad.value.code == "config.bad_value" and "ftp" not in bad.value.message
    built = notifier_for(wanted, {VARIABLE: "http://127.0.0.1:9/hook"}, backoff_s=FAST)
    assert built.kind == "webhook" and built.variable == VARIABLE


def test_no_line_of_the_log_holds_the_address(log_file: Path) -> None:
    port = own_port()
    with webhook(statuses=[500, 404]) as receiver:
        wanted = manifest({"kind": "webhook", "webhook_env": VARIABLE})
        sender = notifier_for(wanted, {VARIABLE: receiver.url}, backoff_s=FAST)
        sender.start()
        try:
            sender.emit("twin.test", "a", {}, time=T)
            receiver.wait_for(1)
        finally:
            sender.close()
    unreachable = notifier(f"http://127.0.0.1:{port}/secret-path-x", backoff_s=(0.05, 0.05))
    unreachable.start()
    notice = unreachable.emit("twin.test", "b", {}, time=T)
    wait_until(lambda: notice.status == FAILED)
    unreachable.close()
    text = log_file.read_text(encoding="utf-8")
    assert receiver.url not in text and "/hooks/twin" not in text
    assert "secret-path-x" not in text and f"127.0.0.1:{port}" not in text
    assert f"127.0.0.1:{receiver.port}" not in text
    assert "notification given up" in text and VARIABLE in text
    for listing in (sender.listing(), unreachable.listing()):
        assert "127.0.0.1" not in json.dumps(listing)
