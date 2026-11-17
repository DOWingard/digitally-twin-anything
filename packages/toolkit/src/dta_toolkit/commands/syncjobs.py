"""Asking a live twin's API for a sync run and following its job to the end.

Shared by `dta build` and `dta sync run`. `POST /sync/runs` answers with the
job made for the run (202), or with the job of the same run already queued or
running (200); the job is then read until it has ended. Jobs carry no time of
the wall: how long the toolkit waits is measured on this machine's monotonic
clock, which is build tooling, never twin logic.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from dta_core.errors import CheckFailed
from dta_core.log import Log
from dta_core.manifest import Manifest

from ..acceptance import TwinApi

log = Log("commands.syncjobs")

API = "/api/v1"
RUNS_PATH = f"{API}/sync/runs"
JOBS_PATH = f"{API}/lifecycle/jobs"
HEALTH_PATH = f"{API}/meta/health"
SOURCES_PATH = f"{API}/sync/sources"

RUN_WAIT_S = 900.0
# The API is asked about a job at most every half second, less often as the wait grows.
POLL_FIRST_S = 0.5
POLL_MAX_S = 2.0

SUCCEEDED, FAILED, PARTIAL = "succeeded", "failed", "partial"
JOB_FINISHED = frozenset({SUCCEEDED, FAILED})
BUSY = "sync.busy"
PARTIAL_CODE = "sync.partial"
CLOCK_UNAVAILABLE = "clock.unavailable"
WAIT_TIMEOUT = "sync.wait_timeout"
NO_REPORT = "sync.no_report"
SOURCE_OK = "ok"
CLOCK_SOURCE_PREFIX = "source:"


class ApiRefused(CheckFailed):
    """The API answered with a problem document; its own code and message are kept."""

    code = "api.refused"


def refused(response: httpx.Response, request: str) -> ApiRefused:
    """The refusal a problem document describes, named by the API's own code."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("code") or f"api.status_{response.status_code}")
    message = str(body.get("detail") or body.get("title") or f"{request} answered an error")
    detail: dict[str, Any] = {
        "request": request,
        "status": response.status_code,
        "trace_id": body.get("trace_id"),
    }
    if isinstance(body.get("job"), dict):
        detail["job"] = body["job"]
    return ApiRefused(message, code=code, detail=detail)


def read_json(api: TwinApi, path: str, request: str | None = None) -> dict[str, Any]:
    """The JSON document a GET answers with 200; anything else is the API's refusal."""
    response = api.get(path)
    if response.status_code != 200:
        error = refused(response, request or f"GET {path}")
        log.warn("the API refused a read", request=request or path, code=error.code)
        raise error
    document = response.json()
    return document if isinstance(document, dict) else {}


def run_body(sources: list[str] | None, replay: tuple[str, str] | None) -> dict[str, Any]:
    """The body of `POST /sync/runs`: every source when nothing is named."""
    body: dict[str, Any] = {}
    if sources:
        body["sources"] = list(sources)
    if replay is not None:
        body["replay"] = {"source": replay[0], "dataset": replay[1]}
    return body


@dataclass(frozen=True)
class Started:
    job: dict[str, Any]
    # False when the API handed back the job of the same run, already queued or running.
    created: bool


def start_run(api: TwinApi, body: Mapping[str, Any]) -> Started:
    """Ask for one run; a refusal raises `ApiRefused` with the API's code."""
    started = time.perf_counter()
    response = api.request("POST", RUNS_PATH, json=dict(body))
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if response.status_code not in (200, 202):
        error = refused(response, f"POST {RUNS_PATH}")
        log.warn(
            "the API refused to start a sync run",
            code=error.code,
            status=response.status_code,
            body=dict(body),
            duration_ms=elapsed,
        )
        raise error
    job = (response.json() or {}).get("job") or {}
    created = response.status_code == 202
    log.info(
        "sync run asked for" if created else "the same sync run was already in progress",
        job=job.get("id"),
        state=job.get("state"),
        body=dict(body),
        duration_ms=elapsed,
    )
    return Started(job=job, created=created)


