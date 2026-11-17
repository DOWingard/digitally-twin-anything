"""The scheduler against a real HTTP server standing in for the twin's API.

The server tells the twin's time, serves a manifest and counts the sync runs
it is asked to start; the test moves its clock by hand.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from dta_core import log as core_log
from dta_core.errors import ConfigError
from dta_runtime.scheduler import Scheduler, boundary
from dta_runtime.settings import SchedulerSettings

TOKEN = "scheduler-test-token-0123456789"
T0 = datetime(2026, 1, 19, 0, 0, tzinfo=UTC)


class Api:
    """What the stand-in API holds; the handler reads and writes it."""

    def __init__(self) -> None:
        self.now: datetime | None = T0
        self.tier = "live"
        self.interval: str | None = "PT15M"
        self.manifest_hash = "h1"
        self.refuse_runs = 0
        # A sync job the API has in progress, as health and a repeated request show it.
        self.job: dict[str, Any] | None = None
        self.hide_job_from_health = False
        self.runs: list[dict[str, Any]] = []
        self.manifest_reads = 0
        self.unauthorised = 0
        # The API's schedule; None for an API that has none (it answers 404).
        self.schedule: list[dict[str, Any]] | None = None
        self.schedule_reads = 0
        # Requests of the other entries, and statuses to answer them with, by path.
        self.asked: list[dict[str, Any]] = []
        self.answers: dict[str, list[int]] = {}


def _handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def _send(self, status: int, document: dict[str, Any]) -> None:
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                return True
            api.unauthorised += 1
            self._send(401, {"code": "auth.invalid"})
            return False

        def do_GET(self) -> None:
            if not self._authorised():
                return
            if self.path == "/api/v1/meta/health":
                clock = {
                    "kind": "source:plant",
                    "available": api.now is not None,
                    "now": api.now.isoformat().replace("+00:00", "Z") if api.now else None,
                }
                manifest = {"hash": api.manifest_hash, "revision": 1}
                shown = None if api.hide_job_from_health else api.job
                sync = {"job": {"id": shown["id"], "state": shown["state"]} if shown else None}
                self._send(
                    200, {"tier": api.tier, "manifest": manifest, "clock": clock, "sync": sync}
                )
            elif self.path == "/api/v1/meta/manifest":
                api.manifest_reads += 1
                sync = {"interval": api.interval} if api.interval else {}
                self._send(200, {"manifest": {"sync": sync}, "manifest_hash": api.manifest_hash})
            elif self.path == "/api/v1/lifecycle/schedule" and api.schedule is not None:
                api.schedule_reads += 1
                self._send(200, {"now": None, "entries": api.schedule})
            else:
                self._send(404, {"code": "api.not_found"})

        def do_POST(self) -> None:
            if not self._authorised():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path != "/api/v1/sync/runs":
                key = self.headers.get("Idempotency-Key")
                statuses = api.answers.get(self.path) or [202]
                status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
                api.asked.append({"path": self.path, "key": key, "status": status})
                job = {"id": f"other-{len(api.asked)}", "idempotency_key": key}
                self._send(status, {"job": job} if status < 400 else {"code": "refused"})
                return
            if api.refuse_runs:
                api.refuse_runs -= 1
                self._send(503, {"code": "store.unavailable"})
                return
            key = self.headers.get("Idempotency-Key")
            if api.job is not None:
                # The same run is in progress: it is handed back, with the key it was made for.
                self._send(200, {"job": api.job})
                return
            api.runs.append({"key": key, "body": body})
            job = {"id": f"job-{len(api.runs)}", "state": "queued", "idempotency_key": key}
            self._send(202, {"job": job})

    return Handler


@pytest.fixture
def api() -> Iterator[tuple[Api, str]]:
    state = Api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def scheduler(api: tuple[Api, str], tmp_path: Path) -> Iterator[Scheduler]:
    _, url = api
    settings = SchedulerSettings(api_url=url, api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05)
    running = Scheduler(settings)
    try:
        yield running
    finally:
        running.close()


def test_a_boundary_is_a_multiple_of_the_interval_counted_from_the_epoch():
    quarter = timedelta(minutes=15)
    assert boundary(T0 + timedelta(minutes=14, seconds=59), quarter) == T0
    assert boundary(T0 + quarter, quarter) == T0 + quarter
    assert boundary(datetime(2026, 1, 19, 7, 3, tzinfo=UTC), timedelta(hours=6)) == datetime(
        2026, 1, 19, 6, tzinfo=UTC
    )


def test_one_run_per_interval_of_the_twins_clock(api, scheduler):
    state, _ = api
    first = scheduler.tick()
    assert first["started"] == "sync" and first["key"] == "sync:2026-01-19T00:00:00+00:00"
    assert scheduler.tick() == {"started": None, "reason": "not_due"}
    state.now = T0 + timedelta(minutes=14)
    assert scheduler.tick()["reason"] == "not_due"
    state.now = T0 + timedelta(minutes=15)
    assert scheduler.tick()["key"] == "sync:2026-01-19T00:15:00+00:00"
    assert [run["key"] for run in state.runs] == [
        "sync:2026-01-19T00:00:00+00:00",
        "sync:2026-01-19T00:15:00+00:00",
    ]
    assert state.manifest_reads == 1  # the manifest is read again only when its hash changes
    assert state.unauthorised == 0


def test_after_a_gap_one_run_is_started_for_the_latest_interval(api, scheduler):
    state, _ = api
    scheduler.tick()
    state.now = T0 + timedelta(hours=6, minutes=7)  # the run itself catches up on what it missed
    assert scheduler.tick()["key"] == "sync:2026-01-19T06:00:00+00:00"
    assert len(state.runs) == 2


def test_a_boundary_that_comes_while_a_run_is_going_gets_its_run_when_that_one_ends(api, scheduler):
    state, _ = api
    assert scheduler.tick()["started"] == "sync"
    # The first run is still going when the next boundary comes.
    state.job = {"id": "job-1", "state": "running", "idempotency_key": state.runs[0]["key"]}
    state.now = T0 + timedelta(minutes=15)
    assert scheduler.tick() == {"started": None, "reason": "sync.in_progress"}
    assert scheduler.tick() == {"started": None, "reason": "sync.in_progress"}
    assert len(state.runs) == 1
    state.job = None
    state.now = T0 + timedelta(minutes=17)
    assert scheduler.tick()["key"] == "sync:2026-01-19T00:15:00+00:00"
    assert scheduler.tick()["reason"] == "not_due"
    assert len(state.runs) == 2


def test_a_run_handed_back_that_is_another_requests_does_not_count_for_the_boundary(
    api, scheduler, tmp_path: Path
):
    state, url = api
    # Someone else's run began after the scheduler looked: health did not show it yet.
    state.job = {"id": "job-x", "state": "running", "idempotency_key": None}
    state.hide_job_from_health = True
    assert scheduler.tick() == {"started": None, "reason": "sync.in_progress"}
    assert state.runs == []
    state.job = None
    assert scheduler.tick()["key"] == "sync:2026-01-19T00:00:00+00:00"
    # Its own run handed back, as after a restart of the scheduler, does count.
    state.job = {"id": "job-1", "state": "running", "idempotency_key": state.runs[0]["key"]}
    state.hide_job_from_health = False
    restarted = Scheduler(
        SchedulerSettings(api_url=url, api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05)
    )
    try:
        assert restarted.tick()["reason"] == "sync.in_progress"  # health shows the job
        state.hide_job_from_health = True
        assert restarted.tick()["started"] == "sync"
        assert restarted.tick()["reason"] == "not_due"
    finally:
        restarted.close()
    assert len(state.runs) == 1


def test_nothing_starts_while_the_clock_is_unavailable(api, scheduler):
    state, _ = api
    state.now = None
    assert scheduler.tick() == {"started": None, "reason": "clock.unavailable"}
    state.now = T0
    assert scheduler.tick()["started"] == "sync"


def test_a_refused_run_is_asked_for_again_with_the_same_key(api, scheduler):
    state, _ = api
    state.refuse_runs = 2
    assert scheduler.tick()["reason"] == "api.refused"
    assert scheduler.tick()["reason"] == "api.refused"
    assert scheduler.tick()["key"] == "sync:2026-01-19T00:00:00+00:00"
    assert len(state.runs) == 1


def test_a_revised_interval_takes_effect_without_a_restart(api, scheduler):
    state, _ = api
    scheduler.tick()
    state.interval, state.manifest_hash = "PT5M", "h2"
    state.now = T0 + timedelta(minutes=5)
    assert scheduler.tick()["key"] == "sync:2026-01-19T00:05:00+00:00"
    assert state.manifest_reads == 2


def test_a_static_twin_and_a_twin_without_an_interval_are_left_alone(api, scheduler):
    state, _ = api
    state.tier = "static"
    assert scheduler.tick()["reason"] == "twin.not_live"
    state.tier, state.interval, state.manifest_hash = "live", None, "h2"
    assert scheduler.tick()["reason"] == "sync.no_interval"
    assert state.runs == []


def test_an_api_that_does_not_answer_is_waited_for(tmp_path: Path):
    settings = SchedulerSettings(
        api_url="http://127.0.0.1:9", api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05
    )
    scheduler = Scheduler(settings)
    try:
        assert scheduler.tick() == {"started": None, "reason": "api.unreachable"}
    finally:
        scheduler.close()


def test_the_loop_keeps_going_and_the_token_is_never_logged(api, scheduler, tmp_path: Path):
    state, _ = api
    root = logging.getLogger("dta")
    saved = (list(root.handlers), root.propagate, root.level)
    log_file = tmp_path / "scheduler.jsonl"
    core_log.configure(log_file=log_file, level="debug")
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run, args=(stop,))
    thread.start()
    try:
        for step in range(1, 4):
            waited = threading.Event()
            while len(state.runs) < step and not waited.wait(0.02):
                pass
            state.now = T0 + timedelta(minutes=15 * step)
    finally:
        stop.set()
        thread.join(timeout=5)
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        handlers, propagate, level = saved
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)
    assert len(state.runs) >= 3 and not thread.is_alive()
    logged = log_file.read_text(encoding="utf-8")
    assert "sync run started" in logged and TOKEN not in logged


def entry(entry_id: str, interval: str, path: str, kind: str | None = None) -> dict[str, Any]:
    """A schedule entry as the API's schedule lists it."""
    return {
        "id": entry_id,
        "kind": kind or entry_id,
        "interval": interval,
        "method": "POST",
        "path": path,
        "key": f"{entry_id}:{{boundary}}",
        "body": {},
    }


