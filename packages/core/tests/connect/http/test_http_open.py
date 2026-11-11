"""An HTTP source opened from its manifest entry: placeholders in the location and
in a header, `probe` and `profile_source` on the connector, and the guarantee
that neither a header value nor the filled-in address reaches a log line or an
error."""

from __future__ import annotations

import json
import secrets
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dta_connect_testkit import CapturedLogs
from dta_core.connect import open_connector, probe, profile_source
from dta_core.connect.http import parse_options
from dta_core.errors import DtaError
from dta_core.manifest.models import Source
from dta_core.sources import Connector, SourceConfigError, SourceError, TimeSource
from dta_http_testkit import (
    KEY_VAR,
    LOCATION_VAR,
    Api,
    Reply,
    cursor_route,
    readings,
    source,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)
COLUMNS = {
    "sensor_id": "string",
    "timestamp": "timestamp[us, tz=UTC]",
    "value": "double",
    "quality": "string",
}
CURSOR = {"type": "cursor", "cursor_path": "next_cursor"}


def _plant(api: Api) -> dict[str, Any]:
    """An interface like a plant's: a clock, paged readings, a catalogue, failing paths."""
    rows = readings(300)
    api.route("/api/v1/time", Reply({"time": "2026-01-19T07:15:00Z", "step_seconds": 900}))
    api.route("/api/v1/telemetry", cursor_route(rows, 100, since=("start", "iso")))
    api.route("/api/v1/sensors", Reply({"data": [{"sensor_id": f"S-{i}"} for i in range(3)]}))
    api.route("/api/v1/empty", Reply({"data": []}))
    api.route("/api/v1/locked", Reply({"error": {"code": "unauthorized"}}, 401))
    api.route("/api/v1/busy", Reply({"error": {}}, 429, {"Retry-After": "120"}))
    api.route("/api/v1/broken", Reply(b"<html>oops</html>", headers={"Content-Type": "text/html"}))
    api.route("/api/v1/failing", Reply({"error": {}}, 500))
    return {
        "readings": {
            "path": "/api/v1/telemetry",
            # A secret may be a parameter as well as a header.
            "params": {"limit": 100, "token": "${" + KEY_VAR + "}"},
            "records": "data",
            "paging": CURSOR,
            "since": {"param": "start", "format": "iso"},
            "columns": COLUMNS,
        },
        "sensors": {"path": "/api/v1/sensors", "records": "data"},
        "empty": {"path": "/api/v1/empty", "records": "data", "columns": {"id": "int64"}},
        "empty_inferred": {"path": "/api/v1/empty", "records": "data"},
        "locked": {"path": "/api/v1/locked"},
        "busy": {"path": "/api/v1/busy"},
        "broken": {"path": "/api/v1/broken"},
        "failing": {"path": "/api/v1/failing"},
        "missing": {"path": "/api/v1/nothing-here"},
    }


def _options() -> dict[str, Any]:
    return {
        "headers": {"X-API-Key": "${" + KEY_VAR + "}"},
        "clock": {"path": "/api/v1/time", "field": "time"},
        "max_attempts": 2,
        "max_wait_s": 0.1,
    }


def _open(api: Api, key: str) -> Connector:
    entry = source(_plant(api), **_options())
    return open_connector("plant", entry, {LOCATION_VAR: api.url, KEY_VAR: key})


