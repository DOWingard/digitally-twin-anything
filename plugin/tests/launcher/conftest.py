"""Fixtures for running the plugin's launcher against the toolkit image under test."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# Helper modules next to this file are imported by name from the tests.
sys.path.insert(0, str(Path(__file__).parent))

import launcher_kit as kit  # noqa: E402
from sample_twin import Twin  # noqa: E402


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


@pytest.fixture
def make_twin(tmp_path: Path, home: Path) -> Iterator[Callable[..., Twin]]:
    """Makes the sample's twin; its stack, volumes and worker images go afterwards."""
    made: list[Twin] = []

    def make(*, code: bool = False) -> Twin:
        base = tmp_path / f"twin{len(made)}"
        base.mkdir()
        twin = Twin(base, home, code=code)
        made.append(twin)
        return twin

    try:
        yield make
    finally:
        left = {}
        for twin in made:
            twin.teardown()
            found = kit.leftovers(twin.project)
            if any(found.values()) or twin.worker_images():
                left[twin.project] = {**found, "worker_images": twin.worker_images()}
        assert not left, left
