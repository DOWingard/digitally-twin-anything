"""Troubleshooting covers what can fail, with codes the tool really produces: every code
it lists, every gate, every build step and every acceptance check."""

from __future__ import annotations

import re

from dta_core.manifest.checks.gates import STAGE_NAMES
from dta_toolkit.commands.build import STEPS
from skilldocs import (
    DOTTED,
    REFERENCE,
    ROOT,
    SOURCES,
    code_exists,
    is_manifest_path,
    markdown_files,
    read,
    resolves,
    spans,
    tables_headed,
    unquote,
)

TROUBLE = REFERENCE / "troubleshooting.md"
ACCEPTANCE = REFERENCE / "stage-7-acceptance.md"
_CHECK = re.compile(r"\(\s*\"([a-z]+\.[A-Za-z0-9_]+)\",\s*lambda")
# Prefixes of the keys of a command's JSON output and of a task's description.
NOT_CODES = {"result", "error", "detail", "reads", "writes"}
FILE_TYPES = {"md", "yaml", "json", "py", "env", "txt"}


def _listed_codes() -> list[str]:
    return [unquote(row[0]) for t in tables_headed(TROUBLE, "Code") for row in t.rows]


def _acceptance_checks() -> set[str]:
    text = read(SOURCES / "toolkit" / "src" / "dta_toolkit" / "acceptance" / "__init__.py")
    return set(_CHECK.findall(text))


def test_every_listed_code_is_produced_by_the_tool() -> None:
    codes = _listed_codes()
    assert len(codes) > 100
    assert len(codes) == len(set(codes)), "a code is listed twice"
    missing = [code for code in codes if not code_exists(code)]
    assert not missing, missing


def test_every_gate_has_its_failure_described() -> None:
    [table] = tables_headed(TROUBLE, "Gate")
    assert [unquote(row[0]) for row in table.rows] == list(STAGE_NAMES.values())
    assert all(row[1] and row[2] and row[3] for row in table.rows)


def test_every_build_step_has_its_failure_described() -> None:
    [table] = tables_headed(TROUBLE, "Step")
    assert [unquote(row[0]) for row in table.rows] == list(STEPS)


def test_every_acceptance_check_is_routed() -> None:
    checks = _acceptance_checks()
    assert len(checks) > 15
    [table] = tables_headed(ACCEPTANCE, "Check")
    routed = [unquote(part.strip()) for row in table.rows for part in row[0].split(",")]
    assert sorted(routed) == sorted(checks)


def _launcher_messages() -> str:
    """The launcher's source with each message whole: a message continued in the next
    quoted argument joined to it, and every shell expansion one NUL character."""
    text = read(ROOT / "bin" / "dta")
    text = re.sub(r'"\s*\\\n\s*"', " ", text)
    return re.sub(r"\$\(\([^)]*\)\)|\$\{[^}]*\}|\$[A-Za-z_]\w*", "\x00", text)


def test_every_launcher_refusal_listed_is_one_it_makes() -> None:
    [table] = tables_headed(TROUBLE, "The last line says")
    assert len(table.rows) > 10
    messages = _launcher_messages()
    missing = []
    for row in table.rows:
        # A placeholder in angle brackets stands for what the launcher fills in.
        pattern = re.sub(r"<[^<>]+>", "\x00", re.escape(unquote(row[0])))
        if not re.search(pattern, messages):
            missing.append(row[0])
    assert not missing, missing


def test_every_code_the_skill_names_is_in_troubleshooting() -> None:
    # One place says what a code means; the other files only name it. A name that is
    # also a field path reads as the field; output keys and file names are not codes.
    listed = set(_listed_codes()) | _acceptance_checks()
    unlisted = []
    for path in markdown_files():
        if path == TROUBLE:
            continue
        for span in spans(path):
            text = span.text
            if not DOTTED.match(text) or "<" in text or text in listed:
                continue
            if text.split(".")[0] in NOT_CODES or text.rsplit(".", 1)[1] in FILE_TYPES:
                continue
            if is_manifest_path(text) and resolves(text):
                continue
            if code_exists(text):
                unlisted.append(f"{span.where()}: {text}")
    assert not unlisted, "\n".join(unlisted)
