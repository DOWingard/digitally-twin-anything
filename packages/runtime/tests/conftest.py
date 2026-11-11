"""Fixtures shared by every test of the runtime.

Tests that need the database get a twin of their own on the Dolt server named
by the `DTA_STORE_*` environment variables, dropped afterwards; without those
variables such tests are skipped.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pymysql
import pytest
import yaml

# Helper modules next to this file are imported by name from the tests.
sys.path.insert(0, str(Path(__file__).parent))

from dta_core.manifest import Manifest, parse_manifest  # noqa: E402
from dta_core.ontology import parse_ontology, type_signature  # noqa: E402
from dta_core.store import StoreSettings  # noqa: E402
from dta_core.store.settings import ENV_NAMES  # noqa: E402
from dta_runtime.twin import Bundle, Twin, install_twin  # noqa: E402

WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[2] / "core/tests/ontology/fixtures/water_network.yaml"
)
T0 = datetime(2026, 1, 5, tzinfo=UTC)
_SESSION = uuid.uuid4().hex[:6]


@pytest.fixture(scope="session")
def server_settings() -> StoreSettings:
    """Settings of the test server; the database name is only a prefix here."""
    missing = [name for name in ENV_NAMES if not os.environ.get(name)]
    if missing:
        pytest.skip("needs a Dolt server: set " + ", ".join(missing))
    return StoreSettings.from_env()


@pytest.fixture(scope="session")
def _databases(server_settings: StoreSettings) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    connection = pymysql.connect(
        host=server_settings.host,
        port=server_settings.port,
        user=server_settings.user,
        password=server_settings.password,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            for name in created:
                cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
    finally:
        connection.close()


@pytest.fixture
def new_settings(
    server_settings: StoreSettings, _databases: list[str]
) -> Callable[[], StoreSettings]:
    """Settings for a database that does not exist yet; each call names a new one."""

    def make() -> StoreSettings:
        name = f"{server_settings.database}_rt_{_SESSION}_{len(_databases)}"
        _databases.append(name)
        return dataclasses.replace(server_settings, database=name)

    return make


@pytest.fixture(scope="session")
def water_ontology_text() -> str:
    return WATER_ONTOLOGY.read_text(encoding="utf-8")


def water_manifest(ontology_text: str, **changes: object) -> Manifest:
    """A small manifest for the water network; `changes` replace top-level sections."""
    document: dict[str, object] = {
        "twin": {"id": "water", "name": "Water network", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/water_network.yaml",
            "type_signature": type_signature(parse_ontology(ontology_text).ontology),
        },
    }
    document.update(changes)
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


@pytest.fixture
def water_bundle(water_ontology_text: str) -> Bundle:
    return Bundle(manifest=water_manifest(water_ontology_text), ontology_text=water_ontology_text)


@pytest.fixture
def make_twin(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
) -> Iterator[Callable[..., Twin]]:
    """Install a twin on a fresh database; closed and dropped after the test."""
    opened: list[Twin] = []

    def make(bundle: Bundle | None = None, t: datetime = T0) -> Twin:
        installed = install_twin(new_settings(), bundle or water_bundle, t=t)
        opened.append(installed.twin)
        return installed.twin

    yield make
    for twin in opened:
        twin.close()


@pytest.fixture
def twin(make_twin: Callable[..., Twin]) -> Twin:
    return make_twin()
