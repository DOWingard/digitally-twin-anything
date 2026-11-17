"""Fixtures of the scenario tests: a twin with a known history and registered models,
in a database of its own on the test Dolt server."""

from __future__ import annotations

import dataclasses
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios_testkit import PLANT_MODULE, InProcess, World, build_world, write_plant_code  # noqa: E402


@pytest.fixture(scope="session")
def plant_code(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """The twin's own family, importable by its entry for the whole session."""
    folder = tmp_path_factory.mktemp("scenario_code")
    write_plant_code(folder)
    sys.path.insert(0, str(folder))
    yield
    sys.path.remove(str(folder))
    sys.modules.pop(PLANT_MODULE, None)


@pytest.fixture(scope="module")
def make_world(
    server_settings: StoreSettings,
    _databases: list[str],
    water_ontology_text: str,
    plant_code: None,
) -> Iterator[Callable[[], World]]:
    """Build the tests' world in a fresh database; closed after the module, dropped
    after the session."""
    made: list[World] = []

    def make() -> World:
        name = f"{server_settings.database}_scn_{uuid.uuid4().hex[:8]}"
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
    """One world for the tests of a module: each makes scenarios of its own name on it."""
    return make_world()


@pytest.fixture
def predictor(world: World) -> InProcess:
    return InProcess(world)
