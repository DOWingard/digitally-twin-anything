"""Fixtures of the API tests: the water network's history on the test Dolt server,
and the API serving it, called in process.

Without the `DTA_STORE_*` variables every test that needs them is skipped.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dta_core import log as core_log
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).parent))

from api_testkit import (  # noqa: E402
    TOKEN,
    Api,
    WaterHistory,
    build_water_history,
    database_name,
    with_database,
)
from dta_runtime.api import create_app  # noqa: E402
from dta_runtime.settings import RuntimeSettings  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
)


@pytest.fixture(scope="session")
def water_text() -> str:
    return WATER_ONTOLOGY.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def water_history(
    server_settings: StoreSettings, _databases: list[str], water_text: str
) -> Iterator[WaterHistory]:
    name = database_name(server_settings, "api")
    _databases.append(name)
    history = build_water_history(with_database(server_settings, name), water_text)
    yield history
    history.twin.close()


@pytest.fixture(scope="session")
def logs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("logs")


@pytest.fixture(scope="session")
def runtime_settings(
    water_history: WaterHistory, logs_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> RuntimeSettings:
    return RuntimeSettings(
        store=water_history.settings,
        api_token=TOKEN,
        artifacts_dir=tmp_path_factory.mktemp("artifacts"),
        logs_dir=logs_dir,
    )


@pytest.fixture(scope="session")
def app_client(runtime_settings: RuntimeSettings) -> Iterator[TestClient]:
    """The API of the water twin, started once; requests carry no token by themselves."""
    with TestClient(create_app(runtime_settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="session")
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="session")
def api(app_client: TestClient, auth: dict[str, str]) -> Api:
    return Api(app_client, auth)


@pytest.fixture
def log_to() -> Iterator[Callable[[Path], Path]]:
    """Send the process's log lines, as JSON, to a file of the test's choosing."""
    root = logging.getLogger("dta")
    saved = (list(root.handlers), root.propagate, root.level)

    def start(path: Path) -> Path:
        core_log.configure(log_file=path, level="debug")
        return path

    try:
        yield start
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        handlers, propagate, level = saved
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)


@pytest.fixture
def log_lines(log_to: Callable[[Path], Path], tmp_path: Path) -> Path:
    """Every line the process logs during the test, written as JSON to the returned file."""
    return log_to(tmp_path / "test.jsonl")
