"""Fixtures of the fidelity tests: a live plant of its own per test, in a database of
its own on the test Dolt server."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fidelity_testkit import Plant, install_plant  # noqa: E402


@pytest.fixture
def make_plant(new_settings: Callable[[], StoreSettings]) -> Iterator[Callable[..., Plant]]:
    """Install a plant on a fresh database; closed after the test, dropped after the
    session."""
    made: list[Plant] = []

    def make(**options: Any) -> Plant:
        plant = install_plant(new_settings(), **options)
        made.append(plant)
        return plant

    yield make
    for plant in made:
        plant.twin.close()
