"""How an HTTP source's failures are retried and reported: rate limits, server
errors, a server that is down or slow, refused credentials, missing paths,
bodies that are not JSON, and redirects or links that lead elsewhere."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from email.utils import formatdate
from typing import Any

import pytest
from dta_connect_testkit import CapturedLogs
from dta_core.sources import (
    SourceAuthFailed,
    SourceError,
    SourceRateLimited,
    SourceReadError,
    SourceUnavailable,
)
from dta_http_testkit import Api, Reply, connect, cursor_route, readings, sequence, table_rows

ROWS = readings(12)
DATASET = {
    "path": "/r",
    "records": "data",
    "paging": {"type": "cursor", "cursor_path": "next_cursor"},
}
FAST = {"max_wait_s": 0.2}


def _read(api: Api | str, dataset: dict[str, Any] | None = None, **options: Any) -> list[Any]:
    with connect(api, {"r": dataset or DATASET}, **options) as connector:
        return table_rows(connector, "r")


def _failure(api: Api | str, cls: type[SourceError], **options: Any) -> SourceError:
    with pytest.raises(cls) as caught:
        _read(api, **options)
    error = caught.value
    assert error.detail["source"] == "plant"
    assert error.detail["dataset"] == "r"
    return error


def _rate_limited(retry_after: str) -> Reply:
    return Reply({"error": {"code": "rate_limited"}}, 429, {"Retry-After": retry_after})


def _retries(logs: CapturedLogs) -> list[dict[str, Any]]:
    """The retry warnings, once each: the capture sees a record at two loggers."""
    unique = {id(record): record for record in logs.records if record.levelname == "WARNING"}
    return [record.data for record in unique.values()]  # type: ignore[attr-defined]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestRateLimit:
    def test_a_wait_within_the_budget_is_waited_out(
        self, api: Api, captured_logs: CapturedLogs
    ) -> None:
        pages = cursor_route(ROWS, 5)
        limited = {"done": False}

        def route(call: Any) -> Reply:
            if call.arg("cursor") == "5" and not limited["done"]:
                limited["done"] = True
                return _rate_limited("1")
            return pages(call)

        api.route("/r", route)
        rows = _read(api, max_wait_s=5)
        assert len(rows) == len(ROWS)
        second = [call for call in api.calls_to("/r") if call.arg("cursor") == "5"]
        assert len(second) == 2
        assert second[1].at - second[0].at >= 0.95
        retries = _retries(captured_logs)
        assert len(retries) == 1
        assert retries[0]["status"] == 429 and retries[0]["retry_after_s"] == 1.0
        assert retries[0]["path"] == "/r" and retries[0]["attempt"] == 1

    def test_a_wait_beyond_the_budget_fails_at_once(self, api: Api) -> None:
        api.route("/r", _rate_limited("30"))
        error = _failure(api, SourceRateLimited, max_wait_s=2)
        assert error.detail["retry_after_s"] == 30.0
        assert error.detail["status"] == 429 and error.detail["attempts"] == 1
        assert len(api.calls_to("/r")) == 1

    def test_a_retry_date_is_measured_against_the_response_date(self, api: Api) -> None:
        api.route("/r", lambda call: _rate_limited(formatdate(time.time() + 30, usegmt=True)))
        error = _failure(api, SourceRateLimited, max_wait_s=2)
        assert 28.0 <= error.detail["retry_after_s"] <= 31.0
        assert len(api.calls_to("/r")) == 1

    def test_still_refused_after_the_attempts(self, api: Api) -> None:
        api.route("/r", _rate_limited("1"))
        error = _failure(api, SourceRateLimited, max_wait_s=5, max_attempts=2)
        assert error.detail["retry_after_s"] == 1.0 and error.detail["attempts"] == 2
        assert len(api.calls_to("/r")) == 2


class TestUnavailable:
    def test_a_server_error_that_recovers(self, api: Api, captured_logs: CapturedLogs) -> None:
        api.route("/r", sequence(Reply({"error": {}}, 500), Reply({"data": ROWS})))
        assert len(_read(api, **FAST)) == len(ROWS)
        assert len(api.calls_to("/r")) == 2
        retries = _retries(captured_logs)
        assert [(w["status"], w["attempt"], w["max_attempts"]) for w in retries] == [(500, 1, 5)]

    def test_a_server_error_that_stays(self, api: Api, captured_logs: CapturedLogs) -> None:
        api.route("/r", Reply({"error": {}}, 503))
        error = _failure(api, SourceUnavailable, max_attempts=3, **FAST)
        assert error.detail["status"] == 503 and error.detail["attempts"] == 3
        assert error.detail["path"] == "/r"
        assert len(api.calls_to("/r")) == 3
        assert [w["attempt"] for w in _retries(captured_logs)] == [1, 2]
        assert error.message in captured_logs.messages("error")

    def test_a_server_that_is_down(self, captured_logs: CapturedLogs) -> None:
        address = f"http://127.0.0.1:{_free_port()}"
        error = _failure(address, SourceUnavailable, max_attempts=2, **FAST)
        assert "Connection refused" in error.message
        assert error.detail["attempts"] == 2 and error.detail["path"] == "/r"
        retries = _retries(captured_logs)
        assert len(retries) == 1 and retries[0]["error"] == "ConnectionError: Connection refused"

    def test_a_server_that_does_not_answer_in_time(self, api: Api) -> None:
        api.route("/r", Reply({"data": ROWS}, delay_s=1.0))
        error = _failure(api, SourceUnavailable, timeout_s=0.3, max_attempts=1)
        assert "no answer within 0.3 s" in error.message


class TestRefused:
    @pytest.mark.parametrize("status", [401, 403])
    def test_credentials_refused(self, api: Api, status: int) -> None:
        api.route("/r", Reply({"error": {"code": "unauthorized"}}, status))
        error = _failure(api, SourceAuthFailed)
        assert error.detail["status"] == status and error.detail["attempts"] == 1
        assert len(api.calls_to("/r")) == 1

    @pytest.mark.parametrize("status", [404, 400, 422])
    def test_other_client_errors(self, api: Api, status: int) -> None:
        if status != 404:
            api.route("/r", Reply({"error": {"code": "bad"}}, status))
        error = _failure(api, SourceReadError)
        assert error.detail["status"] == status
        assert error.detail["path"] == "/r"
        assert f"HTTP {status}" in error.message

    def test_a_body_that_is_not_json(self, api: Api) -> None:
        api.route("/r", Reply(b"<html>maintenance</html>", headers={"Content-Type": "text/html"}))
        error = _failure(api, SourceReadError)
        assert "not JSON" in error.message
        assert error.detail["path"] == "/r"

    def test_a_missing_total_is_reported_without_the_body(self, api: Api) -> None:
        api.route("/r", Reply({"items": [{"secret_value": "row-data-123"}]}))
        dataset = {"path": "/r", "records": "items", "paging": {"type": "page", "total_path": "n"}}
        with pytest.raises(SourceReadError) as caught:
            _read(api, dataset)
        assert "paging" in caught.value.message
        assert "row-data-123" not in str(caught.value.as_dict())


class TestElsewhere:
    def test_a_redirect_within_the_source_is_followed(self, api: Api) -> None:
        api.route("/old", Reply(b"", 301, {"Location": "/r"}))
        api.route("/r", Reply({"data": ROWS}))
        assert len(_read(api, {"path": "/old", "records": "data"})) == len(ROWS)

    def test_a_redirect_to_another_host_is_not_followed(
        self, serve: Callable[[], Api], captured_logs: CapturedLogs
    ) -> None:
        api, other = serve(), serve()
        other.route("/r", Reply({"data": ROWS}))
        api.route("/r", Reply(b"", 302, {"Location": f"{other.url}/r"}))
        with pytest.raises(SourceReadError, match="another host"):
            _read(api, headers={"X-API-Key": "${DTA_TEST_HTTP_KEY}"})
        assert other.calls == []

    def test_a_next_link_to_another_host_is_not_followed(self, serve: Callable[[], Api]) -> None:
        api, other = serve(), serve()
        other.route("/r", Reply({"data": ROWS, "next": None}))
        api.route("/r", Reply({"data": ROWS, "next": f"{other.url}/r?page=2"}))
        dataset = {
            "path": "/r",
            "records": "data",
            "paging": {"type": "link", "next_url_path": "next"},
        }
        with pytest.raises(SourceReadError, match="another host"):
            _read(api, dataset)
        assert other.calls == []
