"""The twin's scheduler: it holds triggers and nothing else.

Every pass it asks the API what time the twin's clock shows and whether the
manifest changed, and starts whatever has come due by calling the API. It
contains no twin logic, opens neither the store nor a source, and keeps no
state worth saving: a restarted scheduler asks for the run of the current
interval again, and asking twice does no harm because every request carries a
key the API recognises and a sync run that finds nothing new writes nothing
of the domain.

What is due is decided on the twin's clock, never on the wall: a twin of a
simulated or replayed system runs at that system's pace. The wall is used
only to wait between two looks.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from dta_core.log import Log
from pydantic import TypeAdapter, ValidationError

from .settings import SchedulerSettings

log = Log("runtime.scheduler")

API = "/api/v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
LIVE = "live"
_DURATION = TypeAdapter(timedelta)

# Why a pass did nothing.
API_UNREACHABLE = "api.unreachable"
API_REFUSED = "api.refused"
CLOCK_UNAVAILABLE = "clock.unavailable"
NOT_LIVE = "twin.not_live"
NO_INTERVAL = "sync.no_interval"
NOT_DUE = "not_due"


def boundary(now: datetime, interval: timedelta) -> datetime:
    """The latest multiple of the interval at or before `now`, counted from the Unix epoch."""
    whole = (now - EPOCH) // interval
    return EPOCH + whole * interval


class Scheduler:
    def __init__(self, settings: SchedulerSettings, *, http: httpx.Client | None = None) -> None:
        self._poll_s = settings.poll_s
        self._http = http or httpx.Client(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {settings.api_token}"},
            timeout=TIMEOUT,
        )
        self._manifest_hash: str | None = None
        self._interval: timedelta | None = None
        self._tier: str | None = None
        self._fired: datetime | None = None
        self._idle_reason: str | None = None

    def close(self) -> None:
        self._http.close()

    # -- one pass ---------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """One look at the twin: what was started, or why nothing was."""
        health = self._get(f"{API}/meta/health")
        if health is None:
            return self._idle(API_UNREACHABLE)
        clock = health.get("clock") or {}
        self._read_schedule(health)
        if self._tier != LIVE:
            return self._idle(NOT_LIVE)
        if self._interval is None:
            return self._idle(NO_INTERVAL)
        if not clock.get("available") or not clock.get("now"):
            return self._idle(CLOCK_UNAVAILABLE)
        now = datetime.fromisoformat(str(clock["now"]).replace("Z", "+00:00"))
        due = boundary(now, self._interval)
        if self._fired is not None and due <= self._fired:
            return self._idle(NOT_DUE, quiet=True)
        key = f"sync:{due.isoformat()}"
        started = time.perf_counter()
        try:
            response = self._http.post(
                f"{API}/sync/runs", headers={"Idempotency-Key": key}, json={}
            )
        except httpx.HTTPError as exc:
            log.warn(
                "sync run could not be started; it is asked for again on the next pass",
                due=due.isoformat(),
                error_type=type(exc).__name__,
            )
            return self._idle(API_UNREACHABLE)
        elapsed = round((time.perf_counter() - started) * 1000.0, 1)
        if response.status_code >= 400:
            log.warn(
                "the API refused to start a sync run; it is asked for again on the next pass",
                due=due.isoformat(),
                status=response.status_code,
                code=_problem_code(response),
                duration_ms=elapsed,
            )
            return self._idle(API_REFUSED)
        missed = 0 if self._fired is None else (due - self._fired) // self._interval - 1
        self._fired, self._idle_reason = due, None
        job = _json(response).get("job") or {}
        log.info(
            "sync run started",
            due=due.isoformat(),
            now=now.isoformat(),
            job=job.get("id"),
            intervals_missed=missed,
            duration_ms=elapsed,
        )
        return {"started": "sync", "due": due.isoformat(), "key": key, "job": job.get("id")}

    def _read_schedule(self, health: dict[str, Any]) -> None:
        """Take the interval from the manifest again whenever the manifest's hash changed."""
        self._tier = health.get("tier")
        found = (health.get("manifest") or {}).get("hash")
        if found == self._manifest_hash and found is not None:
            return
        document = self._get(f"{API}/meta/manifest")
        if document is None:
            return  # keep what is known; the next pass asks again
        raw = ((document.get("manifest") or {}).get("sync") or {}).get("interval")
        try:
            interval = _DURATION.validate_python(raw) if raw is not None else None
        except ValidationError:
            log.error("the manifest's sync interval cannot be read", manifest_hash=found)
            interval = None
        if interval is not None and interval <= timedelta(0):
            log.error("the manifest's sync interval is not positive", manifest_hash=found)
            interval = None
        if interval != self._interval or found != self._manifest_hash:
            log.info(
                "schedule read from the manifest",
                manifest_hash=found,
                tier=self._tier,
                sync_interval_s=interval.total_seconds() if interval else None,
            )
        self._manifest_hash, self._interval = found, interval

    def _idle(self, reason: str, *, quiet: bool = False) -> dict[str, Any]:
        # A reason is logged when it begins, not on every pass it lasts.
        if not quiet and reason != self._idle_reason:
            log.warn("nothing started this pass", reason=reason)
        if not quiet:
            self._idle_reason = reason
        return {"started": None, "reason": reason}

    def _get(self, path: str) -> dict[str, Any] | None:
        try:
            response = self._http.get(path)
        except httpx.HTTPError as exc:
            log.debug("the API did not answer", path=path, error_type=type(exc).__name__)
            return None
        if response.status_code != 200:
            log.warn("the API refused a read", path=path, status=response.status_code)
            return None
        return _json(response)

    # -- running ------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Pass after pass until `stop` is set. A pass that fails is logged; the next one runs."""
        log.info("scheduler running", poll_s=self._poll_s)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                log.error("scheduler pass failed", exc=exc)
            stop.wait(self._poll_s)
        log.info("scheduler stopped")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _problem_code(response: httpx.Response) -> str | None:
    code = _json(response).get("code")
    return code if isinstance(code, str) else None