SYNC = entry("sync", "PT15M", "/api/v1/sync/runs")
CHECK = entry("fidelity", "PT30M", "/api/v1/fidelity/checks")


def keys(state: Api, path: str | None = None) -> list[str]:
    if path is None:
        return [run["key"] for run in state.runs]
    return [found["key"] for found in state.asked if found["path"] == path]


def test_each_entry_is_asked_for_once_per_boundary_and_the_others_after_the_sync(api, scheduler):
    state, _ = api
    state.schedule = [SYNC, CHECK]
    first = scheduler.tick()
    assert first["started"] == ["sync"]
    assert first["waiting"] == {"fidelity": "sync.not_ended"}
    # The run is going: the check waits for it.
    state.job = {"id": "job-1", "state": "running", "idempotency_key": state.runs[0]["key"]}
    assert scheduler.tick()["reason"] == "sync.not_ended"
    state.job = None
    assert scheduler.tick()["started"] == ["fidelity"]
    assert scheduler.tick() == {"started": None, "reason": "not_due", "waiting": {}}
    # A sync boundary that is not a check boundary.
    state.now = T0 + timedelta(minutes=15)
    assert scheduler.tick()["started"] == ["sync"]
    assert scheduler.tick()["reason"] == "not_due"
    state.now = T0 + timedelta(minutes=31)
    assert scheduler.tick()["started"] == ["sync"]
    assert scheduler.tick()["started"] == ["fidelity"]
    assert scheduler.tick()["reason"] == "not_due"
    assert keys(state) == [
        "sync:2026-01-19T00:00:00+00:00",
        "sync:2026-01-19T00:15:00+00:00",
        "sync:2026-01-19T00:30:00+00:00",
    ]
    assert keys(state, CHECK["path"]) == [
        "fidelity:2026-01-19T00:00:00+00:00",
        "fidelity:2026-01-19T00:30:00+00:00",
    ]
    assert state.manifest_reads == 0 and state.schedule_reads == 1