class TestThroughTheFactory:
    def test_placeholders_probe_and_profile(self, api: Api) -> None:
        key = secrets.token_hex(12)
        with _open(api, key) as connector:
            assert isinstance(connector, TimeSource)
            assert connector.now() == datetime(2026, 1, 19, 7, 15, tzinfo=UTC)
            assert connector.datasets() == sorted(_plant(api))
            report = probe(connector)
            since = datetime(2026, 1, 19, 6, 0, tzinfo=UTC) + timedelta(hours=24)
            newer = list(connector.read("readings", cursor_column="timestamp", since=since))
            profile = profile_source(connector, profiled_at=NOW, datasets=["readings", "sensors"])
        assert all(call.headers["X-API-Key"] == key for call in api.calls)
        assert all(
            call.arg("token") == key for call in api.calls if call.path == "/api/v1/telemetry"
        )

        by_name = {d.dataset: d for d in report.datasets}
        assert by_name["readings"].ok and by_name["readings"].has_rows
        assert [(c.name, c.type) for c in by_name["readings"].columns] == [
            ("sensor_id", "string"),
            ("timestamp", "timestamp[us, tz=UTC]"),
            ("value", "double"),
            ("quality", "string"),
        ]
        assert by_name["sensors"].ok and by_name["empty"].ok and not by_name["empty"].has_rows
        failed = {d.dataset: d.error_code for d in report.datasets if not d.ok}
        assert failed == {
            "empty_inferred": "source.invalid_config",
            "locked": "source.auth_failed",
            "busy": "source.rate_limited",
            "broken": "source.read_failed",
            "failing": "source.unavailable",
            "missing": "source.read_failed",
        }

        # 100 instants of 3 sensors 15 minutes apart: from 24 hours on the source sends 4
        # instants, as its start is inclusive, and 3 of them are newer.
        assert sum(t.num_rows for t in newer) == 3 * 3

        readings_profile = profile.datasets["readings"]
        assert readings_profile.rows == 300
        assert readings_profile.time_column == "timestamp"
        assert readings_profile.entity_columns == ["sensor_id"]
        assert readings_profile.refresh_period == timedelta(minutes=15)
        assert profile.datasets["sensors"].rows == 3

    def test_a_location_that_is_not_http_is_refused(self) -> None:
        entry = Source(
            connector="http", location="${URL}", options={"datasets": {"r": {"path": "/r"}}}
        )
        for address in ("ftp://plant:21", "plant:8080", "http://plant:8080/?q=1", "http://plant:x"):
            with pytest.raises(SourceConfigError) as caught:
                open_connector("plant", entry, {"URL": address})
            assert address not in json.dumps(caught.value.as_dict())


class TestSecretsStayHidden:
    def test_no_header_value_or_address_in_logs_or_errors(
        self,
        serve: Callable[[], Api],
        captured_logs: CapturedLogs,
    ) -> None:
        api, elsewhere = serve(), serve()
        api.route("/api/v1/moved", Reply(b"", 302, {"Location": f"{elsewhere.url}/x"}))
        key = secrets.token_hex(12)
        errors: list[str] = []
        with _open(api, key) as connector:
            report = probe(connector)
            errors.append(json.dumps(report.as_dict()))
            connector.now()
            list(connector.read("readings"))
            profile_source(connector, profiled_at=NOW, datasets=["readings"])
            for dataset in ("locked", "busy", "broken", "failing", "missing", "empty_inferred"):
                errors.append(_error_text(lambda c, d=dataset: list(c.read(d)), connector))
            errors.append(_error_text(lambda c: list(c.read("nope")), connector))
            errors.append(_error_text(lambda c: list(c.read("readings", columns=["x"])), connector))
        moved = source({"moved": {"path": "/api/v1/moved"}}, **_options())
        with open_connector("plant", moved, {LOCATION_VAR: api.url, KEY_VAR: key}) as connector:
            errors.append(_error_text(lambda c: list(c.read("moved")), connector))
        assert elsewhere.calls == []

        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            down = f"http://127.0.0.1:{spare.getsockname()[1]}"
        entry = source(_plant(api), **_options())
        with open_connector("plant", entry, {LOCATION_VAR: down, KEY_VAR: key}) as connector:
            errors.append(_error_text(lambda c: c.now(), connector))  # type: ignore[attr-defined]
            errors.append(_error_text(lambda c: list(c.read("readings")), connector))

        assert len(errors) == 12
        logged = captured_logs.text()
        assert captured_logs.messages("error") and captured_logs.messages("warning")
        for hidden in (key, api.url, api.url.removeprefix("http://"), down.removeprefix("http://")):
            assert hidden not in logged
            for text in errors:
                assert hidden not in text

    def test_parsed_options_do_not_show_header_or_parameter_values(self) -> None:
        settings = parse_options(
            {
                "headers": {"X-API-Key": "s3cr3t-value"},
                "datasets": {"r": {"path": "/r", "params": {"token": "s3cr3t-param"}}},
            }
        )
        assert "s3cr3t" not in repr(settings)

    def test_a_malformed_header_names_the_option_not_the_value(self) -> None:
        options = {"headers": {"X-API-Key": "s3cr3t\nvalue"}, "datasets": {"r": {"path": "/r"}}}
        with pytest.raises(SourceConfigError) as caught:
            parse_options(options)
        assert caught.value.detail["option"] == "headers.X-API-Key"
        assert "s3cr3t" not in json.dumps(caught.value.as_dict())


def _error_text(failing: Callable[[Connector], Any], connector: Connector) -> str:
    with pytest.raises(DtaError) as caught:
        failing(connector)
    assert isinstance(caught.value, SourceError)
    return json.dumps(caught.value.as_dict()) + str(caught.value)
