"""SKILL.md: a frontmatter the agent can be found by, a map short enough to read whole,
and a stage table that matches the tool's gates."""

from __future__ import annotations

import re

import yaml
from dta_core.manifest.checks.gates import STAGE_NAMES
from skilldocs import SKILL_MD, frontmatter, links, read, tables_headed, unquote

MAX_LINES = 500
MAX_TRIGGER_CHARS = 1536


def test_frontmatter_names_the_skill_and_fits() -> None:
    raw, _ = frontmatter(SKILL_MD)
    data = yaml.safe_load(raw)
    assert data["name"] == "build-twin"
    trigger = str(data.get("description", "")) + str(data.get("when_to_use", ""))
    assert trigger.strip()
    assert len(trigger) <= MAX_TRIGGER_CHARS, len(trigger)


def test_skill_md_is_short_enough_to_read_whole() -> None:
    assert len(read(SKILL_MD).splitlines()) <= MAX_LINES


def test_stage_table_follows_the_gates() -> None:
    [table] = tables_headed(SKILL_MD, "Stage")
    assert table.header == ["Stage", "Purpose", "Reference", "Commands", "Gate"]
    assert [int(row[0]) for row in table.rows] == sorted(STAGE_NAMES)
    for row in table.rows:
        stage = int(row[0])
        assert unquote(row[4]) == STAGE_NAMES[stage], row
        targets = re.findall(r"\]\(([^)]+)\)", row[2])
        assert len(targets) == 1, row
        assert re.fullmatch(rf"reference/stage-{stage}-[a-z-]+\.md", targets[0]), targets
        assert (SKILL_MD.parent / targets[0]).is_file(), targets[0]
        assert re.findall(r"`(dta [^`]+)`", row[3]), row


def test_stage_references_are_linked_outside_the_table_too() -> None:
    linked = {link.text for link in links(SKILL_MD)}
    for stage in STAGE_NAMES:
        assert any(re.fullmatch(rf"reference/stage-{stage}-[a-z-]+\.md", t) for t in linked)
