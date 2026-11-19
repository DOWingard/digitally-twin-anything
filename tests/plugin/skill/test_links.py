"""The skill is one connected text: every link resolves, every file is reachable from
SKILL.md, and nothing in it points outside the plugin."""

from __future__ import annotations

import re
from pathlib import Path

from skilldocs import PLUGIN, SKILL, SKILL_MD, links, markdown_files, read, skill_files

# What the plugin must never mention: the repository it is built in, how it is built
# and checked, and paths on the machine it was written on.
FORBIDDEN = {
    "the repository's working notes": re.compile(r"CONTEXT/|\bPLANS\b"),
    "a test bed": re.compile(r"\btest ?bed\b", re.IGNORECASE),
    "a milestone": re.compile(r"\bmilestones?\b", re.IGNORECASE),
    "a milestone id": re.compile(r"\bM\d{1,2}\b"),
    "tests": re.compile(r"\btests?\b", re.IGNORECASE),
    "the tool's source tree": re.compile(r"\bpackages/"),
    "a home directory": re.compile(r"/home/|~/"),
    "an agent configuration directory": re.compile(r"\.claude/"),
    "a worktree": re.compile(r"\bworktrees?\b", re.IGNORECASE),
}


def _external(target: str) -> bool:
    return bool(re.match(r"^[a-z]+://", target)) or target.startswith(("#", "mailto:"))


def _resolve(source: Path, target: str) -> Path:
    return (source.parent / target.split("#", 1)[0]).resolve()


def test_every_relative_link_resolves() -> None:
    broken = []
    for path in markdown_files():
        for link in links(path):
            if not _external(link.text) and not _resolve(path, link.text).exists():
                broken.append(f"{link.where()}: {link.text}")
    assert not broken, "\n".join(broken)


def test_every_file_is_reachable_from_skill_md() -> None:
    reached: set[Path] = set()
    queue = [SKILL_MD.resolve()]
    while queue:
        current = queue.pop()
        if current in reached:
            continue
        reached.add(current)
        if current.suffix == ".md":
            for link in links(current):
                if not _external(link.text):
                    target = _resolve(current, link.text)
                    if target.is_file() and target.is_relative_to(SKILL.resolve()):
                        queue.append(target)
    unreached = sorted(
        str(path.relative_to(SKILL)) for path in skill_files() if path.resolve() not in reached
    )
    assert not unreached, unreached


def test_nothing_points_outside_the_plugin() -> None:
    texts = [*skill_files(), *sorted((PLUGIN / ".claude-plugin").glob("*.json"))]
    assert len(texts) > len(skill_files()) + 1
    found = []
    for path in texts:
        for number, line in enumerate(read(path).splitlines(), start=1):
            for what, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(PLUGIN)}:{number}: {what}: {line.strip()}")
    assert not found, "\n".join(found)