def test_a_failing_entry_does_not_hold_up_the_others(api, scheduler):
    state, _ = api
    a, b = entry("a", "PT15M", "/api/v1/a"), entry("b", "PT15M", "/api/v1/b")
    # Entries that would send the token elsewhere, or could not be told apart, are left out.
    elsewhere = {**entry("c", "PT15M", "/api/v1/c"), "path": "http://elsewhere/api/v1/c"}
    keyless = {**entry("d", "PT15M", "/api/v1/d"), "key": "d"}
    state.schedule = [SYNC, a, elsewhere, keyless, b]
    # `a` is refused for good at the first boundary and cannot be taken now at the second.
    state.answers["/api/v1/a"] = [404, 503, 503, 202]
    assert scheduler.tick()["started"] == ["sync"]
    assert scheduler.tick()["started"] == ["b"]
    assert scheduler.tick()["reason"] == "not_due"  # a refusal for good is not asked again
    state.now = T0 + timedelta(minutes=15)
    scheduler.tick()
    second = scheduler.tick()
    assert second["started"] == ["b"] and second["waiting"] == {"a": "api.refused"}
    assert scheduler.tick()["waiting"] == {"a": "api.refused"}
    assert scheduler.tick()["started"] == ["a"]
    assert [found["status"] for found in state.asked if found["path"] == "/api/v1/a"] == [
        404,
        503,
        503,
        202,
    ]
    assert keys(state, "/api/v1/b") == [
        "b:2026-01-19T00:00:00+00:00",
        "b:2026-01-19T00:15:00+00:00",
    ]
    assert {found["path"] for found in state.asked} == {"/api/v1/a", "/api/v1/b"}


