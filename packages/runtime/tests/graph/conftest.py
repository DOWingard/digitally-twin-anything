"""Fixtures of the graph tests: the water network's history from the API tests' kit,
written to a database of its own on the test Dolt server."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_testkit import (  # noqa: E402
    WaterHistory,
    build_water_history,
    database_name,
    with_database,
)
from graph_testkit import Records  # noqa: E402

WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
)


@pytest.fixture(scope="session")
def graph_history(server_settings: StoreSettings, _databases: list[str]) -> Iterator[WaterHistory]:
    name = database_name(server_settings, "graph")
    _databases.append(name)
    history = build_water_history(
        with_database(server_settings, name), WATER_ONTOLOGY.read_text(encoding="utf-8")
    )
    yield history
    history.twin.close()


@pytest.fixture
def logged() -> Iterator[Records]:
    logger = logging.getLogger("dta")
    handler = Records()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
