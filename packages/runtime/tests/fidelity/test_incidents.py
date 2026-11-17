"""Stale incidents, incidents opened and closed from outside, resolutions and retries by
hand, the refusals of every writer, and the order of the events of one check."""

from __future__ import annotations

from datetime import datetime

import pytest
from dta_runtime import fidelity
from dta_runtime.fidelity import FidelityError, Freshness
from dta_runtime.fidelity.records import incident_uid, signal_uid
from fidelity_testkit import SOURCE, Oracle, at, gauge, kinds, shifted

INCIDENT = "twin.fidelity.incident"
STATE = "twin.fidelity.state"
FRESH = {"level": Freshness(fresh=True, age_s=900.0, staleness_s=3600.0, source=SOURCE)}
STALE = {"level": Freshness(fresh=False, age_s=7200.0, staleness_s=3600.0, source=SOURCE)}
UNSURE = {"level": Freshness(fresh=None, age_s=None, staleness_s=3600.0, source=None)}


def checked_plant(make_plant):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(plant)
    assert kinds(plant.check(5, oracle, freshness=FRESH).events) == [(STATE, "healthy")]
    return plant, oracle


def refusal(call, *args, **kwargs) -> str:
    with pytest.raises(FidelityError) as refused:
        call(*args, **kwargs)
    return refused.value.code


