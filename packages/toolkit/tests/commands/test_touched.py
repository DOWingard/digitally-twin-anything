"""What a registration touched, as the build reads it to decide what to judge again."""

from __future__ import annotations

from dta_toolkit.commands import computeops


def test_functions_created_or_changed_are_named_by_id():
    registered = {
        "changes": {
            "functions": {
                "created": ["twin_function:f_new"],
                "changed": ["twin_function:f_leak_zone"],
                "unchanged": ["twin_function:f_latest"],
            },
            "questions": {"created": [], "changed": [], "unchanged": ["twin_question:q"]},
        }
    }
    assert computeops.touched_functions(registered) == {"f_new", "f_leak_zone"}
    assert computeops.touched_questions(registered) == set()


def test_no_registration_touched_nothing():
    assert computeops.touched_functions(None) == set()
    assert computeops.touched_functions({"changes": {}}) == set()
