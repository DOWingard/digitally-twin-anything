"""The source's clock: one GET of the clock path, the named field as an aware time."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dta_core.sources import SourceReadError, SourceUnavailable, TimeSource
from dta_http_testkit import Api, Reply, connect

DATASETS = {"r": {"path": "/r"}}
CLOCK = {"path": "/api/v1/time", "field": "time"}


def _now(api: Api | str, clock: dict[str, Any] | None = None, **options: Any) -> datetime:
    with connect(api, DATASETS, clock=clock or CLOCK, **options) as connector:
        assert isinstance(connector, TimeSource)
        return connector.now()


def test_the_clock_is_read(api: Api) -> None:
    api.route("/api/v1/time", Reply({"time": "2026-01-19T06:15:00Z", "step_seconds": 900}))
    assert _now(api) == datetime(2026, 1, 19, 6, 15, tzinfo=UTC)
    assert len(api.calls_to("/api/v1/time")) == 1


def test_the_offset_is_kept_and_a_nested_field_is_found(api: Api) -> None:
    api.route("/clock", Reply({"data": {"now": "2026-01-19T07:15:00+01:00"}}))
    moment = _now(api, {"path": "/clock", "field": "data.now"})
    assert moment.utcoffset() == timedelta(hours=1)
    assert moment == datetime(2026, 1, 19, 6, 15, tzinfo=UTC)


def test_without_a_clock_the_connector_tells_no_time(api: Api) -> None:
    with connect(api, DATASETS) as connector:
        assert not isinstance(connector, TimeSource)
        assert not hasattr(connector, "now")


@pytest.mark.parametrize(
    "body",
    [
        {"time": "2026-01-19T06:15:00"},
        {"time": "2026-01-19"},
        {"time": "soon"},
        {"time": 1768803300},
        {"other": "2026-01-19T06:15:00Z"},
        ["2026-01-19T06:15:00Z"],
    ],
)
def test_a_time_without_an_offset_or_no_time_is_refused(api: Api, body: Any) -> None:
    api.route("/api/v1/time", Reply(body))
    with pytest.raises(SourceReadError) as caught:
        _now(api)
    assert caught.value.detail["path"] == "/api/v1/time"


def test_a_clock_that_is_not_json(api: Api) -> None:
    api.route("/api/v1/time", Reply(b"06:15", headers={"Content-Type": "text/plain"}))
    with pytest.raises(SourceReadError, match="not JSON"):
        _now(api)


def test_the_clock_down(api: Api) -> None:
    api.route("/api/v1/time", Reply({"error": {}}, 503))
    with pytest.raises(SourceUnavailable) as caught:
        _now(api, max_attempts=2, max_wait_s=0.1)
    assert caught.value.detail["status"] == 503
    assert caught.value.detail["attempts"] == 2
    assert len(api.calls_to("/api/v1/time")) == 2


def test_the_clock_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(SourceUnavailable, match="Connection refused"):
        _now(f"http://127.0.0.1:{port}", max_attempts=1)
