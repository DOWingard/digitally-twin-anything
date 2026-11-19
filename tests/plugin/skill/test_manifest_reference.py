"""The skill's manifest reference is the one the schema renders today."""

from __future__ import annotations

import re

from manifest_reference import RECORDED_ONLY, REGENERATE, TARGET, render
from skilldocs import source_texts


def test_manifest_reference_is_current() -> None:
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    assert current == render(), (
        f"{TARGET.name} differs from the schema; regenerate it from the repository root "
        f"with `{REGENERATE}`"
    )


def test_recorded_only_fields_are_still_not_acted_on() -> None:
    # The reference tells the agent these fields build nothing; the first code that reads
    # one makes that untrue, and the list in the generator must lose it.
    readers = []
    for path in RECORDED_ONLY:
        name = path.rsplit(".", 1)[1]
        pattern = re.compile(rf"\b{name}\b")
        for source, text in source_texts().items():
            if source.name == "models.py" and source.parent.name == "manifest":
                continue
            if pattern.search(text):
                readers.append(f"{path}: {source}")
    assert not readers, "\n".join(readers)
