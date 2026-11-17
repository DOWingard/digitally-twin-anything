"""The twin's scheduler: it holds triggers and nothing else.

Every pass it asks the API what time the twin's clock shows and whether the
manifest changed, and starts whatever has come due by calling the API. It
contains no twin logic, opens neither the store nor a source, and keeps no
state worth saving: a restarted scheduler asks for what is due at the current
boundary again, and asking twice does no harm because every request carries a
key the API recognises.

What to ask for comes from the API's schedule (`/lifecycle/schedule`): entries,
each asked for once per boundary of its interval with its key. The sync run is
asked for first; the other entries once the sync run of the newest sync
boundary has ended, whatever it came to, so that they see the data of that
boundary. One entry that fails does not hold up the others. The schedule is
read again when the manifest's hash changes and at least every
`REREAD_LOOKS` passes. An API without the schedule is asked for the sync run
alone, at the interval of its manifest, as before.

A boundary that comes while a sync run is still going is not dropped: its run
is asked for as soon as that one has ended. What is due is decided on the
twin's clock, never on the wall: a twin of a simulated or replayed system runs
at that system's pace. The wall is used only to wait between two looks.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from dta_core.log import Log
from pydantic import TypeAdapter, ValidationError

from .connections import client_limits
from .settings import SchedulerSettings

log = Log("runtime.scheduler")

API = "/api/v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
LIVE = "live"
SCHEDULE_PATH = f"{API}/lifecycle/schedule"
BOUNDARY = "{boundary}"
REREAD_LOOKS = 60
_DURATION = TypeAdapter(timedelta)
# Answers after which the same request may succeed later.
_TRANSIENT = frozenset({408, 425, 429})
_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Why a pass, or an entry, did nothing.
API_UNREACHABLE = "api.unreachable"
API_REFUSED = "api.refused"
CLOCK_UNAVAILABLE = "clock.unavailable"
NOT_LIVE = "twin.not_live"
NO_INTERVAL = "sync.no_interval"
SYNC_IN_PROGRESS = "sync.in_progress"
SYNC_NOT_ENDED = "sync.not_ended"
NOTHING_SCHEDULED = "schedule.empty"
NOT_DUE = "not_due"


def boundary(now: datetime, interval: timedelta) -> datetime:
    """The latest multiple of the interval at or before `now`, counted from the Unix epoch."""
    whole = (now - EPOCH) // interval
    return EPOCH + whole * interval


@dataclass(frozen=True)
class Entry:
    """One thing the API asks to be asked for at every boundary of `interval`."""

    id: str
    kind: str
    interval: timedelta
    method: str
    path: str
    key: str
    body: dict[str, Any] = field(default_factory=dict)

    def key_at(self, due: datetime) -> str:
        return self.key.replace(BOUNDARY, due.isoformat())


def _entry(document: Any) -> Entry | None:
    """An entry of the schedule, or None for one that cannot be followed safely."""
    if not isinstance(document, dict):
        return None
    try:
        interval = _DURATION.validate_python(document.get("interval"))
    except ValidationError:
        return None
    path, key, method = document.get("path"), document.get("key"), document.get("method")
    body = document.get("body") or {}
    if not (
        isinstance(document.get("id"), str)
        and isinstance(document.get("kind"), str)
        and interval > timedelta(0)
        # A path of this API only: the token is sent with every request.
        and isinstance(path, str)
        and path.startswith(f"{API}/")
        and "//" not in path
        and isinstance(key, str)
        and BOUNDARY in key
        and isinstance(method, str)
        and method.upper() in _METHODS
        and isinstance(body, dict)
    ):
        return None
    return Entry(document["id"], document["kind"], interval, method.upper(), path, key, dict(body))


class Scheduler:
    def __init__(self, settings: SchedulerSettings, *, http: httpx.Client | None = None) -> None:
        self._poll_s = settings.poll_s
        self._http = http or httpx.Client(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {settings.api_token}"},
            timeout=TIMEOUT,
            limits=client_limits(),
        )
        self._manifest_hash: str | None = None
        self._interval: timedelta | None = None
        self._tier: str | None = None
        self._fired: datetime | None = None
        self._idle_reason: str | None = None
        # The schedule: None until read; `_legacy` when the API has none.
        self._entries: list[Entry] | None = None
        self._legacy = False
        self._schedule_hash: str | None = None
        self._looks = 0
        # Entry id -> the latest boundary it was asked for.
        self._asked: dict[str, datetime] = {}
        self._waiting: dict[str, str] = {}

    def close(self) -> None:
        self._http.close()

    # -- one pass ---------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """One look at the twin: what was started, or why nothing was."""
        health = self._get(f"{API}/meta/health")
        if health is None:
            return self._idle(API_UNREACHABLE)
        self._looks += 1
        found = (health.get("manifest") or {}).get("hash")
        unread = self._entries is None and not self._legacy
        if unread or found != self._schedule_hash or self._looks >= REREAD_LOOKS:
            self._read_entries(found)
        if self._legacy:
            return self._tick_sync_only(health)
        if self._entries is None:
            return self._idle(API_UNREACHABLE)
        return self._tick_schedule(health)

    # -- the schedule -------------------------------------------------------------

    def _read_entries(self, manifest_hash: str | None) -> None:
        """Read the schedule; keep what is known when it cannot be read now."""
        status, document = self._fetch(SCHEDULE_PATH)
        if status == 404:
            if not self._legacy:
                log.info(
                    "the API has no schedule; the sync run alone is asked for, at the "
                    "manifest's interval"
                )
            self._legacy, self._entries = True, None
            self._schedule_hash, self._looks = manifest_hash, 0
            return
        if status != 200 or document is None:
            log.warn("the schedule cannot be read now; what is known is kept", status=status)
            return
        entries: list[Entry] = []
        for given in document.get("entries") or []:
            entry = _entry(given)
            if entry is None:
                log.error(
                    "a schedule entry cannot be followed; it is left out",
                    id=given.get("id") if isinstance(given, dict) else None,
                )
                continue
            entries.append(entry)
        if self._legacy or [e.id for e in entries] != [e.id for e in self._entries or []]:
            log.info(
                "schedule read",
                manifest_hash=manifest_hash,
                entries=[
                    {"id": e.id, "kind": e.kind, "interval_s": e.interval.total_seconds()}
                    for e in entries
                ],
            )
        self._legacy, self._entries = False, entries
        self._schedule_hash, self._looks = manifest_hash, 0

    def _tick_schedule(self, health: dict[str, Any]) -> dict[str, Any]:
        self._tier = health.get("tier")
        if self._tier != LIVE:
            return self._idle(NOT_LIVE)
        clock = health.get("clock") or {}
        if not clock.get("available") or not clock.get("now"):
            return self._idle(CLOCK_UNAVAILABLE)
        if not self._entries:
            return self._idle(NOTHING_SCHEDULED)
        now = datetime.fromisoformat(str(clock["now"]).replace("Z", "+00:00"))
        sync_job = (health.get("sync") or {}).get("job")
        syncs = [entry for entry in self._entries if entry.kind == "sync"]
        others = [entry for entry in self._entries if entry.kind != "sync"]
        started: list[dict[str, Any]] = []
        waiting: dict[str, str] = {}
        asked_sync = False
        for entry in syncs:
            due = boundary(now, entry.interval)
            if self._done(entry, due):
                continue
            if sync_job:
                # A run still going began before this boundary and leaves its interval
                # untaken; the boundary's own run is asked for once it has ended.
                waiting[entry.id] = SYNC_IN_PROGRESS
                continue
            try:
                outcome, why = self._ask_sync(entry, due, now)
            except Exception as exc:
                log.error("asking for a schedule entry failed", exc=exc, id=entry.id)
                outcome, why = None, API_REFUSED
            if outcome is None:
                waiting[entry.id] = why
                continue
            started.append(outcome)
            asked_sync = True
        # The others see the data of the newest sync boundary: they wait for its run.
        synced = not sync_job and not asked_sync
        synced = synced and all(self._done(entry, boundary(now, entry.interval)) for entry in syncs)
        for entry in others:
            due = boundary(now, entry.interval)
            if self._done(entry, due):
                continue
            if not synced:
                waiting[entry.id] = SYNC_NOT_ENDED
                continue
            try:
                outcome, why = self._ask_other(entry, due, now)
            except Exception as exc:
                log.error("asking for a schedule entry failed", exc=exc, id=entry.id)
                outcome, why = None, API_REFUSED
            if outcome is None:
                if why is not None:
                    waiting[entry.id] = why
            else:
                started.append(outcome)
        for entry_id, reason in waiting.items():
            if self._waiting.get(entry_id) != reason:
                log.info("a schedule entry waits", id=entry_id, reason=reason)
        self._waiting = waiting
        if started:
            self._idle_reason = None
            return {
                "started": [found["id"] for found in started],
                "fired": started,
                "waiting": waiting,
            }
        reason = next(iter(waiting.values()), NOT_DUE)
        return {"started": None, "reason": reason, "waiting": waiting}

    def _done(self, entry: Entry, due: datetime) -> bool:
        asked = self._asked.get(entry.id)
        return asked is not None and due <= asked

    def _request(self, entry: Entry, due: datetime) -> tuple[httpx.Response | None, str, float]:
        key = entry.key_at(due)
        started = time.perf_counter()
        try:
            response = self._http.request(
                entry.method, entry.path, headers={"Idempotency-Key": key}, json=entry.body
            )
        except httpx.HTTPError as exc:
            log.warn(
                "a schedule entry could not be asked for; it is asked for again on the next pass",
                id=entry.id,
                due=due.isoformat(),
                error_type=type(exc).__name__,
            )
            return None, key, 0.0
        return response, key, round((time.perf_counter() - started) * 1000.0, 1)

    def _ask_sync(
        self, entry: Entry, due: datetime, now: datetime
    ) -> tuple[dict[str, Any] | None, str]:
        """The sync run of a boundary asked for, or why it was not."""
        response, key, elapsed = self._request(entry, due)
        if response is None:
            return None, API_UNREACHABLE
        if response.status_code >= 400:
            log.warn(
                "the API refused to start a sync run; it is asked for again on the next pass",
                id=entry.id,
                due=due.isoformat(),
                status=response.status_code,
                code=_problem_code(response),
                duration_ms=elapsed,
            )
            return None, API_REFUSED
        job = _json(response).get("job") or {}
        if response.status_code == 200 and job.get("idempotency_key") != key:
            # Another run began between the look and the request and was handed back.
            return None, SYNC_IN_PROGRESS
        return self._asked_for(entry, due, now, key, job, elapsed), NOT_DUE

    def _ask_other(
        self, entry: Entry, due: datetime, now: datetime
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Another entry of a boundary asked for; or why not, None when it is not asked
        for again before its next boundary."""
        response, key, elapsed = self._request(entry, due)
        if response is None:
            return None, API_UNREACHABLE
        status = response.status_code
        if status >= 500 or status in _TRANSIENT:
            log.warn(
                "the API could not take a schedule entry now; it is asked for again on the "
                "next pass",
                id=entry.id,
                due=due.isoformat(),
                status=status,
                code=_problem_code(response),
                duration_ms=elapsed,
            )
            return None, API_REFUSED
        if status >= 400:
            # Asking again with the same request gets the same answer: the boundary is
            # counted as asked for, and the next one is asked for in its turn.
            log.error(
                "the API refused a schedule entry; it is asked for again at its next boundary",
                id=entry.id,
                due=due.isoformat(),
                status=status,
                code=_problem_code(response),
                duration_ms=elapsed,
            )
            self._asked[entry.id] = due
            return None, None
        job = _json(response).get("job") or {}
        return self._asked_for(entry, due, now, key, job, elapsed), None

    def _asked_for(
        self,
        entry: Entry,
        due: datetime,
        now: datetime,
        key: str,
        job: dict[str, Any],
        elapsed: float,
    ) -> dict[str, Any]:
        last = self._asked.get(entry.id)
        missed = 0 if last is None else (due - last) // entry.interval - 1
        self._asked[entry.id] = due
        log.info(
            f"{entry.kind} run started" if entry.kind == "sync" else "schedule entry started",
            id=entry.id,
            kind=entry.kind,
            due=due.isoformat(),
            now=now.isoformat(),
            job=job.get("id"),
            intervals_missed=missed,
            duration_ms=elapsed,
        )
        return {"id": entry.id, "due": due.isoformat(), "key": key, "job": job.get("id")}

    # -- an API without a schedule -------------------------------------------------

    def _tick_sync_only(self, health: dict[str, Any]) -> dict[str, Any]:
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
        if (health.get("sync") or {}).get("job"):
            # A run still going began before this boundary and leaves its interval
            # untaken. Asked now, the API would hand that run back; the boundary's own
            # run is asked for once it has ended.
            return self._idle(SYNC_IN_PROGRESS)
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
        job = _json(response).get("job") or {}
        if response.status_code == 200 and job.get("idempotency_key") != key:
            # Another run began between the look and the request and was handed back.
            return self._idle(SYNC_IN_PROGRESS)
        missed = 0 if self._fired is None else (due - self._fired) // self._interval - 1
        self._fired, self._idle_reason = due, None
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

    # -- shared ---------------------------------------------------------------------

    def _idle(self, reason: str, *, quiet: bool = False) -> dict[str, Any]:
        # A reason is logged when it begins, not on every pass it lasts.
        if not quiet and reason != self._idle_reason:
            log.warn("nothing started this pass", reason=reason)
        if not quiet:
            self._idle_reason = reason
        return {"started": None, "reason": reason}

    def _fetch(self, path: str) -> tuple[int | None, dict[str, Any] | None]:
        try:
            response = self._http.get(path)
        except httpx.HTTPError as exc:
            log.debug("the API did not answer", path=path, error_type=type(exc).__name__)
            return None, None
        if response.status_code != 200:
            return response.status_code, None
        return 200, _json(response)

    def _get(self, path: str) -> dict[str, Any] | None:
        status, document = self._fetch(path)
        if status is not None and status != 200:
            log.warn("the API refused a read", path=path, status=status)
        return document

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
