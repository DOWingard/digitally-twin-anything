"""Fixtures of the MCP adapter tests: the water twin's API served over HTTP on the test
Dolt server, and the adapter served over HTTP in front of it, called with FastMCP's
own client as an agent would.

Without the `DTA_STORE_*` variables every test that needs them is skipped.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from dta_core import log as core_log
from dta_core.store import StoreSettings

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(1, str(_HERE.parent / "api"))
sys.path.append(str(_HERE.parent / "sync"))
sys.path.append(str(_HERE.parent / "notify"))
sys.path.append(str(_HERE.parent / "actions"))

from api_testkit import (  # noqa: E402
    TOKEN,
    WaterHistory,
    build_water_history,
    database_name,
    with_database,
)
from dta_runtime.api import create_app  # noqa: E402
from dta_runtime.clock import FixedClock  # noqa: E402
from dta_runtime.settings import RuntimeSettings  # noqa: E402
from dta_runtime.twin import Bundle, Twin  # noqa: E402
from live_api_testkit import PLANT_ENV, Plant, http_client, live_manifest  # noqa: E402
from live_api_testkit import served as serve  # noqa: E402
from live_testkit import at  # noqa: E402
from mcp_testkit import Served, serve_adapter  # noqa: E402
from sync_testkit import T0  # noqa: E402

WATER_ONTOLOGY = _HERE.parents[2] / "core/tests/ontology/fixtures/water_network.yaml"


@pytest.fixture(scope="session")
def water_text() -> str:
    return WATER_ONTOLOGY.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def water_history(
    server_settings: StoreSettings, _databases: list[str], water_text: str
) -> Iterator[WaterHistory]:
    name = database_name(server_settings, "mcp")
    _databases.append(name)
    history = build_water_history(with_database(server_settings, name), water_text)
    yield history
    history.twin.close()


@pytest.fixture(scope="session")
def water(
    water_history: WaterHistory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Served]:
    """The water twin's API over HTTP, and the adapter in front of it."""
    settings = RuntimeSettings(
        store=water_history.settings,
        api_token=TOKEN,
        artifacts_dir=tmp_path_factory.mktemp("artifacts"),
        logs_dir=tmp_path_factory.mktemp("logs"),
    )
    with contextlib.ExitStack() as stack:
        api_url = stack.enter_context(serve(create_app(settings)))
        mcp_url, names = stack.enter_context(serve_adapter(api_url))
        client = stack.enter_context(http_client(api_url))
        yield Served(api_url=api_url, mcp_url=mcp_url, api=client, tools=names)


@pytest.fixture
def plant_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "plant"
    path.mkdir()
    monkeypatch.setenv(PLANT_ENV, str(path))
    return path


@pytest.fixture
def live(
    make_twin: Callable[..., Twin], water_text: str, plant_dir: Path, tmp_path: Path
) -> Iterator[Callable[[dict[str, Any]], Served]]:
    """A fresh live twin with the given sources, its API over HTTP and the adapter."""
    stack = contextlib.ExitStack()
    plants: list[Plant] = []

    def make(sources: dict[str, Any]) -> Served:
        manifest = live_manifest(water_text, sources)
        twin = make_twin(Bundle(manifest=manifest, ontology_text=water_text), t=T0)
        settings = RuntimeSettings(
            store=twin.store.settings,
            api_token=TOKEN,
            artifacts_dir=tmp_path / "artifacts",
            logs_dir=tmp_path / "logs",
        )
        plants.append(Plant())
        app = create_app(settings, twin, clock=FixedClock(at(16)), connect=plants[-1])
        api_url = stack.enter_context(serve(app))
        mcp_url, names = stack.enter_context(serve_adapter(api_url))
        client = stack.enter_context(http_client(api_url))
        return Served(api_url=api_url, mcp_url=mcp_url, api=client, tools=names)

    with stack:
        yield make
        for plant in plants:
            plant.release()


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
