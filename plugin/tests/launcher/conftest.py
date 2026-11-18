"""Fixtures for running the plugin's launcher against the toolkit image under test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Helper modules next to this file are imported by name from the tests.
sys.path.insert(0, str(Path(__file__).parent))

import launcher_kit as kit  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The launcher's HOME: inside the test's directory, never the real one."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def env(home: Path) -> dict[str, str]:
    """The launcher's environment for one test."""
    return kit.environment(home)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """An empty working directory; its physical path, as the launcher mounts it."""
    path = tmp_path / "work"
    path.mkdir()
    return path.resolve()
