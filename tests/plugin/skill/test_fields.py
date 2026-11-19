"""Every manifest field path the skill names exists in the manifest schema."""

from __future__ import annotations

from skilldocs import (
    PLACEHOLDER,
    code_exists,
    commands,
    is_manifest_path,
    markdown_files,
    parse_command,
    resolves,
    spans,
)


def test_every_field_path_in_code_spans_resolves() -> None:
    problems = []
    checked = 0
    for path in markdown_files():
        for span in spans(path):
            if not is_manifest_path(span.text):
                continue
            checked += 1
            # A dotted name that is not a field may be a code or check id of the same prefix.
            if not resolves(span.text) and not code_exists(span.text):
                problems.append(f"{span.where()}: `{span.text}`")
    assert checked > 100
    assert not problems, "\n".join(problems)


def test_every_path_given_to_confirm_resolves() -> None:
    problems = []
    for command in commands():
        parsed = parse_command(command.text)
        if parsed.command[1:] != ["manifest", "confirm"]:
            continue
        for argument in parsed.positionals:
            if argument != PLACEHOLDER and not resolves(argument):
                problems.append(f"{command.where()}: {argument}")
    assert not problems, "\n".join(problems)


def test_the_resolver_refuses_what_the_schema_lacks() -> None:
    assert resolves("contract.questions.<question>.tolerance.value")
    assert resolves("sources.<source>.options.datasets.<dataset>.glob")
    assert not resolves("contract.questions.<question>.tolerance.amount")
    assert not resolves("contract.<question>.text")
    assert not resolves("sync.interval.seconds")