def test_the_schedule_is_read_again_when_the_manifest_changes_and_now_and_then(api, scheduler):
    state, _ = api
    state.schedule = [SYNC]
    scheduler.tick()
    scheduler.tick()
    assert state.schedule_reads == 1
    state.manifest_hash = "h2"
    state.schedule = [SYNC, CHECK]
    assert scheduler.tick()["started"] == ["fidelity"]
    assert state.schedule_reads == 2
    for _ in range(60):
        scheduler.tick()
    assert state.schedule_reads == 3


def test_an_api_without_a_schedule_is_asked_for_the_sync_run_alone_until_it_has_one(api, scheduler):
    state, _ = api
    assert scheduler.tick()["started"] == "sync"  # 404: the sync run, at the manifest's interval
    assert state.manifest_reads == 1
    state.schedule, state.manifest_hash = [SYNC, CHECK], "h2"
    state.now = T0 + timedelta(minutes=15)
    assert scheduler.tick()["started"] == ["sync"]
    assert scheduler.tick()["started"] == ["fidelity"]
    assert keys(state) == ["sync:2026-01-19T00:00:00+00:00", "sync:2026-01-19T00:15:00+00:00"]


def test_a_restarted_scheduler_asks_again_with_the_same_keys(api, scheduler, tmp_path: Path):
    state, url = api
    state.schedule = [SYNC, CHECK]
    scheduler.tick()
    scheduler.tick()
    again = Scheduler(
        SchedulerSettings(api_url=url, api_token=TOKEN, logs_dir=tmp_path, poll_s=0.05)
    )
    try:
        again.tick()
        again.tick()
    finally:
        again.close()
    assert keys(state) == ["sync:2026-01-19T00:00:00+00:00"] * 2
    assert keys(state, CHECK["path"]) == ["fidelity:2026-01-19T00:00:00+00:00"] * 2


@pytest.mark.parametrize("poll", ["0", "soon", "99999"])
def test_settings_refuse_a_poll_period_that_makes_no_sense(tmp_path: Path, poll: str):
    env = {
        "DTA_API_URL": "http://api:8000",
        "DTA_API_TOKEN": TOKEN,
        "DTA_LOGS_DIR": str(tmp_path),
        "DTA_SCHEDULER_POLL_S": poll,
    }
    with pytest.raises(ConfigError) as caught:
        SchedulerSettings.from_env(env)
    assert caught.value.detail == {"name": "DTA_SCHEDULER_POLL_S"}
    del env["DTA_SCHEDULER_POLL_S"]
    with pytest.raises(ConfigError) as missing:
        SchedulerSettings.from_env(env)
    assert "DTA_SCHEDULER_POLL_S" in str(missing.value.detail)
