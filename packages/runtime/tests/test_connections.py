"""No request of the stack is reset by a server closing an idle connection: every client
gives an idle connection up before the server it talks to would."""

from __future__ import annotations

import random
import secrets
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from dta_runtime import cli
from dta_runtime.connections import CLIENT_KEEP_ALIVE_S, SERVER_KEEP_ALIVE_S, client_limits
from dta_runtime.scheduler import Scheduler
from dta_runtime.settings import SchedulerSettings
from dta_runtime.ui.client import ApiClient
from typer.testing import CliRunner

# What httpx keeps an idle connection for when a client is made without limits.
HTTPX_DEFAULT_S = 5.0


def keepalive_expiry(client: httpx.Client) -> float:
    """How long the client's pool keeps an idle connection (httpx holds it privately)."""
    return client._transport._pool._keepalive_expiry  # noqa: SLF001


def test_clients_give_a_connection_up_before_servers_close_it():
    assert CLIENT_KEEP_ALIVE_S < SERVER_KEEP_ALIVE_S
    # A client made without the stack's limits is safe as well.
    assert HTTPX_DEFAULT_S < SERVER_KEEP_ALIVE_S
    assert httpx.Limits().keepalive_expiry == HTTPX_DEFAULT_S
    limits = client_limits()
    assert limits.keepalive_expiry == CLIENT_KEEP_ALIVE_S
    assert limits.max_connections is not None and limits.max_keepalive_connections


def test_the_interface_and_the_scheduler_use_the_clients_limits(tmp_path: Path):
    token = secrets.token_urlsafe(32)
    interface = ApiClient("http://127.0.0.1:9", token)
    scheduler = Scheduler(
        SchedulerSettings(
            api_url="http://127.0.0.1:9", api_token=token, logs_dir=tmp_path, poll_s=1.0
        )
    )
    try:
        assert keepalive_expiry(interface._http) == CLIENT_KEEP_ALIVE_S  # noqa: SLF001
        assert keepalive_expiry(scheduler._http) == CLIENT_KEEP_ALIVE_S  # noqa: SLF001
    finally:
        interface.close()
        scheduler.close()


def test_the_api_is_served_with_the_servers_keep_alive(monkeypatch, tmp_path: Path):
    served: dict = {}
    monkeypatch.setattr("dta_runtime.api.create_app", lambda settings: "the app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.update(options, app=app))
    env = {
        "DTA_STORE_HOST": "127.0.0.1",
        "DTA_STORE_PORT": "9",
        "DTA_STORE_USER": "root",
        "DTA_STORE_PASSWORD": secrets.token_urlsafe(16),
        "DTA_STORE_DATABASE": "twin",
        "DTA_API_TOKEN": secrets.token_urlsafe(32),
        "DTA_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "DTA_LOGS_DIR": str(tmp_path / "logs"),
    }
    result = CliRunner().invoke(cli.app, ["api", "--port", "8123"], env=env)
    assert result.exit_code == 0, result.output
    assert served["app"] == "the app"
    assert served["timeout_keep_alive"] == SERVER_KEEP_ALIVE_S
    assert served["port"] == 8123


async def _ok(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _serve(keep_alive_s: int) -> tuple[uvicorn.Server, int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(
        _ok, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=keep_alive_s
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, port


def _lost(port: int, expiry_s: float, idle_s: float, requests: int, clients: int) -> int:
    """Requests that failed, each sent after the client's connection idled about `idle_s`."""
    lost, lock = [0], threading.Lock()

    def poll(seed: int) -> None:
        jitter = random.Random(seed)
        limits = httpx.Limits(
            max_connections=10, max_keepalive_connections=10, keepalive_expiry=expiry_s
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", limits=limits) as client:
            for _ in range(requests):
                try:
                    client.get("/")
                except httpx.HTTPError:
                    with lock:
                        lost[0] += 1
                time.sleep(idle_s + jitter.uniform(-0.004, 0.004))

    threads = [threading.Thread(target=poll, args=(seed,)) for seed in range(clients)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return lost[0]


@pytest.mark.parametrize("idle_s", [1.0, 0.4])
def test_no_request_is_lost_when_the_client_gives_up_first(idle_s: float):
    # The stack's rule scaled down: the server keeps a connection 1 s, the client 0.4 s,
    # and requests are sent right when one or the other timer runs out.
    server, port = _serve(1)
    try:
        assert _lost(port, expiry_s=0.4, idle_s=idle_s, requests=25, clients=8) == 0
    finally:
        server.should_exit = True
