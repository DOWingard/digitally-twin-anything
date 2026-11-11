"""Opening a source from its manifest entry: kinds, options, placeholders, and the
guarantee that a filled-in location never reaches a log line or an error."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dta_connect_testkit import CapturedLogs, PostgresBackend, SqliteBackend
from dta_core.connect import ProbeReport, open_connector, probe, profile_source
from dta_core.errors import ConfigError, DtaError
from dta_core.manifest.models import Source
from dta_core.sources import Connector, SourceConfigError, SourceError, UnknownDataset

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def _exercise(connector: Connector, failing_reads: list[Callable[[Connector], Any]]) -> list[str]:
    """Probe, read and profile, then run reads expected to fail; return their errors as text."""
    report = probe(connector)
    for name in connector.datasets():
        list(connector.read(name, limit=3))
    profile_source(connector, profiled_at=NOW)
    errors = [json.dumps(report.as_dict())]
    for failing in failing_reads:
        with pytest.raises(DtaError) as caught:
            failing(connector)
        errors.append(json.dumps(caught.value.as_dict()) + str(caught.value))
    return errors


def _assert_hidden(value: str, logs: CapturedLogs, errors: list[str]) -> None:
    assert logs.records, "nothing was logged"
    assert value not in logs.text()
    for text in errors:
        assert value not in text


def _missing_column(connector: Connector) -> None:
    list(connector.read(connector.datasets()[0], columns=["no_such_column"]))


def _unknown_dataset(connector: Connector) -> None:
    list(connector.read("no_such_dataset"))


class TestPlaceholders:
    def test_a_missing_variable_is_named(self) -> None:
        source = Source(connector="sql", location="postgresql://${DB_USER}:${DB_PASSWORD}@h/db")
        with pytest.raises(ConfigError) as caught:
            open_connector("db", source, {"DB_USER": "reader", "DB_PASSWORD": ""})
        assert caught.value.code == "config.missing_env"
        assert caught.value.detail["missing"] == ["DB_PASSWORD"]
        assert "DB_PASSWORD" in caught.value.message

    def test_sqlite_directory_never_reaches_logs_or_errors(
        self, tmp_path: Path, captured_logs: CapturedLogs
    ) -> None:
        directory = tmp_path / f"secret-{secrets.token_hex(8)}"
        directory.mkdir()
        backend = SqliteBackend(directory)
        backend.execute(
            "CREATE TABLE r (id integer primary key, ts timestamp, v real)",
            "INSERT INTO r VALUES (1, '2024-01-01 00:00:00', 1.5), (2, '2024-01-01 00:01:00', 2)",
        )
        with backend.open() as connector:
            errors = _exercise(connector, [_missing_column, _unknown_dataset])
        backend.drop()
        _assert_hidden(str(directory), captured_logs, errors)

    def test_file_location_never_reaches_logs_or_errors(
        self,
        tmp_path: Path,
        captured_logs: CapturedLogs,
        http_server: Callable[[Path], str],
    ) -> None:
        directory = tmp_path / f"secret-{secrets.token_hex(8)}"
        (directory / "logs").mkdir(parents=True)
        (directory / "logs" / "a.csv").write_text("ts,v\n2024-01-01T00:00:00Z,1\n")
        (directory / "logs" / "b.csv").write_text("ts,w\n2024-01-01T00:01:00Z,2\n")
        datasets = {
            "logs": {"glob": "logs/*.csv", "format": "csv"},
            "first": {"glob": "logs/a.csv", "format": "csv"},
            "gone": {"glob": "nothing/*.csv", "format": "csv"},
        }
        base = http_server(directory)
        for location, env, hidden in [
            ("file://${DTA_TEST_DIR}", {"DTA_TEST_DIR": str(directory)}, str(directory)),
            ("${DTA_TEST_URL}/", {"DTA_TEST_URL": base}, base),
        ]:
            source = Source(connector="file", location=location, options={"datasets": datasets})
            with open_connector("files", source, env) as connector:
                report = probe(connector)
                assert not report.ok
                failed = {d.dataset: d.error_code for d in report.datasets if not d.ok}
                assert failed == {"logs": "source.read_failed", "gone": "source.read_failed"}
                list(connector.read("first"))
                profile_source(connector, profiled_at=NOW, datasets=["first"])
                errors = [json.dumps(report.as_dict())]
                for failing in (_unknown_dataset, lambda c: list(c.read("logs"))):
                    with pytest.raises(SourceError) as caught:
                        failing(connector)
                    errors.append(json.dumps(caught.value.as_dict()))
            _assert_hidden(hidden, captured_logs, errors)

    def test_postgres_password_never_reaches_logs_or_errors(
        self, postgres: PostgresBackend, captured_logs: CapturedLogs
    ) -> None:
        password = postgres.env["DTA_CONNECT_PG_PASSWORD"]
        table = postgres.qualified("r")
        postgres.execute(
            f"CREATE TABLE {table} (id int primary key, ts timestamptz, v numeric(6, 2))",
            f"INSERT INTO {table} VALUES "
            "(1, '2024-01-01 00:00Z', 1.5), (2, '2024-01-01 00:01Z', 2)",
        )
        with postgres.open() as connector:
            errors = _exercise(connector, [_missing_column, _unknown_dataset])
        with pytest.raises(SourceError) as caught:
            wrong = {**postgres.env, "DTA_CONNECT_PG_DATABASE": "no_such_database"}
            open_connector("pg", postgres.source(), wrong)
        errors.append(json.dumps(caught.value.as_dict()))
        _assert_hidden(password, captured_logs, errors)


class TestKindsAndOptions:
    @pytest.mark.parametrize("kind", ["custom"])
    def test_kinds_that_are_not_available(self, kind: str) -> None:
        with pytest.raises(SourceConfigError, match="not available") as caught:
            open_connector("x", Source(connector=kind, location="https://example.invalid"), {})
        assert caught.value.detail["kind"] == kind

    def test_source_without_kind_or_location(self) -> None:
        with pytest.raises(SourceConfigError):
            open_connector("x", Source(location="sqlite://"), {})
        with pytest.raises(SourceConfigError):
            open_connector("x", Source(connector="sql"), {})

    @pytest.mark.parametrize(
        ("options", "option"),
        [
            ({"bogus": 1}, "bogus"),
            ({"timezone": "Mars/Olympus"}, "timezone"),
            ({"schema": ""}, "schema"),
            ({"connect_timeout_s": -1}, "connect_timeout_s"),
            ({"datasets": []}, "datasets"),
            ({"datasets": {}}, "datasets"),
            ({"datasets": {"a": "readings"}}, "datasets.a"),
            ({"datasets": {"a": {"table": "t", "query": "SELECT 1"}}}, "datasets.a"),
            ({"datasets": {"a": {}}}, "datasets.a"),
            ({"datasets": {"a": {"table": "t", "where": "x"}}}, "datasets.a.where"),
            ({"datasets": {"a": {"query": "UPDATE t SET x = 1"}}}, "datasets.a.query"),
        ],
    )
    def test_malformed_sql_options_name_the_option(
        self, sqlite: SqliteBackend, options: dict[str, Any], option: str
    ) -> None:
        with pytest.raises(SourceConfigError) as caught:
            open_connector("db", Source(connector="sql", location="sqlite://", options=options), {})
        assert caught.value.detail["option"] == option
        assert option in caught.value.message

    @pytest.mark.parametrize(
        ("options", "option"),
        [
            ({}, "datasets"),
            ({"datasets": {"a": {"format": "csv"}}}, "datasets.a.glob"),
            ({"datasets": {"a": {"glob": "*.csv"}}}, "datasets.a.format"),
            ({"datasets": {"a": {"glob": "*.xlsx", "format": "xlsx"}}}, "datasets.a.format"),
            ({"datasets": {"a": {"glob": "../up/*.csv", "format": "csv"}}}, "datasets.a.glob"),
            ({"datasets": {"a": {"glob": "/etc/*", "format": "csv"}}}, "datasets.a.glob"),
            (
                {"datasets": {"a": {"glob": "*.csv", "format": "csv", "types": {"c": "int"}}}},
                "datasets.a.types.c",
            ),
            (
                {"datasets": {"a": {"glob": "*.csv", "format": "csv", "timezone": "Nowhere"}}},
                "datasets.a.timezone",
            ),
            ({"datasets": {"a": {"glob": "*", "format": "csv", "sep": ";"}}}, "datasets.a.sep"),
        ],
    )
    def test_malformed_file_options_name_the_option(
        self, tmp_path: Path, options: dict[str, Any], option: str
    ) -> None:
        source = Source(connector="file", location=f"file://{tmp_path}", options=options)
        with pytest.raises(SourceConfigError) as caught:
            open_connector("files", source, {})
        assert caught.value.detail["option"] == option

    def test_arrow_type_names(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("a,b,c,d\n1,2.5,2024-01-01 00:00:00Z,x\n")
        types = {
            "a": "int32",
            "b": "decimal128(6, 2)",
            "c": "timestamp[ms, tz=UTC]",
            "d": "large_string",
        }
        datasets = {"a": {"glob": "a.csv", "format": "csv", "types": types}}
        source = Source(
            connector="file", location=f"file://{tmp_path}", options={"datasets": datasets}
        )
        with open_connector("files", source, {}) as connector:
            table = list(connector.read("a"))[0]
        assert [str(t) for t in table.schema.types] == [
            "int32",
            "double",
            "timestamp[us, tz=UTC]",
            "string",
        ]


class TestReadArguments:
    @pytest.fixture
    def connector(self, sqlite: SqliteBackend) -> Connector:
        sqlite.execute("CREATE TABLE r (id integer, ts timestamp)")
        return sqlite.open()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"since": 1},
            {"limit": -1},
            {"limit": True},
            {"columns": "id"},
            {"columns": []},
            {"cursor_column": 3},
        ],
    )
    def test_bad_arguments_are_refused_at_the_call(
        self, connector: Connector, kwargs: dict[str, Any]
    ) -> None:
        with connector, pytest.raises(SourceConfigError):
            connector.read("r", **kwargs)

    def test_unknown_dataset_is_refused_at_the_call(self, connector: Connector) -> None:
        with connector, pytest.raises(UnknownDataset):
            connector.read("missing")


class TestProbe:
    def test_failing_datasets_are_reported_and_the_rest_probed(self, sqlite: SqliteBackend) -> None:
        sqlite.execute(
            "CREATE TABLE r (id integer primary key, ts timestamp, amount numeric(6, 2))",
            "INSERT INTO r VALUES (1, '2024-01-01 00:00:00', 1.5)",
            "CREATE TABLE e (id integer)",
        )
        datasets = {
            "r": {"table": "r"},
            "e": {"table": "e"},
            "gone": {"table": "missing"},
            "bad": {"query": "SELECT nope FROM r"},
        }
        with sqlite.open(datasets=datasets) as connector:
            report = probe(connector)
        assert isinstance(report, ProbeReport) and not report.ok
        by_name = {d.dataset: d for d in report.datasets}
        assert list(by_name) == ["bad", "e", "gone", "r"]
        assert by_name["r"].ok and by_name["r"].has_rows
        assert [(c.name, c.type) for c in by_name["r"].columns] == [
            ("id", "int64"),
            ("ts", "timestamp[us, tz=UTC]"),
            ("amount", "double"),
        ]
        assert by_name["e"].ok and not by_name["e"].has_rows
        assert by_name["gone"].error_code == "source.invalid_config"
        assert by_name["bad"].error_code == "source.invalid_config"
        assert not by_name["bad"].columns
        as_dict = report.as_dict()
        assert as_dict["ok"] is False and len(as_dict["datasets"]) == 4
