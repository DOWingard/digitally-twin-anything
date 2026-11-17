"""Fixtures of the frame tests: twins with a known history on the test Dolt server,
each built once for the module that reads it."""

from __future__ import annotations

import dataclasses
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dta_runtime.twin import Bundle, Twin, install_twin  # noqa: E402
from runtime_testkit import water_manifest  # noqa: E402
from training_testkit import T0, History, build_generated, build_small  # noqa: E402


@pytest.fixture(scope="module")
def module_twin(
    server_settings: StoreSettings, _databases: list[str], water_ontology_text: str
) -> Iterator[Callable[[], Twin]]:
    """Install the water twin on a fresh database; closed after the module, dropped
    after the session."""
    opened: list[Twin] = []

    def make() -> Twin:
        name = f"{server_settings.database}_train_{uuid.uuid4().hex[:8]}"
        _databases.append(name)
        bundle = Bundle(
            manifest=water_manifest(water_ontology_text), ontology_text=water_ontology_text
        )
        installed = install_twin(dataclasses.replace(server_settings, database=name), bundle, t=T0)
        opened.append(installed.twin)
        return installed.twin

    yield make
    for twin in opened:
        twin.close()


@pytest.fixture(scope="module")
def small(module_twin: Callable[[], Twin]) -> History:
    return build_small(module_twin())


@pytest.fixture(scope="module")
def generated(module_twin: Callable[[], Twin]) -> History:
    return build_generated(module_twin())
