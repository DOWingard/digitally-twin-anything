"""What `dta actions` says of a control's fail-safe and of a record's origin, before and
without asking the API."""

from __future__ import annotations

from dta_toolkit.commands.acting import BAD_PARAM, _failure_words


def test_each_choice_of_a_control_is_told_in_the_words_of_the_manifest_summary() -> None:
    assert _failure_words(None) == ""
    assert _failure_words({"mode": "hold", "parameters": {}}).startswith("hold: nothing is sent")
    released = _failure_words(
        {
            "mode": "release",
            "action": "release_pump",
            "parameters": {"mode": "local"},
            "after_stale": "PT2H",
        }
    )
    assert released.startswith("release: `release_pump` with mode = local")
    assert released.endswith("or once data has been too old for 2 h")
    safe = _failure_words(
        {"mode": "safe_value", "action": "set_pump", "parameters": {"value": False}}
    )
    assert safe.startswith("safe value: `set_pump` with value = false")
    # A document this toolkit cannot read is shown as it came.
    assert _failure_words({"mode": "flee"}) == '{"mode": "flee"}'


def test_an_origin_that_does_not_exist_is_refused_before_the_api_is_asked(workspace) -> None:
    code, document = workspace.json("actions", "records", "--origin", "nobody")
    # As a parameter that is not name=value: the caller's mistake, not the twin's refusal.
    assert code == 2
    assert document["error"]["code"] == BAD_PARAM
    assert document["error"]["detail"] == {"origin": "nobody"}
