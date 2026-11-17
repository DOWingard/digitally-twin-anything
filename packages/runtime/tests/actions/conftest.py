"""Fixtures of the action tests: a water twin with controls in a database of its own on
the test Dolt server, and a plant answering over HTTP on a local port."""

from __future__ import annotations

import dataclasses
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))

from actions_testkit import Plant, World, build_world  # noqa: E402


@pytest.fixture(scope="module")
def make_world(
    server_settings: StoreSettings, _databases: list[str], water_ontology_text: str
) -> Iterator[Callable[[], World]]:
    """Build the tests' world in a fresh database; closed after the module, dropped
    after the session."""
    made: list[World] = []

    def make() -> World:
        name = f"{server_settings.database}_act_{uuid.uuid4().hex[:8]}"
        _databases.append(name)
        world = build_world(
            dataclasses.replace(server_settings, database=name), water_ontology_text
        )
        made.append(world)
        return world

    yield make
    for world in made:
        world.twin.close()


@pytest.fixture(scope="module")
def world(make_world: Callable[[], World]) -> World:
    """One world for the tests of a module."""
    return make_world()


@pytest.fixture(scope="module")
def plant() -> Iterator[Plant]:
    found = Plant()
    yield found
    found.stop()


@pytest.fixture
def fresh_plant(plant: Plant) -> Plant:
    plant.reset()
    return plant