def test_a_stale_question_opens_resolves_fails_and_recovers(make_plant):
    plant, oracle = checked_plant(make_plant)
    opened = plant.step(6, oracle, freshness=STALE)
    assert kinds(opened.events) == [(INCIDENT, "opened"), (STATE, "correcting")]
    first = opened.opened[0]
    assert first["uid"] == incident_uid("stale", "level", at(6))
    assert (first["kind"], first["subject"], first["status"], first["attempt"]) == (
        "stale",
        "level",
        "open",
        1,
    )
    assert first["detail"]["opened"]["age_s"] == 7200.0
    assert opened.check["freshness"]["questions"]["level"]["fresh"] is False
    fresh = plant.step(7, oracle, freshness=FRESH)
    assert kinds(fresh.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]

    again = plant.step(8, oracle, freshness=STALE)
    assert [found["uid"] for found in again.opened] == [incident_uid("stale", "level", at(8))]
    # One more stale check is counted, and is no event.
    assert plant.step(9, oracle, freshness=STALE).events == []
    # A check that cannot tell changes nothing.
    assert plant.step(10, oracle, freshness=UNSURE).events == []
    failed = plant.step(11, oracle, freshness=STALE)
    assert kinds(failed.events) == [(INCIDENT, "changed"), (STATE, "failed")]
    record = fidelity.incident(plant.twin, again.opened[0]["uid"])
    assert (record["status"], record["attempt"]) == ("failed", 3)
    assert record["steps"][-1]["action"] == "failed"
    assert plant.step(12, oracle, freshness=STALE).events == []
    recovered = plant.step(13, oracle, freshness=FRESH)
    assert kinds(recovered.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    # Freshness of a question the contract does not ask is set aside.
    unknown = {"nope": Freshness(fresh=False, age_s=1.0, staleness_s=1.0, source=SOURCE)}
    assert plant.step(14, oracle, freshness=unknown).events == []
    assert fidelity.incidents(plant.twin, status="unresolved") == []
    assert len(fidelity.incidents(plant.twin, kind="stale")) == 2


def test_a_failed_stale_incident_is_retried(make_plant):
    plant, oracle = checked_plant(make_plant)
    outcomes = plant.run(range(6, 9), oracle, freshness=STALE)
    uid = outcomes[6].opened[0]["uid"]
    assert fidelity.incident(plant.twin, uid)["status"] == "failed"
    retried = fidelity.retry(plant.twin, uid, by="operator", note="source fixed", now=at(8))
    assert (retried["status"], retried["attempt"]) == ("open", 0)
    assert retried["detail"]["failed_at"] is None
    assert kinds(retried["events"]) == [(INCIDENT, "changed"), (STATE, "correcting")]
    assert plant.step(9, oracle, freshness=STALE).events == []
    assert fidelity.incident(plant.twin, uid)["attempt"] == 1
    resolved = plant.step(10, oracle, freshness=FRESH)
    assert kinds(resolved.events) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    assert refusal(fidelity.retry, plant.twin, uid, by="operator", note=None, now=at(10)) == (
        "fidelity.conflict"
    )


def test_a_breach_and_a_stale_incident_opened_by_one_check(make_plant):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(plant, shifted(1.0, since=at(6)))
    plant.run(range(6, 10), oracle, freshness=FRESH)
    both = plant.step(10, oracle, freshness=STALE)
    assert kinds(both.events) == [(INCIDENT, "opened"), (INCIDENT, "opened"), (STATE, "correcting")]
    assert [event["data"]["kind"] for event in both.events[:2]] == ["breach", "stale"]
    assert [found.incident for found in both.corrections] == [both.events[0]["subject"]]
    assert {found["kind"] for found in fidelity.incidents(plant.twin, status="open")} == {
        "breach",
        "stale",
    }


def test_incidents_opened_and_closed_from_outside(make_plant):
    plant, oracle = checked_plant(make_plant)
    subject = "twin_action_record:7"
    opened = fidelity.open_incident(
        plant.twin, "reconcile", subject, {"expected": 15.0, "observed": 11.0}, now=at(5)
    )
    assert (opened["kind"], opened["subject"], opened["status"]) == ("reconcile", subject, "open")
    assert opened["detail"]["opened"] == {"expected": 15.0, "observed": 11.0}
    assert opened["version"] is not None
    assert kinds(opened["events"]) == [(INCIDENT, "opened"), (STATE, "correcting")]
    head = plant.store.head()
    again = fidelity.open_incident(plant.twin, "reconcile", subject, {}, now=at(5))
    assert again["uid"] == opened["uid"] and again["version"] is None and again["events"] == []
    assert plant.store.head() == head
    assert fidelity.state(plant.twin)["state"] == "correcting"

    closed = fidelity.close_incident(
        plant.twin,
        "reconcile",
        subject,
        by="actions",
        note="the effect was observed",
        now=at(5),
        detail={"observed": 15.0},
    )
    assert closed is not None and closed["status"] == "resolved"
    assert closed["detail"]["closed"] == {"observed": 15.0}
    assert kinds(closed["events"]) == [(INCIDENT, "resolved"), (STATE, "healthy")]
    assert (
        fidelity.close_incident(plant.twin, "reconcile", subject, by="a", note=None, now=at(5))
        is None
    )

    # Opened again at the same time: a uid of its own.
    second = fidelity.open_incident(plant.twin, "reconcile", subject, {}, now=at(5))
    assert second["uid"] == f"{opened['uid']}-2"
    resolved = fidelity.resolve(plant.twin, second["uid"], by="operator", note="checked", now=at(5))
    assert resolved["status"] == "resolved" and resolved["detail"]["resolved_by"] == "operator"
    assert resolved["steps"][-1] == {
        "at": "2026-01-05T01:15:00Z",
        "action": "resolve",
        "by": "operator",
        "note": "checked",
    }
    record = fidelity.incident(plant.twin, second["uid"])
    assert record["resolved_version"] == resolved["version"]
    listed = fidelity.incidents(plant.twin, kind="reconcile")
    assert sorted(found["uid"] for found in listed) == sorted([opened["uid"], second["uid"]])
    assert fidelity.incidents(plant.twin, kind="reconcile", status="unresolved") == []


def test_every_writer_refuses_what_it_cannot_do(make_plant):
    plant, oracle = checked_plant(make_plant)
    twin = plant.twin
    reconcile = fidelity.open_incident(twin, "reconcile", "twin_action_record:1", {}, now=at(5))
    stale = plant.step(6, oracle, freshness=STALE).opened[0]["uid"]
    now = at(6)
    assert refusal(fidelity.open_incident, twin, "breach", "level", {}, now=now) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.close_incident, twin, "stale", "level", by="x", note=None, now=now) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.open_incident, twin, "reconcile", "", {}, now=now) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.open_incident, twin, "reconcile", "s", {"when": object()}, now=now) == (
        "fidelity.bad_request"
    )
    naive = datetime(2026, 1, 5, 2, 0)
    assert refusal(fidelity.resolve, twin, reconcile["uid"], by="x", note=None, now=naive) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.resolve, twin, reconcile["uid"], by="", note=None, now=now) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.resolve, twin, stale, by="x", note=None, now=now) == (
        "fidelity.bad_request"
    )
    assert refusal(fidelity.retry, twin, reconcile["uid"], by="x", note=None, now=now) == (
        "fidelity.bad_request"
    )
    missing = "twin_incident:breach-none"
    assert refusal(fidelity.resolve, twin, missing, by="x", note=None, now=now) == (
        "fidelity.not_found"
    )
    assert refusal(fidelity.retry, twin, missing, by="x", note=None, now=now) == (
        "fidelity.not_found"
    )
    signal = signal_uid("Gauge.level", gauge("G1"))
    assert refusal(fidelity.clear_signal, twin, signal, by="x", note=None, now=now) == (
        "fidelity.not_found"
    )
    # Between two checks made already; a check at the time of one is given back instead.
    assert refusal(plant.check, 5.5, oracle) == "fidelity.out_of_order"
    assert plant.check(5, oracle).replayed


def test_the_row_that_keeps_a_suspect_reading():
    row = fidelity.observed_reading("Gauge.level", gauge("G1"), 11.5, t=at(3))
    assert row == {
        "uid": signal_uid("Gauge.level", gauge("G1")),
        "reading": "11.5",
        "reading_at": at(3),
    }
    with pytest.raises(FidelityError):
        fidelity.observed_reading("Gauge.level", gauge("G1"), 11.5, t=datetime(2026, 1, 5))
