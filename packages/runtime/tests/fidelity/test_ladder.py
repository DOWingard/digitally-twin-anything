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
    return next_step(
        position, settings, found, now=now, tolerance=TOL, remodelled=remodel, interval=D
    )


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
    # While the request is open, cycle 2 is due one fidelity interval on.
    assert actions(remodel) == ["remodel_requested", "cycle_ended"]
    assert remodel.position.due == T0 + 2 * D
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
    ended = step(spent)
    # The next cycle starts one fidelity interval later, on newer data.
    assert actions(ended) == ["cycle_ended"] and ended.request is None
    assert ended.position.due == T0 + D
    assert step(ended.position, now=T0 + D - timedelta(seconds=1)).request is None
    found = step(ended.position, now=T0 + D)
    assert actions(found) == ["cycle"]
    assert found.request == (1, 0, 2)
    assert not found.position.after_remodel and found.position.due is None
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
    ended = step(replace(opened(), attempt=2), settings=nothing_but_recalibration)
    assert ended.request is None and ended.position.due == T0 + D
    found = step(ended.position, settings=nothing_but_recalibration, now=T0 + D)
    assert found.request == (1, 0, 2)
    no_remodel = Correction(agent_remodel=False)
    rung_three = Position(OPEN, 3, 2, 1)
    ended = step(rung_three, settings=no_remodel)
    assert ended.request is None and ended.position.due == T0 + D
    assert step(ended.position, settings=no_remodel, now=T0 + D).request == (1, 0, 2)


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


# -- cycles over data time -------------------------------------------------------------


def climb(settings: Correction, checks: int, *, accept_at: int | None = None):
    """An incident opened at T0 and checked once per fidelity interval, every correction
    refused as soon as it is asked for (the one asked for at check `accept_at` accepted).
    The checks at which each cycle started and ended, and the position after each check."""
    position = opened()
    starts, ends, positions = {1: 0}, {}, []
    for k in range(checks):
        now = T0 + k * D
        # The model the accepted correction put in place holds from the next check on.
        found = GOOD if position.awaiting is not None else NONE
        result = step(position, found, settings=settings, now=now)
        for item in result.logged:
            if item["action"] == "cycle":
                starts[item["cycle"]] = k
            elif item["action"] == "cycle_ended":
                ends[item["cycle"]] = k
        position = result.position
        if result.request is not None:
            position = answered(position, accepted=k == accept_at, fitted_to=now)
        positions.append(position)
        if position.status in (FAILED, RESOLVED):
            break
    return starts, ends, positions


def test_each_further_cycle_waits_twice_as_long_as_the_one_before():
    starts, ends, _ = climb(Correction(retry_budget=5), 200)
    # Rung 1 twice and rung 2 make a cycle of three checks; it ends at the fourth.
    assert starts == {1: 0, 2: 4, 3: 9, 4: 16, 5: 27}
    assert ends == {1: 3, 2: 7, 3: 12, 4: 19}
    for cycle in range(2, 6):
        assert (starts[cycle] - ends[cycle - 1]) * D == D * 2 ** (cycle - 2)


def test_retry_backoff_sets_the_first_wait():
    starts, ends, _ = climb(Correction(retry_budget=3, retry_backoff=timedelta(hours=1)), 200)
    assert (starts[2] - ends[1]) * D == timedelta(hours=1)
    assert (starts[3] - ends[2]) * D == timedelta(hours=2)


def test_with_a_remodel_request_open_the_budget_runs_its_cycles_and_only_the_timeout_fails():
    for budget in (1, 3, 5):
        settings = Correction(retry_budget=budget)
        starts, _, positions = climb(settings, 200)
        assert sorted(starts) == list(range(1, budget + 1))
        last = positions[-1]
        assert last.status == FAILED
        # The request was made when cycle 1 ended and stayed open through every cycle.
        requested_at = T0 + 3 * D
        assert last.failed_at == requested_at + settings.remodel_timeout
        while_open = [p for p in positions[:-1] if p.requested_at is not None]
        assert {p.status for p in while_open} == {REMODEL_REQUESTED}
        assert {p.requested_at for p in while_open} == {requested_at}


def test_without_rung_three_the_budget_fails_the_incident_at_the_end_of_its_last_cycle():
    for budget, end in ((1, 3), (3, 12), (5, 30)):
        starts, _, positions = climb(Correction(retry_budget=budget, agent_remodel=False), 200)
        assert sorted(starts) == list(range(1, budget + 1))
        last = positions[-1]
        assert last.status == FAILED and last.failed_at == T0 + end * D
        assert all(p.requested_at is None for p in positions)


def test_the_steps_of_a_spent_budget_with_the_request_open():
    settings = Correction(retry_budget=2)
    _, _, positions = climb(settings, 9)
    # Cycle 2 ran from check 4 to 6 and ended at 7: nothing is left but the request.
    spent = positions[7]
    assert (spent.status, spent.rung, spent.cycle, spent.due) == (REMODEL_REQUESTED, 3, 2, None)
    assert spent.spent and not positions[6].spent
    found = step(positions[6], settings=settings, now=T0 + 7 * D)
    assert found.logged == [{"action": "cycles_spent", "cycles": 2}]
    # Later checks add nothing until the model or the timeout.
    waiting = step(spent, settings=settings, now=T0 + 8 * D)
    assert waiting.logged == [] and waiting.position == spent


def test_a_late_cycle_whose_model_holds_resolves_the_incident():
    _, _, positions = climb(Correction(retry_budget=5), 200, accept_at=16)
    accepted = positions[16]
    assert accepted.status == REMODEL_REQUESTED and accepted.awaiting == T0 + 16 * D
    assert accepted.cycle == 4
    assert len(positions) == 18
    resolved = positions[-1]
    assert resolved.status == RESOLVED and resolved.due is None


def test_a_model_registered_while_further_cycles_run_brings_rung_two_back():
    settings = Correction(retry_budget=5)
    _, _, positions = climb(settings, 9)
    # Check 5: cycle 2's first recalibration was refused; the agent's model is there.
    found = step(positions[4], settings=settings, now=T0 + 5 * D, remodel=True)
    assert actions(found) == ["remodelled"]
    assert found.request == (2, 1, 2)
    assert found.position.status == OPEN and found.position.after_remodel
    # While waiting for the next cycle, the same.
    waiting = positions[7]
    assert waiting.due == T0 + 9 * D
    found = step(waiting, settings=settings, now=T0 + 8 * D, remodel=True)
    assert found.request == (2, 1, 2) and found.position.due is None


def test_the_timeout_fails_an_incident_with_a_correction_of_a_further_cycle_running():
    settings = Correction(retry_budget=5, remodel_timeout=timedelta(hours=1))
    position = replace(
        Position(REMODEL_REQUESTED, 1, 0, 2, requested_at=T0),
        pending=Pending("k", 1, 0, 2, T0 + 3 * D),
    )
    failed = step(position, settings=settings, now=T0 + 4 * D)
    assert failed.position.status == FAILED and failed.position.pending is None
    assert failed.logged[-1]["reason"] == "remodel_timeout"


def test_cycles_without_a_rung_to_run_are_not_waited_for():
    remodel_only = Correction(recalibrate_attempts=0, reselect=False)
    asked = step(opened(), settings=remodel_only)
    assert asked.remodel_requested and asked.position.due is None
    assert actions(asked) == ["remodel_requested"]
    assert step(asked.position, settings=remodel_only, now=T0 + D).logged == []
