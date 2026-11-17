"""Fixtures of the model tests. Handlers are called as the worker calls them, with a
reader and artifacts of the test's own (`models_testkit`)."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The frame tests' twins with a known history, for the handlers that read a real store.
sys.path.append(str(Path(__file__).resolve().parents[1] / "training"))

from models_testkit import Kept  # noqa: E402


@pytest.fixture
def kept() -> Kept:
    return Kept()


@pytest.fixture
def own_code(tmp_path: Path) -> Iterator[Callable[[str, str], None]]:
    """Write a module of the twin's own where the worker would have put it: in a
    directory on `sys.path`. Forgotten after the test."""
    written: list[str] = []
    folder = tmp_path / "code"
    folder.mkdir()
    sys.path.insert(0, str(folder))

    def write(module: str, text: str) -> None:
        (folder / f"{module}.py").write_text(text, encoding="utf-8")
        written.append(module)

    yield write
    sys.path.remove(str(folder))
    for module in written:
        sys.modules.pop(module, None)
