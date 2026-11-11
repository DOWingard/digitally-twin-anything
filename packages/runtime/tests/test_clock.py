"""The twin's clock: the wall, a time set by hand, or the clock of a source."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from dta_core.errors import ConfigError
from dta_core.manifest import parse_manifest
from dta_core.sources import SourceUnavailable
from dta_runtime.clock import ClockUnavailable, FixedClock, SourceClock, WallClock, clock_for

T0 = datetime(2026, 1, 19, 0, 0, tzinfo=UTC)


class Ticker:
    """A stand-in for a connector whose source tells the time."""

    source_id = "plant"

    def __init__(self, times: list[Any]) -> None:
        self.times = times
        self.reads = 0
        self.closed = 0

    def now(self) -> datetime:
        self.reads += 1
        value = self.times.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.closed += 1


class Mute:
    """A connector without a clock."""

    source_id = "files"

    def close(self) -> None:
        return None


def test_the_wall_clock_tells_utc_time_with_an_offset():
    now = WallClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_a_fixed_clock_moves_only_by_hand_and_refuses_a_time_without_an_offset():
    clock = FixedClock(T0)
    assert clock.now() == T0 == clock.now()
    assert clock.advance(timedelta(minutes=15)) == T0 + timedelta(minutes=15)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 1, 19))


def test_a_source_clock_reuses_an_answer_for_a_short_time_then_reads_again():
    wall = [0.0]
    ticker = Ticker([T0, T0 + timedelta(minutes=15)])
    clock = SourceClock("plant", lambda: ticker, ttl_s=1.0, monotonic=lambda: wall[0])
    assert clock.kind == "source:plant"
    assert clock.now() == T0
    wall[0] = 0.9
    assert clock.now() == T0 and ticker.reads == 1
    wall[0] = 1.0
    assert clock.now() == T0 + timedelta(minutes=15) and ticker.reads == 2


def test_a_source_clock_answers_in_utc_whatever_offset_the_source_uses():
    local = datetime(2026, 1, 19, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    clock = SourceClock("plant", lambda: Ticker([local]))
    assert clock.now() == T0 and clock.now().utcoffset() == timedelta(0)


def test_an_unreachable_source_makes_the_clock_unavailable_and_it_recovers():
    opened: list[Ticker] = []

    def connect() -> Ticker:
        times: list[Any] = [SourceUnavailable("no answer")] if not opened else [T0]
        opened.append(Ticker(times))
        return opened[-1]

    clock = SourceClock("plant", connect, ttl_s=0.0)
    with pytest.raises(ClockUnavailable) as caught:
        clock.now()
    assert caught.value.code == "clock.unavailable"
    assert caught.value.detail == {"source": "plant", "cause": "source.unavailable"}
    assert opened[0].closed == 1  # the next read starts from a fresh connection
    assert clock.now() == T0 and len(opened) == 2
    clock.close()
    assert opened[1].closed == 1


def test_a_time_without_an_offset_is_not_a_time():
    clock = SourceClock("plant", lambda: Ticker([datetime(2026, 1, 19)]))
    with pytest.raises(ClockUnavailable) as caught:
        clock.now()
    assert caught.value.detail["cause"] == "clock.no_offset"


def test_a_source_without_a_clock_is_a_configuration_error_reported_as_unavailable():
    clock = SourceClock("files", lambda: Mute())  # type: ignore[arg-type,return-value]
    with pytest.raises(ClockUnavailable) as caught:
        clock.now()
    assert caught.value.detail["cause"] == "clock.unsupported"


def _manifest(clock: str) -> Any:
    return parse_manifest(
        {
            "twin": {"id": "plant", "name": "Plant"},
            "sources": {"plant": {"connector": "http", "location": "http://plant:8080"}},
            "runtime": {"clock": clock},
        }
    )


def test_the_manifest_chooses_the_clock_and_the_source_is_opened_only_when_asked():
    assert isinstance(clock_for(_manifest("wall"), {}), WallClock)
    calls: list[tuple[str, Any]] = []

    def connect(source_id: str, source: Any, env: Any) -> Ticker:
        calls.append((source_id, env))
        return Ticker([T0])

    clock = clock_for(_manifest("source:plant"), {"A": "b"}, connect=connect)
    assert calls == []
    assert clock.now() == T0 and calls == [("plant", {"A": "b"})]
    with pytest.raises(ConfigError) as caught:
        clock_for(_manifest("source:other"), {})
    assert caught.value.code == "clock.unknown_source"