def wait_job(
    api: TwinApi,
    job_id: str,
    *,
    limit_s: float = RUN_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    what: str = "sync",
    code: str = WAIT_TIMEOUT,
    follow: str = "dta sync status",
) -> dict[str, Any]:
    """The job once it has succeeded or failed; `limit_s` seconds at most.

    `what` names the kind of job in what is logged and said, `code` is the code of
    giving up, and `follow` says where the job can be followed afterwards.
    """
    begun = clock()
    pause = POLL_FIRST_S
    polls = 0
    path = f"{JOBS_PATH}/{job_id}"
    while True:
        polls += 1
        job = read_json(api, path, f"GET {JOBS_PATH}/{{id}}").get("data") or {}
        waited = clock() - begun
        if job.get("state") in JOB_FINISHED:
            log.info(
                f"{what} job ended",
                job=job_id,
                state=job.get("state"),
                polls=polls,
                waited_s=round(waited, 1),
                job_duration_ms=job.get("duration_ms"),
            )
            return job
        if waited >= limit_s:
            log.error(
                f"gave up waiting for a {what} job",
                job=job_id,
                state=job.get("state"),
                limit_s=limit_s,
            )
            raise CheckFailed(
                f"the {what} job did not end within {limit_s:g} s; it may still be running "
                f"(see {follow})",
                code=code,
                detail={"job": job, "limit_s": limit_s},
            )
        log.debug(f"{what} job not ended yet", job=job_id, state=job.get("state"), polls=polls)
        sleep(pause)
        pause = min(pause * 2, POLL_MAX_S)


@dataclass(frozen=True)
class Verdict:
    """What a finished sync job says about its run."""

    ok: bool
    code: str | None = None
    message: str | None = None
    # Each source the run could not sync: its id, status, error code and message.
    failing: list[dict[str, Any]] = field(default_factory=list)


def failing_sources(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    found = []
    for source_id, entry in sorted(((report or {}).get("sources") or {}).items()):
        entry = entry or {}
        error = entry.get("error") or {}
        if entry.get("status") != SOURCE_OK or error:
            found.append(
                {
                    "source": source_id,
                    "status": entry.get("status"),
                    "code": error.get("code"),
                    "message": error.get("message"),
                }
            )
    return found


def _named(failing: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"source {entry['source']} ({entry['code']}: {entry['message']})" for entry in failing
    )


def verdict(job: Mapping[str, Any]) -> Verdict:
    """Ok only when the job succeeded and its run's status is `succeeded`."""
    report = job.get("result")
    failing = failing_sources(report)
    status = (report or {}).get("status")
    if job.get("state") == SUCCEEDED and status == SUCCEEDED:
        return Verdict(ok=True)
    if status == PARTIAL:
        return Verdict(
            ok=False,
            code=PARTIAL_CODE,
            message=f"the run was partial, some sources failed: {_named(failing)}",
            failing=failing,
        )
    if job.get("state") == SUCCEEDED:
        return Verdict(
            ok=False,
            code=NO_REPORT,
            message=f"the job succeeded but its run reports {status!r}",
            failing=failing,
        )
    error = job.get("error") or (report or {}).get("error") or {}
    code = str(error.get("code") or "sync.failed")
    message = f"the run failed: {code}: {error.get('message') or 'no message'}"
    if failing:
        message += f"; {_named(failing)}"
    return Verdict(ok=False, code=code, message=message, failing=failing)


def clock_origin(manifest: Manifest) -> str:
    """Where the twin's clock is read, in words."""
    clock = manifest.runtime.clock
    if clock.startswith(CLOCK_SOURCE_PREFIX):
        return f"source {clock[len(CLOCK_SOURCE_PREFIX) :]}"
    return "the wall clock of the API's host"


def run_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    """The numbers of a sync job's run that a person reads first."""
    report = job.get("result") or {}
    return {
        "job": job.get("id"),
        "job_state": job.get("state"),
        "idempotency_key": job.get("idempotency_key"),
        "run": report.get("run"),
        "kind": report.get("kind"),
        "status": report.get("status"),
        "now": report.get("now"),
        "versions_written": report.get("versions_written"),
        "rows_read": report.get("rows_read"),
        "rows_written": report.get("rows_written"),
        "rows_rejected": report.get("rows_rejected"),
        "rows_late": report.get("rows_late"),
        "watermark": report.get("watermark"),
        "duration_ms": report.get("duration_ms"),
    }
