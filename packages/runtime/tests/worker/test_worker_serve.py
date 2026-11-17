"""How the worker is served and sized: its keep-alive, its clients' limits, its pools,
the stack's default limits, and the command that runs it until it is told to stop."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from api_testkit import TOKEN
from dta_runtime.connections import CLIENT_KEEP_ALIVE_S, SERVER_KEEP_ALIVE_S
from dta_runtime.settings import WorkerSettings
from dta_runtime.worker import serve
from dta_runtime.worker.channel import Setup
from dta_runtime.worker.limits import (
    MIB,
    MIN_PROCESS_MEMORY,
    interactive_size,
    process_memory,
)
from dta_runtime.worker.run import Runner
from live_api_testkit import free_port
from worker_testkit import (
    TwinApi,
    function_body,
    predict_body,
    table_of,
    twin_code,
    wait_until,
)


def keepalive_expiry(client: httpx.Client) -> float:
    """How long the client's pool keeps an idle connection (httpx holds it privately)."""
    return client._transport._pool._keepalive_expiry  # noqa: SLF001


def test_the_worker_is_served_with_the_servers_keep_alive(monkeypatch: pytest.MonkeyPatch):
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.update(options, app=app))
    settings = WorkerSettings(
        api_url="http://127.0.0.1:9", api_token=secrets.token_urlsafe(32), cpus=1, memory_mb=512
    )
    serve(settings, host="127.0.0.1", port=8123)
    assert served["timeout_keep_alive"] == SERVER_KEEP_ALIVE_S
    assert (served["host"], served["port"]) == ("127.0.0.1", 8123)
    assert served["app"].title == "Digital twin worker"


def test_a_run_process_reaches_the_api_with_the_clients_limits(tmp_path: Path):
    setup = Setup(
        api_url="http://127.0.0.1:9",
        api_token=secrets.token_urlsafe(32),
        memory_bytes=MIN_PROCESS_MEMORY,
        code_root=str(tmp_path),
    )
    runner = Runner(setup, sender=None)  # type: ignore[arg-type]
    try:
        assert keepalive_expiry(runner._http) == CLIENT_KEEP_ALIVE_S  # noqa: SLF001
        assert CLIENT_KEEP_ALIVE_S < SERVER_KEEP_ALIVE_S
    finally:
        runner.close()
    # The token is not shown with the setup.
    assert setup.api_token not in repr(setup)


def test_pools_and_limits_are_sized_from_the_containers_cpus_and_memory():
    assert [interactive_size(cpus) for cpus in (0.25, 1, 1.9, 2, 3.5, 4, 16)] == [
        1, 1, 1, 2, 3, 4, 4,
    ]  # fmt: skip
    # Divided among every process that can run at once (both pools), at least 256 MiB.
    assert process_memory(3072) == 3072 * MIB
    assert process_memory(1024) == 1024 * MIB
    assert process_memory(128) == MIN_PROCESS_MEMORY


def test_the_stacks_default_limits_run_shipped_code_and_own_code(
    start_worker: object, twin_api: TwinApi
):
    # The manifest's defaults: one CPU and 2048 MB, which each of two processes may map.
    worker = start_worker(cpus=1.0, memory_mb=2048)  # type: ignore[operator]
    assert (worker.pools.interactive.size, worker.pools.batch.size) == (1, 1)
    assert worker.pools.memory_bytes == 2048 * MIB
    table, _result = table_of(worker.run(predict_body("read")))
    assert table.num_rows == 6
    head = twin_api.water.head
    code = twin_api.upload(twin_code())
    body = function_body(code, "twin_code:noop", head.id, branch=head.branch)
    assert worker.ok(body)["value"] is None


def _children(pid: int) -> list[int]:
    """Processes started by any thread of the process."""
    tasks = Path(f"/proc/{pid}/task").glob("*/children")
    return sorted(int(found) for task in tasks for found in task.read_text().split())


def test_the_command_serves_until_it_is_told_to_stop(twin_api: TwinApi, tmp_path: Path):
    port = free_port()
    command = Path(sys.executable).parent / "dta-runtime"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "DTA_API_URL": twin_api.url,
        "DTA_API_TOKEN": TOKEN,
        "DTA_WORKER_CPUS": "1",
        "DTA_WORKER_MEMORY_MB": "1024",
    }
    log_path = tmp_path / "worker.jsonl"
    with log_path.open("wb") as log_file:
        worker = subprocess.Popen(
            [str(command), "worker", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=log_file,
        )
    try:

        def healthy() -> bool:
            assert worker.poll() is None, "the worker exited"
            checked = subprocess.run(
                [str(command), "health", "worker", "--port", str(port)],
                env=env,
                capture_output=True,
            )
            return checked.returncode == 0

        wait_until(healthy, timeout_s=60, what="a healthy worker")
        # Its two pool processes run beside it, in sessions of their own.
        processes = _children(worker.pid)
        assert len(processes) == 2
        assert all(os.getsid(found) == found for found in processes)
        started = time.monotonic()
        worker.send_signal(signal.SIGTERM)
        # uvicorn stops gracefully, then lets the signal it caught end the process.
        assert worker.wait(timeout=30) in (0, -signal.SIGTERM)
        assert time.monotonic() - started < 10
        assert not [found for found in processes if Path(f"/proc/{found}").exists()]
        assert worker.stdout is not None and worker.stdout.read() == b""
        messages = [json.loads(line)["message"] for line in log_path.read_text().splitlines()]
        assert messages.count("pool closed") == 2 and "worker stopped" in messages
        assert TOKEN not in log_path.read_text()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
