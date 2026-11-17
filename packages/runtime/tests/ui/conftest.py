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
# The live twin's sources: folders of CSV files that change between runs.
sys.path.append(str(Path(__file__).parents[1] / "sync"))

import httpx  # noqa: E402
from api_testkit import WaterHistory, build_water_history, database_name, with_database  # noqa: E402
from dta_runtime.api import create_app as create_api  # noqa: E402
from dta_runtime.settings import RuntimeSettings, UiSettings  # noqa: E402
from dta_runtime.twin import Bundle, Twin  # noqa: E402
from dta_runtime.ui import create_app as create_ui  # noqa: E402
from live_api_testkit import (  # noqa: E402
    PLANT_ENV,
    Plant,
    SwitchedClock,
    live_manifest,
    plant,
    wait_for_job,
)
from live_testkit import Folder, at  # noqa: E402
from sync_testkit import T0  # noqa: E402
from ui_compute import (  # noqa: E402
    PLAIN_LAST,
    Computed,
    prepare_computed,
    prepare_plain,
    serve_computed,
)
from ui_testkit import (  # noqa: E402
    COMPARE_LIMIT_S,
    COMPARE_S,
    DRIVER_AGENT,
    FIRST_RENDER_LIMIT_S,
    FIRST_RENDER_S,
    FOLLOW_LIMIT_S,
    FOLLOW_S,
    QUESTIONS_LIMIT_S,
    QUESTIONS_S,
    RUN_NOW_LIMIT_S,
    RUN_NOW_S,
    SWITCH_LIMIT_S,
    SWITCH_S,
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
    if FOLLOW_S:
        terminalreporter.write_line(
            f"UI new head shown to a following session (limit {FOLLOW_LIMIT_S:.0f} s): "
            f"{len(FOLLOW_S)} heads, slowest {max(FOLLOW_S):.2f} s, "
            f"each " + ", ".join(f"{seconds:.2f}" for seconds in FOLLOW_S) + " s"
        )
    if RUN_NOW_S:
        terminalreporter.write_line(
            f"UI 'Run now' answered (limit {RUN_NOW_LIMIT_S:.0f} s): "
            f"{len(RUN_NOW_S)} clicks, slowest {max(RUN_NOW_S):.2f} s"
        )
    for what, limit, measured in (
        ("questions tab first render", QUESTIONS_LIMIT_S, QUESTIONS_S),
        ("scenario comparison drawn from the click", COMPARE_LIMIT_S, COMPARE_S),
        ("open tab redrawn on a switch of branch", SWITCH_LIMIT_S, SWITCH_S),
    ):
        if measured:
            terminalreporter.write_line(
                f"UI {what} (limit {limit:.0f} s): "
                + ", ".join(f"{seconds:.2f}" for seconds in measured)
                + " s"
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


@pytest.fixture(scope="session")
def computed(
    server_settings: StoreSettings,
    _databases: list[str],
    water_text: str,
    ui_logs: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Computed]:
    """A computed twin with its models evaluated, its questions measured and its scenarios
    made and run through a real worker, served with its interface; shared, read only."""
    name = database_name(server_settings, "uicompute")
    _databases.append(name)
    root = tmp_path_factory.mktemp("computed")
    with serve_computed(
        with_database(server_settings, name),
        water_text,
        root,
        ui_logs,
        prepare=prepare_computed,
    ) as world:
        yield world


@pytest.fixture(scope="session")
def plain(
    server_settings: StoreSettings,
    _databases: list[str],
    water_text: str,
    ui_logs: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Computed]:
    """The same twin without a worker, with no model evaluated and no scenario."""
    name = database_name(server_settings, "uiplain")
    _databases.append(name)
    root = tmp_path_factory.mktemp("plain")
    with serve_computed(
        with_database(server_settings, name),
        water_text,
        root,
        ui_logs,
        worker=False,
        last=PLAIN_LAST,
        prepare=prepare_plain,
    ) as world:
        yield world


@dataclass
class LiveStack:
    """A live twin of a small plant, its API (whose clock the test moves) and its interface.

    Runs are started by the test through the API, as an operator's tool would; no
    scheduler runs. The test's own calls carry `DRIVER_AGENT` as their user agent.
    """

    twin: Twin
    clock: SwitchedClock
    plant: Plant
    folders: dict[str, Folder]
    token: str
    api_url: str
    ui_url: str
    api_requests: RequestLog
    ui_responses: ResponseLog
    log_file: Path
    driver: httpx.Client

    def health(self) -> dict[str, Any]:
        response = self.driver.get("/api/v1/meta/health")
        assert response.status_code == 200, response.text
        return response.json()

    def head_seq(self) -> int:
        return int(self.health()["head"]["seq"])

    def start(self) -> str:
        """Ask for a run of every source; the id of its job."""
        response = self.driver.post("/api/v1/sync/runs", json={})
        assert response.status_code == 202, response.text
        return str(response.json()["job"]["id"])

    def wait(self, job_id: str) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            response = self.driver.get(f"/api/v1/lifecycle/jobs/{job_id}")
            assert response.status_code == 200, response.text
            return response.json()["data"]

        return wait_for_job(read)

    def run(self) -> dict[str, Any]:
        """Start a run and wait for its job to end."""
        return self.wait(self.start())

    def runs(self, **params: Any) -> list[dict[str, Any]]:
        response = self.driver.get("/api/v1/sync/runs", params={"limit": 1000, **params})
        assert response.status_code == 200, response.text
        return response.json()["data"]


@pytest.fixture
def make_live(
    make_twin: Callable[..., Twin],
    water_text: str,
    ui_logs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., LiveStack]]:
    """Start a live twin over a new database, its API and its interface; the plant's
    sources (`registry`, `scada`, `lab`) unless others are given, and one run done."""
    root = tmp_path / "plant"
    root.mkdir()
    monkeypatch.setenv(PLANT_ENV, str(root))
    exits: list[Callable[[], None]] = []

    def make(
        sources: tuple[dict[str, Any], dict[str, Folder]] | None = None,
        *,
        questions: dict[str, Any] | None = None,
        first_run: bool = True,
    ) -> LiveStack:
        chosen, folders = sources or plant(root)
        manifest = live_manifest(water_text, chosen, questions=questions)
        twin = make_twin(Bundle(manifest=manifest, ontology_text=water_text), t=T0)
        token = secrets.token_urlsafe(32)
        clock = SwitchedClock(at(16))
        connect = Plant()
        settings = RuntimeSettings(
            store=twin.store.settings,
            api_token=token,
            artifacts_dir=tmp_path / "artifacts",
            logs_dir=ui_logs,
        )
        api_requests = RequestLog(create_api(settings, twin, clock=clock, connect=connect))
        api = ApiServer(api_requests, free_port()).__enter__()
        api_url = f"http://127.0.0.1:{api.port}"
        ui_app = create_ui(UiSettings(api_url=api_url, api_token=token, logs_dir=ui_logs))
        ui_responses = ResponseLog(ui_app.server)
        ui = UiServer(ui_responses, free_port()).__enter__()
        driver = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": DRIVER_AGENT},
            timeout=httpx.Timeout(60.0),
        )

        def stop() -> None:
            # A run still held at a gate would hold up the API's stop.
            connect.release()
            driver.close()
            ui.__exit__(None, None, None)
            api.__exit__(None, None, None)

        exits.append(stop)
        live = LiveStack(
            twin=twin,
            clock=clock,
            plant=connect,
            folders=folders,
            token=token,
            api_url=api_url,
            ui_url=f"http://127.0.0.1:{ui.port}",
            api_requests=api_requests,
            ui_responses=ui_responses,
            log_file=ui_logs / "stack.jsonl",
            driver=driver,
        )
        if first_run:
            done = live.run()
            assert done["state"] == "succeeded", done
        return live

    yield make
    for stop in reversed(exits):
        stop()


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
