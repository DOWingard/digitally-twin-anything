"""Where a twin reads the current time.

Twin logic never asks the operating system what time it is. Whatever needs
`now` (what is due, what is stale, up to which moment data may be read) is
handed a clock, chosen by `runtime.clock` in the manifest:

- `wall`: the time on the wall, for a twin of a system that lives in real time;
- `source:<id>`: the clock of one of the twin's sources, for a system whose
  data runs on a clock of its own: a simulation, a replay, a plant in another
  time zone of its own making.

A version's time is always the time of the data it holds; `now` only decides
what happens next and is never written as a version's time by itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from dta_core.connect import open_connector
from dta_core.errors import ConfigError, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.sources import Connector, SourceError, TimeSource

log = Log("runtime.clock")

WALL = "wall"
SOURCE_PREFIX = "source:"
# How long a source's answer is reused, in seconds on the wall: one request
# serves every caller of a tick instead of one request each.
SOURCE_TTL_S = 1.0


class ClockUnavailable(DtaError):
    """The twin's clock cannot be read right now. Whatever depends on `now` waits."""

    code = "clock.unavailable"


class Clock(Protocol):
    kind: str

    def now(self) -> datetime:
        """The current time, with an offset. Raises `ClockUnavailable`."""
        ...

    def close(self) -> None: ...


class WallClock:
    """The operating system's clock. The one place twin code reads it."""

    kind = WALL

    def now(self) -> datetime:
        return datetime.now(UTC)

    def close(self) -> None:
        return None


class FixedClock:
    """A clock that tells the time it was given; tests and replays move it by hand."""

    kind = "fixed"

    def __init__(self, at: datetime) -> None:
        self.set(at)

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("a clock's time needs an offset")
        self._at = at

    def advance(self, by: timedelta) -> datetime:
        self._at += by
        return self._at

    def now(self) -> datetime:
        return self._at

    def close(self) -> None:
        return None


class SourceClock:
    """The clock of one of the twin's sources.

    The source is opened on first use and kept open. An answer is reused for
    `ttl_s` seconds on the wall. A failed read closes the connector, so the
    next call starts from a fresh connection, and raises `ClockUnavailable`:
    the caller skips its tick and health reports the clock as unavailable.
    """

    def __init__(
        self,
        source_id: str,
        connect: Callable[[], Connector],
        *,
        ttl_s: float = SOURCE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kind = f"{SOURCE_PREFIX}{source_id}"
        self.source_id = source_id
        self._connect = connect
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._connector: Connector | None = None
        self._read_at: float | None = None
        self._value: datetime | None = None
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            moment = self._monotonic()
            if (
                self._value is not None
                and self._read_at is not None
                and moment - self._read_at < self._ttl_s
            ):
                return self._value
            started = time.perf_counter()
            try:
                value = self._source().now()
            except (SourceError, ConfigError) as exc:
                self._drop()
                log.warn(
                    "the source's clock cannot be read; the tick is skipped",
                    source=self.source_id,
                    code=exc.code,
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
                )
                raise ClockUnavailable(
                    f"the clock of source '{self.source_id}' cannot be read: {exc.message}",
                    detail={"source": self.source_id, "cause": exc.code},
                ) from exc
            if value.tzinfo is None:
                self._drop()
                log.error("the source's clock gave a time without an offset", source=self.source_id)
                raise ClockUnavailable(
                    f"the clock of source '{self.source_id}' gave a time without an offset",
                    detail={"source": self.source_id, "cause": "clock.no_offset"},
                )
            self._value, self._read_at = value.astimezone(UTC), moment
            log.debug(
                "source clock read",
                source=self.source_id,
                now=self._value.isoformat(),
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )
            return self._value

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _source(self) -> TimeSource:
        if self._connector is None:
            self._connector = self._connect()
        if not isinstance(self._connector, TimeSource):
            raise ConfigError(
                f"source '{self.source_id}' does not tell the time: give it a clock option",
                code="clock.unsupported",
                detail={"source": self.source_id},
            )
        return self._connector

    def _drop(self) -> None:
        connector, self._connector = self._connector, None
        self._value = self._read_at = None
        if connector is not None:
            connector.close()


def clock_for(
    manifest: Manifest,
    env: Mapping[str, str],
    *,
    connect: Callable[..., Connector] = open_connector,
) -> Clock:
    """The clock `runtime.clock` names. The source of a source clock is opened on first use."""
    kind = manifest.runtime.clock
    if kind == WALL:
        return WallClock()
    source_id = kind.removeprefix(SOURCE_PREFIX)
    source = manifest.sources.get(source_id)
    if source is None:
        log.error("runtime.clock names a source the manifest does not have", source=source_id)
        raise ConfigError(
            f"runtime.clock names source '{source_id}', which the manifest does not have",
            code="clock.unknown_source",
            detail={"source": source_id, "sources": sorted(manifest.sources)},
        )
    log.info("the twin's clock is a source's clock", source=source_id)
    return SourceClock(source_id, lambda: connect(source_id, source, env))
