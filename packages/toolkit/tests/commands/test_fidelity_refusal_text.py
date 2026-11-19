"""How `dta fidelity incident` tells a correction that was not accepted; no twin needed."""

from __future__ import annotations

from dta_toolkit.commands.acting import _refusal_text


def test_a_refused_correction_is_told_with_its_code_rule_and_numbers():
    step = {
        "at": "2026-01-05T03:00:00Z",
        "action": "result",
        "rung": "reselect",
        "accepted": False,
        "reason": {
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
        },
    }
    assert _refusal_text(step).splitlines() == [
        "2026-01-05T03:00:00Z reselect: not_accepted: no candidate is accepted over the "
        "recent history",
        "  accepted at mae <= 0.5",
        "  twin_model:level/naive: error 0.9, baseline 0.7, skill -0.29, refused: above the "
        "threshold",
    ]
