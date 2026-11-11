"""Starting sync runs through the API: each run asked for is started once, never two at once,
and its job tells how it ended. The sources are folders of CSV files read by the real file
connector; the store and the sync pipeline are the real ones."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from dta_core.sources import SourceRateLimited, SourceUnavailable
from dta_runtime.api import runs as runs_module
from live_api_testkit import (
    HEADERS,
    LiveApi,
    SwitchedClock,
    http_client,
    plant,
    problem_code,
    records,
)
from live_testkit import at

MakeLive = Callable[..., LiveApi]
RUNS = "/api/v1/sync/runs"
DOWN = "the source did not answer in time"
SLOW_DOWN = "the source asked for fewer requests"
# A POST answers at once, whatever the run takes.
POST_LIMIT_MS = 200.0
JOB_FIELDS = {
    "id",
    "kind",
    "lane",
    "state",
    "params",
    "idempotency_key",
    "trace_id",
    "sequence",
    "duration_ms",
    "result",
    "error",
}


def test_a_run_is_started_as_a_job_and_recorded(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry", "lab")
    live = live_api(sources)

    response = live.post(RUNS, json={})

    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert set(job) == JOB_FIELDS
    assert re.fullmatch(r"[0-9a-f]{32}", job["id"])
    assert response.headers["location"] == f"/api/v1/lifecycle/jobs/{job['id']}"
    assert (job["kind"], job["params"], job["idempotency_key"]) == (
        "sync",
        {"sources": None, "replay": None},
        None,
    )
    assert job["state"] in ("queued", "running", "succeeded")
    # The job's work runs under the trace of the request that made it.
    assert job["trace_id"] == response.headers["traceparent"].split("-")[1]

    done = live.wait(job["id"])
    assert (done["state"], done["error"]) == ("succeeded", None)
    assert isinstance(done["duration_ms"], int) and done["duration_ms"] >= 0
    report = done["result"]
    assert (report["status"], report["kind"]) == ("succeeded", "incremental")
    assert set(report["sources"]) == {"registry", "lab"}
    assert report["rows_written"] > 0

    # The readings are in the graph.
    readings = live.ok("/api/v1/objects/Reading")["data"]
    assert sorted(row["uid"] for row in readings) == ["reading:R1", "reading:R2"]

    # And the run is recorded: listed, and readable alone.
    (listed,) = live.runs()
    assert listed["uid"] == report["run"]
    assert (listed["status"], listed["kind"], listed["clock_at"]) == (
        "succeeded",
        "incremental",
        "2026-01-05T00:16:00Z",
    )
    assert listed["rows_written"] == report["rows_written"]
    one = live.ok(f"{RUNS}/{report['run']}")
    assert one["data"] == listed
    assert one["version"]["seq"] == live.twin.store.head().seq
    assert live.get(f"{RUNS}/twin_sync_run:nope").json()["code"] == "sync.run_not_found"


def test_an_idempotency_key_gives_back_the_job_made_for_it(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry", "scada")
    live = live_api(sources)
    key = {"Idempotency-Key": "sync:2026-01-05T00:15:00+00:00"}

    first = live.post(RUNS, json={}, headers=key)
    again = live.post(RUNS, headers=key)
    assert (first.status_code, again.status_code) == (202, 200)
    job_id = first.json()["job"]["id"]
    assert again.json()["job"]["id"] == job_id
    assert again.headers["location"] == f"/api/v1/lifecycle/jobs/{job_id}"
    assert live.wait(job_id)["state"] == "succeeded"

    # Once the job has ended the key still names it; nothing is run again.
    late = live.post(RUNS, json={"sources": None, "replay": None}, headers=key)
    assert late.status_code == 200
    assert (late.json()["job"]["id"], late.json()["job"]["state"]) == (job_id, "succeeded")
    # The same parameters written another way are the same run.
    other_key = {"Idempotency-Key": "client-7"}
    made = live.post(RUNS, json={"sources": ["scada", "registry", "scada"]}, headers=other_key)
    assert made.status_code == 202
    assert made.json()["job"]["params"] == {"sources": ["registry", "scada"], "replay": None}
    live.wait(made.json()["job"]["id"])
    same = live.post(RUNS, json={"sources": ["registry", "scada"]}, headers=other_key)
    assert (same.status_code, same.json()["job"]["id"]) == (200, made.json()["job"]["id"])

    # The key with other parameters is refused.
    reused = live.post(RUNS, json={"sources": ["scada"]}, headers=key)
    assert (reused.status_code, problem_code(reused)) == (409, "sync.key_reused")
    assert len(live.runs()) == 2


@pytest.mark.parametrize(
    ("body", "key", "code"),
    [
        ({"sources": ["nowhere"]}, None, "sync.unknown_source"),
        ({"replay": {"source": "registry", "dataset": "nothing"}}, None, "sync.replay_unknown"),
        ({"replay": {"source": "nowhere", "dataset": "tanks"}}, None, "sync.replay_unknown"),
        ({"source": ["registry"]}, None, "api.bad_request"),
        ({"sources": []}, None, "api.bad_request"),
        ({"replay": {"source": "registry"}}, None, "api.bad_request"),
        ([], None, "api.bad_request"),
        ({}, "", "api.bad_request"),
        ({}, "k" * 201, "api.bad_request"),
        ({}, b"caf\xc3\xa9", "api.bad_request"),
    ],
)
def test_what_cannot_be_run_is_refused_at_once(
    live_api: MakeLive, plant_dir: Path, body: Any, key: str | bytes | None, code: str
):
    sources, _ = plant(plant_dir, "registry")
    live = live_api(sources)
    headers: Any = {"Idempotency-Key": key} if key is not None else None

    response = live.post(RUNS, json=body, headers=headers)

    assert (response.status_code, problem_code(response)) == (400, code), response.text
    assert live.ok("/api/v1/lifecycle/jobs")["data"] == []


def test_a_body_that_is_not_json_is_refused(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry")
    live = live_api(sources)
    response = live.post(RUNS, content=b"{not json", headers={"Content-Type": "application/json"})
    assert (response.status_code, problem_code(response)) == (400, "api.bad_request")


def test_while_a_run_is_in_progress_it_is_given_back_or_others_are_refused(
    live_api: MakeLive, plant_dir: Path
):
    sources, folders = plant(plant_dir)
    live = live_api(sources)
    gate = live.plant.hold("lab")

    started = time.perf_counter()
    response = live.post(RUNS, json={})
    post_ms = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    assert gate.reached.wait(60), "the run never reached the held source"
    print(f"\nPOST /sync/runs while the run is held: {post_ms:.1f} ms")
    assert post_ms < POST_LIMIT_MS

    # The same run asked for again: the job in progress.
    started = time.perf_counter()
    same = live.post(RUNS, json={"sources": None})
    again_ms = (time.perf_counter() - started) * 1000.0
    assert (same.status_code, same.json()["job"]["id"]) == (200, job_id)
    assert same.json()["job"]["state"] == "running"
    assert again_ms < POST_LIMIT_MS
    # Asked with a key of its own, the same run is still that job, which carries no key.
    keyed = live.post(RUNS, json={}, headers={"Idempotency-Key": "next-boundary"})
    assert (keyed.status_code, keyed.json()["job"]["id"]) == (200, job_id)
    assert keyed.json()["job"]["idempotency_key"] is None
    # Another run: refused, and told which job is in the way.
    busy = live.post(RUNS, json={"sources": ["lab"]}, headers={"Idempotency-Key": "lab-only"})
    assert (busy.status_code, problem_code(busy)) == (409, "sync.busy")
    assert busy.json()["job"]["id"] == job_id
    assert busy.json()["job"]["state"] == "running"
    health = live.health()
    assert health["sync"]["job"] == {"id": job_id, "state": "running"}

    # Readers are served meanwhile.
    for path in (
        "/api/v1/objects/Tank",
        "/api/v1/history/versions",
        "/api/v1/sync/sources",
        RUNS,
        "/api/v1/lifecycle/jobs",
        f"/api/v1/lifecycle/jobs/{job_id}",
    ):
        started = time.perf_counter()
        assert live.get(path).status_code == 200, path
        assert (time.perf_counter() - started) < 5.0, path

    gate.open()
    done = live.wait(job_id)
    assert (done["state"], done["result"]["status"]) == ("succeeded", "succeeded")
    assert live.health()["sync"]["job"] is None
    # A refused key is not remembered: asked again later it starts its run.
    later = live.post(RUNS, json={"sources": ["lab"]}, headers={"Idempotency-Key": "lab-only"})
    assert later.status_code == 202
    live.wait(later.json()["job"]["id"])
    assert len(live.runs()) == 2
    # Nor is a key bound to a job that was made for another request: the run that was in
    # progress began before this one was asked for, so the key now starts its own.
    own = live.post(RUNS, json={}, headers={"Idempotency-Key": "next-boundary"})
    assert own.status_code == 202
    assert own.json()["job"]["idempotency_key"] == "next-boundary"
    assert own.json()["job"]["id"] != job_id
    live.wait(own.json()["job"]["id"])
    assert len(live.runs()) == 3


def test_twenty_requests_at_once_make_one_job_and_one_run(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry", "lab")
    live = live_api(sources, served=True)
    gate = live.plant.hold("lab")
    together = threading.Barrier(20)
    base_url = str(live.client.base_url)

    def ask(_: int) -> tuple[int, str]:
        with http_client(base_url) as client:
            together.wait(30)
            response = client.post(RUNS, json={}, headers=HEADERS)
            return response.status_code, response.json()["job"]["id"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        answers = list(pool.map(ask, range(20)))
    gate.open()

    statuses = sorted(status for status, _ in answers)
    assert statuses == [200] * 19 + [202]
    (job_id,) = {found for _, found in answers}
    assert live.wait(job_id)["state"] == "succeeded"
    assert [job["id"] for job in live.ok("/api/v1/lifecycle/jobs")["data"]] == [job_id]
    assert len(live.runs()) == 1


def test_a_source_that_fails_makes_the_run_partial_until_it_is_mended(
    live_api: MakeLive, plant_dir: Path
):
    sources, folders = plant(plant_dir)
    live = live_api(sources)
    live.plant.break_("lab", SourceUnavailable(DOWN))

    first = live.run({})

    assert (first["state"], first["error"]) == ("succeeded", None)
    assert first["result"]["status"] == "partial"
    failing = live.sources()["lab"]
    assert (failing["status"], failing["last_error"]) == (
        "failing",
        f"source.unavailable: {DOWN}",
    )
    assert live.sources()["scada"]["status"] == "ok"
    by_id = {found["id"]: found for found in live.health()["sync"]["sources"]}
    assert (by_id["lab"]["status"], by_id["scada"]["status"]) == ("failing", "ok")
    last = live.health()["sync"]["last_run"]
    assert (last["uid"], last["status"]) == (first["result"]["run"], "partial")
    assert last["error"].startswith("sync.partial: ")

    live.plant.mend()
    folders["lab"].append("readings", [("R3", "S7", at(20), 4.0, 3)])
    live.clock.set(at(31))
    second = live.run({})

    assert (second["state"], second["result"]["status"]) == ("succeeded", "succeeded")
    mended = live.sources()["lab"]
    assert (mended["status"], mended["last_error"]) == ("ok", None)
    assert {found["status"] for found in live.health()["sync"]["sources"]} == {"ok"}
    assert [run["status"] for run in live.runs()] == ["succeeded", "partial"]
    assert [run["status"] for run in live.runs(status="partial")] == ["partial"]


def test_a_run_in_which_every_source_fails_fails_its_job(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry", "scada")
    live = live_api(sources)
    live.plant.break_("registry", SourceUnavailable(DOWN))
    live.plant.break_("scada", SourceRateLimited(SLOW_DOWN, detail={"retry_after_s": 60}))

    done = live.run({})

    assert done["state"] == "failed"
    report = done["result"]
    assert report["status"] == "failed"
    assert done["error"] == report["error"]
    assert done["error"]["code"] == "sync.source_failed"
    (recorded,) = live.runs(status="failed")
    assert recorded["uid"] == report["run"]
    listed = live.ok("/api/v1/lifecycle/jobs", params={"state": "failed"})["data"]
    assert [job["id"] for job in listed] == [done["id"]]


def test_a_clock_that_cannot_be_read_is_reported_and_runs_nothing(
    live_api: MakeLive, plant_dir: Path, log_lines: Path
):
    sources, _ = plant(plant_dir, "registry", "scada")
    clock = SwitchedClock(at(16))
    live = live_api(sources, clock=clock)
    live.run({})
    clock.down = True
    head = live.twin.store.head()

    for _ in range(5):
        health = live.health()
        assert health["clock"] == {"kind": "source:plant", "available": False, "now": None}
        assert (health["sync"]["freshness_s"], health["sync"]["fresh"]) == (None, None)
        assert all(found["freshness_s"] is None for found in health["sync"]["sources"])
    sources_now = live.ok("/api/v1/sync/sources")
    assert sources_now["clock"]["available"] is False
    assert all(found["freshness_s"] is None for found in sources_now["data"])
    # Said once, not on every poll.
    warned = [
        found
        for found in records(log_lines)
        if found["message"].startswith("the twin's clock cannot be read")
    ]
    assert len(warned) == 1 and warned[0]["level"] == "warn"
    assert warned[0]["data"]["code"] == "clock.unavailable"

    response = live.post(RUNS, json={})
    assert response.status_code == 202
    done = live.wait(response.json()["job"]["id"])

    assert (done["state"], done["result"]) == ("failed", None)
    assert done["error"]["code"] == "clock.unavailable"
    assert live.twin.store.head() == head
    assert len(live.runs()) == 1


def test_a_static_twin_runs_nothing(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir, "registry")
    live = live_api(sources, sync=None, tier="static")

    response = live.post(RUNS, json={}, headers={"Idempotency-Key": "k1"})

    assert (response.status_code, problem_code(response)) == (409, "twin.not_live")
    health = live.health()
    assert health["sync"] is None
    assert health["clock"] == {"kind": "fixed", "available": True, "now": "2026-01-05T00:16:00Z"}
    assert live.runs() == []
    assert live.ok("/api/v1/lifecycle/jobs")["data"] == []
    (registry,) = live.ok("/api/v1/sync/sources")["data"]
    assert registry["id"] == "registry"


@pytest.mark.parametrize("window_rows", [2, runs_module.WINDOW_ROWS])
def test_runs_are_paged_newest_first_and_runs_that_started_together_by_uid(
    live_api: MakeLive, plant_dir: Path, monkeypatch: pytest.MonkeyPatch, window_rows: int
):
    # With a window of two rows, the three runs that started together overflow every window
    # that holds their moment, down to the moment alone.
    monkeypatch.setattr(runs_module, "WINDOW_ROWS", window_rows)
    sources, _ = plant(plant_dir, "registry", "scada")
    live = live_api(sources)
    for minute, count in ((16, 3), (31, 2), (46, 2)):
        live.clock.set(at(minute))
        for _ in range(count):
            live.run({})
    live.plant.break_("scada", SourceUnavailable(DOWN))
    live.run({})

    every = live.runs(limit=100)
    assert len(every) == 8
    keys = [(run["started_at"], run["uid"]) for run in every]
    expected = sorted(keys, key=lambda key: key[1])
    expected.sort(key=lambda key: key[0], reverse=True)
    assert keys == expected
    # Several runs started at one moment: the pages must cut through them.
    assert len({run["started_at"] for run in every}) < len(every)

    for limit in (1, 2, 3):
        paged: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            page = live.ok(RUNS, params=params)
            assert len(page["data"]) <= limit
            paged.extend(page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert paged == every, limit

    (partial,) = live.runs(status="partial")
    assert [partial] == [run for run in every if run["status"] == "partial"]
    assert live.runs(status="partial", kind="replay") == []
    assert len(live.runs(kind="incremental")) == 8
    first = live.ok(RUNS, params={"limit": 2})
    # A cursor keeps reading the version of its first page, and its own filters.
    live.run({})
    following = live.ok(RUNS, params={"limit": 2, "cursor": first["next_cursor"]})
    assert following["version"] == first["version"]
    assert following["data"] == every[2:4]
    mixed = live.get(RUNS, params={"status": "failed", "cursor": first["next_cursor"]})
    assert (mixed.status_code, problem_code(mixed)) == (400, "api.bad_cursor")
    for params in ({"status": "lost"}, {"limit": 0}, {"limit": 10_001}):
        assert live.get(RUNS, params=params).status_code == 400, params
