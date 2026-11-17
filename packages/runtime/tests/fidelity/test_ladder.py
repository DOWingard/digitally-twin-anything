"""The next step of a breach incident, a stale incident, and the state, as plain data."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from dta_core.manifest.models import Correction
from dta_runtime.fidelity import ladder
from dta_runtime.fidelity.ladder import (
    CORRECTING,
    FAILED,
    HEALTHY,
    OPEN,
    REMODEL_REQUESTED,
    RESOLVED,
    UNKNOWN,
    Pending,
    Position,
    answered,
    cut_of,
    next_step,
    opened,
    remodelled,
    retried,
    stale_step,
    state_of,
)
from dta_runtime.fidelity.measures import Evidence

T0 = datetime(2026, 1, 5, tzinfo=UTC)
D = timedelta(minutes=15)
TOL = 0.5
NONE = Evidence(0, None, T0)
GOOD = Evidence(3, 0.2, T0)
BAD = Evidence(3, 0.9, T0)
DEFAULT = Correction()


def step(position: Position, found: Evidence = NONE, *, settings=DEFAULT, now=T0, remodel=False):
    return next_step(position, settings, found, now=now, tolerance=TOL, remodelled=remodel)


def actions(found: ladder.Step) -> list[str]:
    return [item["action"] for item in found.logged]


def pending(position: Position, rung: int) -> Position:
    return replace(position, pending=Pending("k", rung, position.attempt, position.cycle, T0))


def test_an_opened_incident_asks_for_a_recalibration_first():
    found = step(opened())
    assert found.request == (1, 0, 1)
    assert found.position.status == OPEN and found.logged == []


def test_while_a_correction_is_pending_or_its_model_has_few_origins_it_waits():
    assert step(pending(opened(), 1)).request is None
    awaiting = replace(opened(), awaiting=T0)
    found = step(awaiting, Evidence(2, 0.9, T0))
    assert found.request is None and found.position == awaiting


def test_a_correction_that_holds_resolves_and_one_that_does_not_moves_on():
    awaiting = replace(opened(), awaiting=T0)
    held = step(awaiting, GOOD)
    assert held.position.status == RESOLVED and actions(held) == ["held"]
    missed = step(awaiting, BAD)
    assert actions(missed) == ["not_held"]
    assert missed.request == (1, 1, 1)
    assert missed.position.awaiting is None


def test_with_no_correction_in_place_good_origins_since_the_opening_resolve_it():
    found = step(pending(opened(), 1), GOOD)
    assert found.position.status == RESOLVED and found.position.pending is None
    assert actions(found) == ["recovered"]


def test_the_rungs_in_order_and_the_remodel_request():
    position = replace(opened(), attempt=2)
    reselect = step(position)
    assert reselect.request == (2, 0, 1)
    position = ladder.failed_rung(reselect.position)
    assert position.rung == 3
    remodel = step(position, now=T0 + D)
    assert remodel.remodel_requested and remodel.request is None
    assert remodel.position.status == REMODEL_REQUESTED
    assert remodel.position.requested_at == T0 + D
    assert actions(remodel) == ["remodel_requested"]
    # A model registered or changed: rung 2 again, its second attempt.
    again = step(remodel.position, now=T0 + 2 * D, remodel=True)
    assert again.request == (2, 1, 1)
    assert again.position.after_remodel and again.position.status == OPEN
    assert actions(again) == ["remodelled"]


def test_the_remodel_timeout_fails_the_incident():
    waiting = Position(REMODEL_REQUESTED, 3, 2, 1, requested_at=T0)
    timeout = DEFAULT.remodel_timeout
    assert step(waiting, now=T0 + timeout - D).position.status == REMODEL_REQUESTED
    failed = step(waiting, now=T0 + timeout)
    assert failed.position.status == FAILED and failed.position.failed_at == T0 + timeout
    assert failed.logged[-1]["reason"] == "remodel_timeout"


def test_a_cycle_ends_after_the_remodel_and_the_budget_fails_it():
    spent = Position(OPEN, 3, 3, 1, after_remodel=True)
    found = step(spent)
    assert actions(found) == ["cycle"]
    assert found.request == (1, 0, 2)
    assert not found.position.after_remodel
    last = replace(spent, cycle=DEFAULT.retry_budget)
    failed = step(last)
    assert failed.position.status == FAILED
    assert failed.logged[-1] == {"action": "failed", "reason": "retry_budget", "cycles": 3}


def test_each_switch_of_the_correction_settings():
    no_recalibration = Correction(recalibrate_attempts=0)
    assert step(opened(), settings=no_recalibration).request == (2, 0, 1)
    no_reselect = Correction(reselect=False)
    assert step(replace(opened(), attempt=2), settings=no_reselect).remodel_requested
    nothing_but_recalibration = Correction(reselect=False, agent_remodel=False, retry_budget=2)
    found = step(replace(opened(), attempt=2), settings=nothing_but_recalibration)
    assert found.request == (1, 0, 2)
    no_remodel = Correction(agent_remodel=False)
    rung_three = Position(OPEN, 3, 2, 1)
    assert step(rung_three, settings=no_remodel).request == (1, 0, 2)


def test_a_failed_incident_resolves_only_on_a_model_fitted_after_the_failure():
    failed = Position(FAILED, 1, 0, 3, failed_at=T0)
    assert cut_of(failed, T0 - 10 * D, T0 - D) is None
    assert cut_of(failed, T0 - 10 * D, T0 + D) == T0 + D
    assert step(failed, Evidence(0, None, None)).position == failed
    assert step(failed, BAD).position == failed
    resolved = step(failed, GOOD)
    assert resolved.position.status == RESOLVED and actions(resolved) == ["resolved"]
    assert cut_of(opened(), T0, None) == T0
    assert cut_of(replace(opened(), awaiting=T0 + D), T0, None) == T0 + D


def test_an_answer_and_a_retry():
    asked = pending(opened(), 1)
    accepted = answered(asked, accepted=True, fitted_to=T0 + D)
    assert accepted.awaiting == T0 + D and accepted.pending is None
    refused = answered(asked, accepted=False, fitted_to=None)
    assert refused.attempt == 1 and refused.pending is None
    again = retried(Position(FAILED, 3, 3, 2, failed_at=T0, offset=1))
    assert (again.status, again.rung, again.attempt, again.cycle) == (OPEN, 1, 0, 1)
    # Keys count on past the cycles before the retry, so none is used twice.
    assert again.offset == 3
    assert step(again).request == (1, 0, 4)


def test_a_stale_incident():
    budget = 3
    assert stale_step(None, 0, True, budget=budget).status is None
    assert stale_step(None, 0, None, budget=budget).logged == []
    first = stale_step(None, 0, False, budget=budget)
    assert (first.status, first.count) == (OPEN, 1)
    assert [item["action"] for item in first.logged] == ["opened", "stale"]
    second = stale_step(OPEN, 1, False, budget=budget)
    assert (second.status, second.count) == (OPEN, 2)
    third = stale_step(OPEN, 2, False, budget=budget)
    assert (third.status, third.count) == (FAILED, 3)
    assert stale_step(FAILED, 3, False, budget=budget).status == FAILED
    assert stale_step(FAILED, 3, True, budget=budget).status == RESOLVED
    assert stale_step(OPEN, 2, True, budget=budget).status == RESOLVED
    assert stale_step(OPEN, 2, None, budget=budget).logged == []
    reopened = stale_step(RESOLVED, 2, False, budget=budget)
    assert (reopened.status, reopened.count) == (OPEN, 1)


def test_the_state_from_the_incidents():
    assert state_of([], checked=False) == UNKNOWN
    assert state_of([], checked=True) == HEALTHY
    assert state_of([RESOLVED], checked=True) == HEALTHY
    assert state_of([OPEN, RESOLVED], checked=True) == CORRECTING
    assert state_of([REMODEL_REQUESTED], checked=False) == CORRECTING
    assert state_of([OPEN, FAILED], checked=True) == FAILED


def test_a_remodel_is_a_candidate_registered_or_changed():
    before = {"twin_model:a/x": "1"}
    assert not remodelled(before, before)
    assert remodelled(before, {**before, "twin_model:a/y": "2"})
    assert remodelled(before, {"twin_model:a/x": "3"})
    assert not remodelled(before, {})
