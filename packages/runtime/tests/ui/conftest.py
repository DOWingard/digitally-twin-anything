"""Fixtures of the interface's tests: a real API over a real Dolt store, the interface
served as the runtime serves it, and a headless Chrome driven by Selenium.

Tests that need the store are skipped without the `DTA_STORE_*` variables; tests
that need a browser are skipped when Chrome is not installed.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from dta_core import log as core_log
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "api"))

from api_testkit import WaterHistory, build_water_history, database_name, with_database  # noqa: E402
from dta_runtime.api import create_app as create_api  # noqa: E402
from dta_runtime.settings import RuntimeSettings, UiSettings  # noqa: E402
from dta_runtime.ui import create_app as create_ui  # noqa: E402
from ui_testkit import (  # noqa: E402
    FIRST_RENDER_LIMIT_S,
    FIRST_RENDER_S,
    ApiServer,
    RequestLog,
    ResponseLog,
    UiServer,
    extend_history,
    free_port,
)

CHROME = Path("/usr/bin/google-chrome-stable")
WATER_ONTOLOGY = (
    Path(__file__).resolve().parents[3] / "core/tests/ontology/fixtures/water_network.yaml"
)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if FIRST_RENDER_S:
        measured = ", ".join(f"{tab} {seconds:.2f} s" for tab, seconds in FIRST_RENDER_S.items())
        terminalreporter.write_line(
            f"UI first render (limit {FIRST_RENDER_LIMIT_S:.0f} s each): {measured}"
        )


@dataclass
class Stack:
    """A twin's API and its interface, both served on loopback ports."""

    history: WaterHistory
    token: str
    api_url: str
    ui_url: str
    api_requests: RequestLog
    ui_responses: ResponseLog
    log_file: Path


@pytest.fixture(scope="session")
def water_text() -> str:
    return WATER_ONTOLOGY.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ui_logs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The process's log lines, written as the stack's services write theirs.

    The API serves this directory's lines under `/lifecycle/logs`; the logging
    set-up the run had before is restored afterwards.
    """
    directory = tmp_path_factory.mktemp("ui-logs")
    root = logging.getLogger("dta")
    saved = (list(root.handlers), root.propagate, root.level)
    core_log.configure(log_file=directory / "stack.jsonl", level="info")
    try:
        yield directory
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        handlers, propagate, level = saved
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)


@pytest.fixture(scope="session")
def make_stack(
    server_settings: StoreSettings,
    _databases: list[str],
    water_text: str,
    ui_logs: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callable[[], Stack]]:
    """Start a stack over a new database holding the water history and its extension."""
    exits: list[Callable[[], None]] = []

    def make() -> Stack:
        name = database_name(server_settings, "ui")
        _databases.append(name)
        history = build_water_history(with_database(server_settings, name), water_text)
        extend_history(history)
        token = secrets.token_urlsafe(32)
        api_requests = RequestLog(
            create_api(
                RuntimeSettings(
                    store=history.settings,
                    api_token=token,
                    artifacts_dir=tmp_path_factory.mktemp("artifacts"),
                    logs_dir=ui_logs,
                )
            )
        )
        api = ApiServer(api_requests, free_port()).__enter__()
        api_url = f"http://127.0.0.1:{api.port}"
        ui_app = create_ui(UiSettings(api_url=api_url, api_token=token, logs_dir=ui_logs))
        ui_responses = ResponseLog(ui_app.server)
        ui = UiServer(ui_responses, free_port()).__enter__()

        def stop() -> None:
            ui.__exit__(None, None, None)
            api.__exit__(None, None, None)
            history.twin.close()

        exits.append(stop)
        return Stack(
            history=history,
            token=token,
            api_url=api_url,
            ui_url=f"http://127.0.0.1:{ui.port}",
            api_requests=api_requests,
            ui_responses=ui_responses,
            log_file=ui_logs / "stack.jsonl",
        )

    yield make
    for stop in reversed(exits):
        stop()


@pytest.fixture(scope="session")
def stack(make_stack: Callable[[], Stack]) -> Stack:
    """The stack the read-only tests share; tests that write versions start their own."""
    return make_stack()


@pytest.fixture
def browser() -> Iterator[Any]:
    """A headless Chrome, with its network log kept, closed after the test."""
    if not CHROME.exists() and shutil.which("google-chrome-stable") is None:
        pytest.skip(f"needs Chrome at {CHROME} for the browser tests")
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException

    options = webdriver.ChromeOptions()
    options.binary_location = str(CHROME)
    for argument in ("--headless=new", "--window-size=1600,1100", "--disable-gpu"):
        options.add_argument(argument)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as exc:
        pytest.skip(f"Chrome cannot be driven: {exc.msg}")
    try:
        yield driver
    finally:
        driver.quit()
