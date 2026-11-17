"""Fixtures of the function tests: twins with known histories, each in a database of
its own on the test Dolt server."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_testkit import database_name, with_database  # noqa: E402
from functions_testkit import History, build_generated, build_story  # noqa: E402

WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
)


@pytest.fixture(scope="session")
def story(server_settings: StoreSettings, _databases: list[str]) -> Iterator[History]:
    """The hand-made history every expected value of these tests is worked out from."""
    name = database_name(server_settings, "fn_story")
    _databases.append(name)
    history = build_story(
        with_database(server_settings, name), WATER_ONTOLOGY.read_text(encoding="utf-8")
    )
    yield history
    history.twin.close()


@pytest.fixture(scope="session")
def generated(server_settings: StoreSettings, _databases: list[str]) -> Iterator[History]:
    name = database_name(server_settings, "fn_generated")
    _databases.append(name)
    history = build_generated(
        with_database(server_settings, name), WATER_ONTOLOGY.read_text(encoding="utf-8")
    )
    yield history
    history.twin.close()
