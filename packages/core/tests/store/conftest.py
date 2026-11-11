"""Fixtures of the store tests. Every test that needs the database gets its own,
created on the Dolt server named by the `DTA_STORE_*` environment variables
and dropped afterwards; without those variables such tests are skipped."""

from __future__ import annotations

import dataclasses
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dta_core.errors import ConfigError  # noqa: E402
from dta_core.schema import Ontology  # noqa: E402
from dta_core.store import Store, StoreSettings  # noqa: E402
from dta_core.store.settings import ENV_NAMES  # noqa: E402
from dta_store_testkit import raw_connection, sql, water_ontology  # noqa: E402

SCALE = "scale"
_SESSION = uuid.uuid4().hex[:6]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{SCALE}: loads the full-size fixtures and asserts the performance thresholds; "
        f"run with -m {SCALE}",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if SCALE in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason=f"full-size fixture; run with -m {SCALE}")
    for item in items:
        if SCALE in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def server_settings() -> StoreSettings:
    """Settings of the test server; the database name is only a prefix here."""
    missing = [name for name in ENV_NAMES if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "needs a Dolt server: set "
            + ", ".join(missing)
            + " (see packages/core/tests/store/compose.yaml)"
        )
    try:
        return StoreSettings.from_env()
    except ConfigError as exc:
        pytest.skip(f"store settings are unusable: {exc}")


@pytest.fixture(scope="session")
def _databases(server_settings: StoreSettings) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    connection = raw_connection(server_settings, branch=None)
    try:
        for name in created:
            sql(connection, f"DROP DATABASE IF EXISTS `{name}`")
    finally:
        connection.close()


@pytest.fixture
def new_settings(
    server_settings: StoreSettings, _databases: list[str]
) -> Callable[[], StoreSettings]:
    """Settings for a database that does not exist yet; each call names a new one."""

    def make() -> StoreSettings:
        name = f"{server_settings.database}_{_SESSION}_{len(_databases)}"
        _databases.append(name)
        return dataclasses.replace(server_settings, database=name, lock_timeout_s=20.0)

    return make


@pytest.fixture
def make_store(new_settings: Callable[[], StoreSettings]) -> Iterator[Callable[..., Store]]:
    """Build an installed store on a fresh database; closed and dropped after the test."""
    opened: list[Store] = []

    def make(ontology: Ontology | None = None, install: bool = True, **kwargs: object) -> Store:
        store = Store(new_settings(), ontology or water_ontology(), **kwargs)
        opened.append(store)
        if install:
            store.install("twin-under-test")
        return store

    yield make
    for store in opened:
        store.close()


@pytest.fixture
def store(make_store: Callable[..., Store]) -> Store:
    return make_store()
