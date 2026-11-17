"""Fixtures of the model tests. Handlers are called as the worker calls them, with a
reader and artifacts of the test's own (`models_testkit`)."""

from __future__ import annotations

import dataclasses
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The frame tests' rows and commits, for the handlers that read a real store.
sys.path.append(str(Path(__file__).resolve().parents[1] / "training"))

from dta_runtime.twin import Bundle, Twin, install_twin  # noqa: E402
from models_testkit import Kept  # noqa: E402
from runtime_testkit import water_manifest  # noqa: E402
from training_testkit import T0  # noqa: E402


@pytest.fixture
def kept() -> Kept:
    return Kept()


@pytest.fixture
def own_code(tmp_path: Path) -> Iterator[Callable[[str, str], None]]:
    """Write a module of the twin's own where the worker would have put it: in a
    directory on `sys.path`. Forgotten after the test."""
    written: list[str] = []
    folder = tmp_path / "code"
    folder.mkdir()
    sys.path.insert(0, str(folder))

    def write(module: str, text: str) -> None:
        (folder / f"{module}.py").write_text(text, encoding="utf-8")
        written.append(module)

    yield write
    sys.path.remove(str(folder))
    for module in written:
        sys.modules.pop(module, None)


@pytest.fixture(scope="module")
def water_twin(
    server_settings: StoreSettings, _databases: list[str], water_ontology_text: str
) -> Iterator[Twin]:
    """The water twin on a fresh database of the test Dolt server; closed after the
    module, dropped after the session."""
    name = f"{server_settings.database}_models_{uuid.uuid4().hex[:8]}"
    _databases.append(name)
    bundle = Bundle(manifest=water_manifest(water_ontology_text), ontology_text=water_ontology_text)
    installed = install_twin(dataclasses.replace(server_settings, database=name), bundle, t=T0)
    yield installed.twin
    installed.twin.close()
