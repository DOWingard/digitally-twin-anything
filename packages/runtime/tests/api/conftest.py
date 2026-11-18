"""Fixtures of the API tests: the water network's history on the test Dolt server,
and the API serving it, called in process; and live twins whose sources are
folders of CSV files, synced through the API.

Without the `DTA_STORE_*` variables every test that needs them is skipped.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from dta_core import log as core_log
from dta_core.store import StoreSettings

sys.path.insert(0, str(Path(__file__).parent))
# The sync tests' sources that change between runs, used as they are.
sys.path.append(str(Path(__file__).resolve().parents[1] / "sync"))
# The notification tests' webhook and the ports this package's servers may listen on.
sys.path.append(str(Path(__file__).resolve().parents[1] / "notify"))
# The action library tests' twin with controls, and the plant its executors call.
sys.path.append(str(Path(__file__).resolve().parents[1] / "actions"))

from actions_api_testkit import Acting, own_port, serve_acting  # noqa: E402
from api_testkit import (  # noqa: E402
    TOKEN,
    Api,
    WaterHistory,
    build_water_history,
    database_name,
    with_database,
)
from compute_testkit import T0 as COMPUTE_T0  # noqa: E402
from compute_testkit import (  # noqa: E402
    Small,
    World,
    plant_code,
    serve_world,
    write_history,
)
from compute_testkit import at as compute_at  # noqa: E402
from compute_testkit import manifest_for as compute_manifest  # noqa: E402
from compute_testkit import runtime_settings as compute_settings  # noqa: E402
from dta_runtime.api import create_app  # noqa: E402
from dta_runtime.clock import FixedClock  # noqa: E402
from dta_runtime.settings import RuntimeSettings  # noqa: E402
from dta_runtime.twin import Bundle, Twin  # noqa: E402
from fidelity_api_testkit import Checked, serve_checked  # noqa: E402
from live_api_testkit import (  # noqa: E402
    PLANT_ENV,
    LiveApi,
    Plant,
    http_client,
    live_manifest,
)
from live_api_testkit import served as serve  # noqa: E402
from live_testkit import LIVE, at  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from sync_testkit import T0  # noqa: E402
from webhook_testkit import webhook  # noqa: E402

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


@pytest.fixture
def plant_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the plant's source folders are; its sources name it through a variable."""
    path = tmp_path / "plant"
    path.mkdir()
    monkeypatch.setenv(PLANT_ENV, str(path))
    return path


@pytest.fixture
def live_api(
    make_twin: Callable[..., Twin], water_text: str, plant_dir: Path, tmp_path: Path
) -> Iterator[Callable[..., LiveApi]]:
    """A fresh twin whose manifest holds the given sources, behind an API of its own whose
    clock is the test's; called in process, or served over HTTP by uvicorn with `served`.
    Stopped before the twin is closed and dropped."""
    started: list[tuple[LiveApi, contextlib.ExitStack]] = []

    def make(
        sources: Mapping[str, Any],
        *,
        sync: Mapping[str, Any] | None = LIVE,
        tier: str = "live",
        clock: FixedClock | None = None,
        served: bool = False,
        questions: Mapping[str, Any] | None = None,
    ) -> LiveApi:
        manifest = live_manifest(water_text, sources, sync=sync, tier=tier, questions=questions)
        twin = make_twin(Bundle(manifest=manifest, ontology_text=water_text), t=T0)
        settings = RuntimeSettings(
            store=twin.store.settings,
            api_token=TOKEN,
            artifacts_dir=tmp_path / "artifacts",
            logs_dir=tmp_path / "logs",
        )
        plant = Plant()
        chosen = clock or FixedClock(at(16))
        app = create_app(settings, twin, clock=chosen, connect=plant)
        stack = contextlib.ExitStack()
        if served:
            base_url = stack.enter_context(serve(app))
            client: httpx.Client = stack.enter_context(http_client(base_url))
        else:
            client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        live = LiveApi(
            client=client, twin=twin, clock=chosen, plant=plant, settings=settings, app=app
        )
        started.append((live, stack))
        return live

    yield make
    for live, stack in reversed(started):
        # A run still held at a gate would hold up the API's stop.
        live.plant.release()
        stack.close()


@pytest.fixture(scope="session")
def world(
    server_settings: StoreSettings,
    _databases: list[str],
    water_text: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[World]:
    """The computed twin with its history, its API served over HTTP and a worker beside
    it; nothing is registered yet."""
    name = database_name(server_settings, "compute")
    _databases.append(name)
    root = tmp_path_factory.mktemp("compute")
    with serve_world(with_database(server_settings, name), water_text, root) as found:
        yield found


@pytest.fixture(scope="session")
def registered(world: World) -> dict[str, Any]:
    """The world with its code kept and its registries in line with its manifest: what
    the registration answered."""
    world.code = world.upload(plant_code())
    return world.register(world.code)


@pytest.fixture(scope="session")
def evaluated(world: World, registered: dict[str, Any]) -> dict[str, Any]:
    """The world with every model specification evaluated once: the job's result."""
    return world.done(
        world.post("/models/evaluate", json={}, headers={"Idempotency-Key": "evaluate-all"})
    )


@pytest.fixture
def checked(
    new_settings: Callable[[], StoreSettings], water_text: str, tmp_path: Path
) -> Iterator[Callable[..., Checked]]:
    """A fresh live twin whose fidelity is checked through its API, with a worker beside
    it and its models evaluated; stopped and dropped after the test."""
    stack = contextlib.ExitStack()
    made: list[Checked] = []

    def make(**options: Any) -> Checked:
        root = tmp_path / f"checked-{len(made)}"
        made.append(stack.enter_context(serve_checked(new_settings(), water_text, root, **options)))
        return made[-1]

    with stack:
        yield make


@pytest.fixture
def small(
    make_twin: Callable[..., Twin], water_text: str, tmp_path: Path
) -> Iterator[Callable[..., Small]]:
    """A fresh computed twin behind an API called in process, without a worker unless
    it is told where one is; with `history`, the first steps of the network's history
    are written."""
    stack = contextlib.ExitStack()

    def make(history: int = 0, worker_url: str | None = None, **sections: Any) -> Small:
        manifest = compute_manifest(water_text, **sections)
        twin = make_twin(Bundle(manifest=manifest, ontology_text=water_text), t=COMPUTE_T0)
        if history:
            write_history(twin, history)
        settings = compute_settings(twin.store.settings, tmp_path, worker_url)
        app = create_app(settings, twin, clock=FixedClock(compute_at(history)))
        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        return Small(client=client, twin=twin, app=app, ontology_text=water_text)

    with stack:
        yield make


@pytest.fixture
def acting(
    new_settings: Callable[[], StoreSettings],
    water_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Acting]]:
    """A fresh twin with controls behind its API, a worker and the plant it acts on;
    with `webhook`, its notices go to a webhook of the test's. Stopped and dropped after
    the test."""
    stack = contextlib.ExitStack()
    made: list[Acting] = []

    def make(*, with_webhook: bool = False, **options: Any) -> Acting:
        root = tmp_path / f"acting-{len(made)}"
        receiver = stack.enter_context(webhook(own_port())) if with_webhook else None
        world = serve_acting(
            new_settings(),
            water_text,
            root,
            monkeypatch.setenv,
            webhook_receiver=receiver,
            **options,
        )
        made.append(stack.enter_context(world))
        return made[-1]

    with stack:
        yield make
