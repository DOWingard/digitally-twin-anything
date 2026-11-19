"""How the steps of an incident are told in the fidelity tab; no browser and no store
needed."""

from __future__ import annotations

from dta_runtime.ui.text import step_outcome

REASON = {
    "code": "not_accepted",
    "message": "no candidate is accepted over the recent history",
    "numbers": {
        "metric": "mae",
        "threshold": 0.5,
        "candidates": [
            {
                "uid": "twin_model:level/naive",
                "error": 0.9,
                "baseline_error": 0.7,
                "skill": -0.29,
                "accepted": False,
                "reasons": ["above the threshold"],
            }
        ],
    },
}


def test_a_correction_not_accepted_says_why_with_its_numbers():
    told = step_outcome({"action": "result", "accepted": False, "reason": REASON})
    assert told.startswith(
        "not accepted (not_accepted): no candidate is accepted over the recent history"
    )
    assert "accepted at mae <= 0.5" in told
    assert "twin_model:level/naive: error 0.9, baseline 0.7, skill -0.29" in told
    # A step written before reasons were kept, or one without any.
    assert step_outcome({"action": "result", "accepted": False}) == "not accepted"
    empty = {"code": None, "message": None, "numbers": None}
    assert step_outcome({"action": "result", "accepted": False, "reason": empty}) == (
        "not accepted"
    )


def test_an_unknown_value_and_a_clearing_say_what_they_rest_on():
    unknown = {
        "action": "unknown",
        "why": "horizon_passed",
        "message": "the forecast's lead passed the horizon of 2 steps",
        "questions": ["level", "level_far"],
    }
    assert step_outcome(unknown) == (
        "value unknown (horizon_passed): the forecast's lead passed the horizon of 2 steps; "
        "without an answer: level, level_far"
    )
    stuck = {"action": "cleared", "by": "check", "changes": 3, "run": 0}
    assert step_outcome(stuck) == (
        "cleared by check: 3 changes of the raw readings since the flag, 0 intervals "
        "without one"
    )
    biased = {
        "action": "cleared",
        "by": "check",
        "reading": 12.1,
        "inferred": 12.0,
        "tolerance": 0.5,
    }
    assert step_outcome(biased) == "cleared by check: the reading 12.1 within 1 of the inferred 12"
    assert step_outcome({"action": "cleared", "by": "operator"}) == "cleared by operator"
