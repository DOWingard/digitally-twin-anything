"""Behaviour that belongs to one database: zoned timestamps, credentials,
reachability and the database's own refusal to write."""

from __future__ import annotations

import json
import secrets
import socket
from datetime import UTC, datetime

import pyarrow as pa
import pytest
import sqlalchemy as sa
from dta_connect_testkit import PG_LOCATION, PostgresBackend, SqliteBackend
from dta_core.connect import open_connector
from dta_core.manifest.models import Source
from dta_core.sources import BATCH_ROWS, SourceAuthFailed, SourceUnavailable


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestPostgres:
    def test_zoned_and_zone_less_timestamps(self, postgres: PostgresBackend) -> None:
        table = postgres.qualified("times")
        postgres.execute(
            f"CREATE TABLE {table} (id int PRIMARY KEY, local timestamp, zoned timestamptz)",
            f"INSERT INTO {table} VALUES "
            "(1, '2024-07-01 12:00', '2024-07-01 12:00+02'), "
            "(2, '2024-01-01 12:00', '2024-01-01 12:00-05')",
        )
        with postgres.open(timezone="Europe/Berlin") as connector:
            rows = list(connector.read("times"))[0]
            assert rows.schema.field("local").type == pa.timestamp("us", tz="UTC")
            assert rows.schema.field("zoned").type == pa.timestamp("us", tz="UTC")
            assert rows.sort_by("id").to_pylist() == [
                {
                    "id": 1,
                    "local": datetime(2024, 7, 1, 10, tzinfo=UTC),
                    "zoned": datetime(2024, 7, 1, 10, tzinfo=UTC),
                },
                {
                    "id": 2,
                    "local": datetime(2024, 1, 1, 11, tzinfo=UTC),
                    "zoned": datetime(2024, 1, 1, 17, tzinfo=UTC),
                },
            ]
            since = datetime(2024, 1, 1, 12, tzinfo=UTC)
            by_zoned = list(connector.read("times", cursor_column="zoned", since=since))[0]
            assert sorted(by_zoned.column("id").to_pylist()) == [1, 2]
            later = datetime(2024, 1, 1, 17, tzinfo=UTC)
            assert list(connector.read("times", cursor_column="zoned", since=later))[0].column(
                "id"
            ).to_pylist() == [1]
            assert list(connector.read("times", cursor_column="local", since=later))[0].column(
                "id"
            ).to_pylist() == [1]

    def test_numeric_and_decimal_columns_become_floats(self, postgres: PostgresBackend) -> None:
        table = postgres.qualified("money")
        postgres.execute(
            f"CREATE TABLE {table} (a numeric(12, 3), b numeric, c real, d double precision, "
            "e bigint, f smallint)",
            f"INSERT INTO {table} VALUES (1.125, 2.5, 0.5, 0.25, 9007199254740993, 7)",
        )
        with postgres.open() as connector:
            rows = list(connector.read("money"))[0]
        assert rows.schema.types == [pa.float64()] * 4 + [pa.int64(), pa.int64()]
        assert rows.to_pylist() == [
            {"a": 1.125, "b": 2.5, "c": 0.5, "d": 0.25, "e": 9007199254740993, "f": 7}
        ]

    def test_default_schema_is_listed_without_a_schema_option(
        self, postgres: PostgresBackend
    ) -> None:
        name = f"listed_{secrets.token_hex(4)}"
        postgres.execute(f'CREATE TABLE public."{name}" (id int)')
        try:
            source = Source(connector="sql", location=PG_LOCATION)
            with open_connector("pg", source, postgres.env) as connector:
                assert name in connector.datasets()
                assert list(connector.read(name))[0].num_rows == 0
        finally:
            postgres.execute(f'DROP TABLE public."{name}"')

    def test_url_without_a_driver_uses_the_installed_one(self, postgres: PostgresBackend) -> None:
        location = PG_LOCATION.replace("postgresql+psycopg2://", "postgresql://")
        source = Source(connector="sql", location=location, options={"schema": postgres.schema})
        with open_connector("pg", source, postgres.env) as connector:
            assert connector.datasets() == []

    def test_wrong_password_is_an_auth_failure_without_the_password(
        self, postgres: PostgresBackend, captured_logs
    ) -> None:
        wrong = "wrong-" + secrets.token_hex(12)
        env = {**postgres.env, "DTA_CONNECT_PG_PASSWORD": wrong}
        with pytest.raises(SourceAuthFailed) as caught:
            open_connector("pg", postgres.source(), env)
        error = caught.value
        assert error.code == "source.auth_failed"
        assert wrong not in error.message
        assert wrong not in json.dumps(error.detail)
        assert wrong not in str(error) and error.__cause__ is None
        assert error.detail["location"] == PG_LOCATION
        assert wrong not in captured_logs.text()
        assert any(r.levelname == "ERROR" for r in captured_logs.records)

    def test_unreachable_server_is_unavailable(self, postgres: PostgresBackend) -> None:
        env = {**postgres.env, "DTA_CONNECT_PG_PORT": str(_free_port())}
        with pytest.raises(SourceUnavailable) as caught:
            open_connector("pg", postgres.source(connect_timeout_s=5), env)
        assert caught.value.code == "source.unavailable"
        assert postgres.env["DTA_CONNECT_PG_PASSWORD"] not in json.dumps(caught.value.as_dict())

    def test_postgres_refuses_a_write_through_the_connectors_connection(
        self, postgres: PostgresBackend
    ) -> None:
        table = postgres.qualified("target")
        postgres.execute(f"CREATE TABLE {table} (id int)")
        with postgres.open() as connector:
            with connector.engine.connect() as connection:
                with pytest.raises(sa.exc.DBAPIError) as caught:
                    connection.execute(sa.text(f"INSERT INTO {table} VALUES (1)"))
                assert caught.value.orig.pgcode == "25006"  # read_only_sql_transaction
                connection.rollback()
                with pytest.raises(sa.exc.DBAPIError):
                    connection.execute(sa.text(f"CREATE TABLE {postgres.qualified('new')} (a int)"))
        with postgres.admin.connect() as connection:
            assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

    def test_a_read_stopped_early_leaves_no_open_transaction(
        self, postgres: PostgresBackend
    ) -> None:
        table = postgres.qualified("big")
        postgres.execute(
            f"CREATE TABLE {table} AS SELECT g AS id FROM generate_series(1, {BATCH_ROWS * 3}) g"
        )
        with postgres.open() as connector:
            batches = connector.read("big")
            first = next(batches)
            assert first.num_rows == BATCH_ROWS
            batches.close()
            assert "idle in transaction" not in postgres.open_sessions()


class TestSqlite:
    def test_sqlite_file_is_opened_read_only(self, sqlite: SqliteBackend) -> None:
        sqlite.execute("CREATE TABLE target (id integer)")
        with sqlite.open() as connector:
            with connector.engine.connect() as connection:
                with pytest.raises(sa.exc.OperationalError, match="readonly"):
                    connection.execute(sa.text("INSERT INTO target VALUES (1)"))

    def test_missing_database_file_is_unavailable_and_not_created(
        self, sqlite: SqliteBackend
    ) -> None:
        source = Source(connector="sql", location="sqlite:///${DTA_TEST_SQLITE_DIR}/absent.db")
        with pytest.raises(SourceUnavailable):
            open_connector("lite", source, sqlite.env)
        assert not (sqlite.directory / "absent.db").exists()
