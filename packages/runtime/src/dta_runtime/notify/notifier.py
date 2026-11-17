"""What the twin tells the outside about itself, as CloudEvents 1.0 in structured JSON.

`emit` puts one event on a bounded queue and returns at once; one thread delivers the
events in the order they were emitted, each by a POST to the webhook. An event is tried
three times, waiting `backoff_s` between tries; any 2xx answer delivers it. A queue that
is full drops its oldest undelivered event to make room. Closing delivers what it can
for at most `drain_s`.

The webhook's address is a secret: it is read from the environment, and it is never
logged, listed or put in an error. An event's `time` is the data time of what it
reports, never the wall clock.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from dta_core.config import require_env
from dta_core.errors import ConfigError
from dta_core.log import Log

log = Log("notify")

SPEC_VERSION = "1.0"
EVENT_MEDIA = "application/cloudevents+json"
DATA_MEDIA = "application/json"
WEBHOOK = "webhook"
LOG = "log"

TRIES = 3
BACKOFF_S: tuple[float, ...] = (1.0, 5.0)
QUEUE_CAPACITY = 1_000
KEPT = 200
DRAIN_S = 5.0
REQUEST_TIMEOUT_S = 10.0

# What became of an event.
QUEUED = "queued"
DELIVERED = "delivered"
FAILED = "failed"
DROPPED = "dropped"
LOGGED = "logged"
UNSENT = "unsent"


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("an event's time needs an offset")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class Notice:
    """One event and what became of it."""

    sequence: int
    event: dict[str, Any]
    status: str = QUEUED
    tries: int = 0
    # The HTTP status of the last answer, or None when there was none.
    http_status: int | None = None
    # Why the last try failed: an HTTP status or the kind of connection error.
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "id": self.event["id"],
            "type": self.event["type"],
            "subject": self.event.get("subject"),
            "time": self.event["time"],
            "status": self.status,
            "tries": self.tries,
            "http_status": self.http_status,
            "error": self.error,
            "data": self.event.get("data"),
        }


class Notifier:
    """The API's one notifier: a queue and the thread that delivers it."""

    def __init__(
        self,
        *,
        twin_id: str,
        kind: str,
        address: str | None = None,
        variable: str | None = None,
        backoff_s: Sequence[float] = BACKOFF_S,
        capacity: int = QUEUE_CAPACITY,
        kept: int = KEPT,
        timeout_s: float = REQUEST_TIMEOUT_S,
        client: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if kind == WEBHOOK and not address:
            raise ValueError("a webhook notifier needs an address")
        if len(backoff_s) != TRIES - 1:
            raise ValueError(f"backoff_s holds {TRIES - 1} waits, one between two tries")
        self.source = f"/twins/{twin_id}"
        self.kind = kind
        # Only the variable's name is ever told; the address it holds never is.
        self.variable = variable
        self._address = address
        self._backoff = tuple(float(wait) for wait in backoff_s)
        self._capacity = capacity
        self._timeout_s = timeout_s
        self._client_factory = client or (lambda: httpx.Client(follow_redirects=False))
        self._lock = threading.Condition()
        self._queue: deque[Notice] = deque()
        self._history: deque[Notice] = deque(maxlen=kept)
        self._sequence = 0
        self._thread: threading.Thread | None = None
        self._closing = False
        self._deadline: float | None = None

    # -- life -------------------------------------------------------------------------

    def start(self) -> None:
        if self.kind != WEBHOOK:
            log.info("notifications are logged, not sent", source=self.source, kind=self.kind)
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._deliver_all, name="dta-notify", daemon=True
            )
            self._thread.start()
        log.info(
            "notification delivery started",
            source=self.source,
            variable=self.variable,
            tries=TRIES,
            backoff_s=list(self._backoff),
            capacity=self._capacity,
        )

    def close(self, drain_s: float = DRAIN_S) -> None:
        """Deliver what is queued for at most `drain_s`, then stop."""
        started = time.monotonic()
        with self._lock:
            thread = self._thread
            self._closing = True
            self._deadline = started + drain_s
            self._lock.notify_all()
        if thread is not None:
            thread.join(timeout=drain_s + self._timeout_s)
            if thread.is_alive():
                log.warn("the notification thread did not stop in time", waited_s=drain_s)
        with self._lock:
            left = list(self._queue)
            self._queue.clear()
            for notice in left:
                notice.status = UNSENT
                notice.done.set()
        if left:
            log.warn(
                "notifications not delivered before the API stopped",
                count=len(left),
                ids=[notice.event["id"] for notice in left],
                types=[notice.event["type"] for notice in left],
            )
        log.info(
            "notification delivery stopped",
            unsent=len(left),
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
        )

    # -- emitting ---------------------------------------------------------------------

    def emit(
        self, type: str, subject: str | None, data: Mapping[str, Any], *, time: datetime
    ) -> Notice:
        """Queue one event; `time` is the data time of what it reports."""
        event = {
            "specversion": SPEC_VERSION,
            "id": uuid.uuid4().hex,
            "source": self.source,
            "type": type,
            "subject": subject,
            "time": _iso(time),
            "datacontenttype": DATA_MEDIA,
            "data": dict(data),
        }
        if subject is None:
            del event["subject"]
        with self._lock:
            self._sequence += 1
            notice = Notice(self._sequence, event)
            self._history.append(notice)
            if self.kind != WEBHOOK:
                notice.status = LOGGED
                notice.done.set()
            elif self._closing:
                notice.status = UNSENT
                notice.done.set()
            else:
                if len(self._queue) >= self._capacity:
                    dropped = self._queue.popleft()
                    dropped.status = DROPPED
                    dropped.done.set()
                    log.warn(
                        "the notification queue is full; its oldest event is dropped",
                        capacity=self._capacity,
                        id=dropped.event["id"],
                        type=dropped.event["type"],
                    )
                self._queue.append(notice)
                self._lock.notify_all()
        if self.kind != WEBHOOK:
            log.info(
                "notification",
                id=event["id"],
                type=type,
                subject=subject,
                time=event["time"],
                event_data=event["data"],
            )
        elif notice.status == UNSENT:
            log.warn("a notification was emitted while the API stops; not sent", type=type)
        else:
            log.debug("notification queued", id=event["id"], type=type, subject=subject)
        return notice

    def emit_all(self, events: Sequence[Mapping[str, Any]], *, time: datetime) -> None:
        """Queue events as the fidelity records give them: `type`, `subject`, `data`."""
        for found in events:
            self.emit(found["type"], found.get("subject"), found.get("data") or {}, time=time)

    def listing(self) -> list[dict[str, Any]]:
        """The events kept, newest first, with what became of them."""
        with self._lock:
            return [notice.document() for notice in reversed(self._history)]

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- delivering -------------------------------------------------------------------

    def _left(self) -> float | None:
        if self._deadline is None:
            return None
        return self._deadline - time.monotonic()

    def _next(self) -> Notice | None:
        with self._lock:
            while True:
                if self._closing:
                    left = self._left()
                    if not self._queue or (left is not None and left <= 0):
                        return None
                if self._queue:
                    return self._queue.popleft()
                self._lock.wait()

    def _deliver_all(self) -> None:
        with self._client_factory() as client:
            while True:
                notice = self._next()
                if notice is None:
                    return
                self._deliver(client, notice)

    def _pause(self, seconds: float) -> bool:
        """Wait between two tries; False when closing leaves no time for another."""
        end = time.monotonic() + seconds
        with self._lock:
            while True:
                now, deadline = time.monotonic(), self._deadline
                if deadline is not None and now >= deadline:
                    return False
                if now >= end:
                    return True
                self._lock.wait((end if deadline is None else min(end, deadline)) - now)

    def _deliver(self, client: httpx.Client, notice: Notice) -> None:
        event = notice.event
        body = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        assert self._address is not None
        for attempt in range(1, TRIES + 1):
            notice.tries = attempt
            started = time.perf_counter()
            left = self._left()
            timeout = self._timeout_s if left is None else max(0.1, min(self._timeout_s, left))
            try:
                response = client.post(
                    self._address,
                    content=body,
                    headers={"content-type": EVENT_MEDIA},
                    timeout=httpx.Timeout(timeout),
                )
            except httpx.HTTPError as exc:
                # The error's text may hold the address: only its kind is kept.
                notice.http_status, notice.error = None, type(exc).__name__
                outcome = "unreachable"
            else:
                notice.http_status, notice.error = response.status_code, None
                if 200 <= response.status_code < 300:
                    notice.status = DELIVERED
                    notice.done.set()
                    log.info(
                        "notification delivered",
                        id=event["id"],
                        type=event["type"],
                        status=response.status_code,
                        attempt=attempt,
                        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
                    )
                    return
                notice.error = f"HTTP {response.status_code}"
                outcome = "refused"
            last = attempt == TRIES
            fields = {
                "id": event["id"],
                "type": event["type"],
                "status": notice.http_status,
                "error": notice.error,
                "attempt": attempt,
                "of": TRIES,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
            if last:
                break
            wait = self._backoff[attempt - 1]
            log.warn(f"notification not delivered ({outcome}); trying again", wait_s=wait, **fields)
            if not self._pause(wait):
                log.warn("the API stops; the notification is not tried again", **fields)
                break
        notice.status = FAILED
        notice.done.set()
        log.error(
            "notification given up",
            id=event["id"],
            type=event["type"],
            status=notice.http_status,
            error=notice.error,
            attempt=notice.tries,
            of=TRIES,
        )


def address_variable(manifest: Any) -> tuple[str, str | None]:
    """The kind of notification the manifest asks for and the variable naming the webhook."""
    contract = getattr(manifest, "contract", None)
    notification = getattr(contract, "notification", None) if contract is not None else None
    if notification is None or notification.kind is None:
        return LOG, None
    if notification.kind == WEBHOOK:
        return WEBHOOK, notification.webhook_env
    return LOG, None


def notifier_for(
    manifest: Any,
    env: Mapping[str, str],
    *,
    backoff_s: Sequence[float] = BACKOFF_S,
    capacity: int = QUEUE_CAPACITY,
) -> Notifier:
    """The notifier the manifest asks for. A webhook whose variable is missing or empty
    stops the caller with a `ConfigError` naming the variable."""
    twin_id = manifest.twin.id if getattr(manifest, "twin", None) is not None else "twin"
    kind, variable = address_variable(manifest)
    if kind != WEBHOOK:
        return Notifier(twin_id=twin_id, kind=LOG, backoff_s=backoff_s, capacity=capacity)
    if not variable:
        log.error("the manifest asks for a webhook and names no variable for its address")
        raise ConfigError(
            "contract.notification.webhook_env names no variable",
            code="config.missing_env",
            detail={"missing": ["contract.notification.webhook_env"]},
        )
    address = require_env([variable], env)[variable]
    if not address.startswith(("http://", "https://")):
        log.error("the webhook's address is not an http(s) URL", variable=variable)
        raise ConfigError(
            f"{variable} does not hold an http(s) URL",
            code="config.bad_value",
            detail={"name": variable},
        )
    return Notifier(
        twin_id=twin_id,
        kind=WEBHOOK,
        address=address,
        variable=variable,
        backoff_s=backoff_s,
        capacity=capacity,
    )
