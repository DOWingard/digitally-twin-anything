"""The SQL connector against real databases: every behaviour runs on a SQLite file
and on the test PostgreSQL server (skipped without its environment variables)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest
import sqlalchemy as sa
from dta_connect_testkit import SqlBackend
from dta_core.sources import (
    BATCH_ROWS,
    Connector,
    SourceConfigError,
    SourceError,
    UnknownDataset,
)

START = datetime(2024, 1, 1)
ROWS = 120


def _readings(backend: SqlBackend, name: str = "readings", rows: int = ROWS) -> sa.Table:
    table = sa.Table(
        name,
        backend.metadata(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sensor", sa.Text, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=False), nullable=False),
        sa.Column("value", sa.Float),
        sa.Column("amount", sa.Numeric(10, 2)),
    )
    backend.create(
        table,
        [
            {
                "id": i,
                "sensor": f"s{i % 3}",
                "ts": START + timedelta(minutes=i),
                "value": None if i % 10 == 0 else i * 0.5,
                "amount": Decimal(i) / 4,
            }
            for i in range(rows)
        ],
    )
    return table


def _read_all(connector: Connector, dataset: str, **kwargs: Any) -> pa.Table:
    tables = list(connector.read(dataset, **kwargs))
    assert tables, "read must yield at least one table"
    schemas = {table.schema for table in tables}
    assert len(schemas) == 1, f"one schema per dataset, got {schemas}"
    assert all(table.num_rows <= BATCH_ROWS for table in tables)
    return pa.concat_tables(tables)


@pytest.fixture
def statements(backend: SqlBackend) -> Iterator[list[str]]:
    """SQL text of every statement sent through any engine while the test runs."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    sa.event.listen(sa.Engine, "before_cursor_execute", record)
    yield seen
    sa.event.remove(sa.Engine, "before_cursor_execute", record)


def test_lists_every_table_of_the_schema(backend: SqlBackend) -> None:
    _readings(backend)
    _readings(backend, "other", rows=1)
    with backend.open() as connector:
        assert connector.datasets() == ["other", "readings"]


def test_reads_a_table_with_normalised_types(backend: SqlBackend) -> None:
    _readings(backend)
    with backend.open() as connector:
        table = _read_all(connector, "readings")
    assert table.num_rows == ROWS
    assert table.schema.names == ["id", "sensor", "ts", "value", "amount"]
    assert table.schema.field("ts").type == pa.timestamp("us", tz="UTC")
    assert table.schema.field("amount").type == pa.float64()
    assert table.schema.field("value").type == pa.float64()
    first = table.slice(1, 1).to_pylist()[0]
    assert first == {
        "id": 1,
        "sensor": "s1",
        "ts": datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        "value": 0.5,
        "amount": 0.25,
    }


def test_explicit_datasets_name_tables_and_queries(backend: SqlBackend) -> None:
    _readings(backend)
    _readings(backend, "other", rows=1)
    query = f"SELECT sensor, COUNT(*) AS n FROM {backend.qualified('readings')} GROUP BY sensor"
    datasets = {"r": {"table": "readings"}, "per_sensor": {"query": query}}
    with backend.open(datasets=datasets) as connector:
        assert connector.datasets() == ["per_sensor", "r"]
        assert _read_all(connector, "r").num_rows == ROWS
        counts = _read_all(connector, "per_sensor").sort_by("sensor").to_pylist()
        assert counts == [{"sensor": f"s{k}", "n": 40} for k in range(3)]
        with pytest.raises(UnknownDataset):
            connector.read("other")


def test_query_datasets_honour_columns_cursor_and_limit(backend: SqlBackend) -> None:
    _readings(backend)
    query = f"SELECT id, ts, value FROM {backend.qualified('readings')} WHERE id < 100"
    with backend.open(datasets={"q": {"query": query}}) as connector:
        table = _read_all(
            connector, "q", columns=["value", "id"], cursor_column="id", since=90, limit=5
        )
        assert table.schema.names == ["value", "id"]
        assert table.column("id").to_pylist() == [91, 92, 93, 94, 95]
        later = _read_all(connector, "q", cursor_column="ts", since=START + timedelta(minutes=97))
        assert later.column("id").to_pylist() == [98, 99]
        assert later.schema.field("ts").type == pa.timestamp("us", tz="UTC")


def test_column_selection_keeps_the_requested_order(backend: SqlBackend) -> None:
    _readings(backend)
    with backend.open() as connector:
        table = _read_all(connector, "readings", columns=["value", "id"])
        assert table.schema.names == ["value", "id"]
        with pytest.raises(SourceConfigError) as caught:
            connector.read("readings", columns=["id", "nope"]).__next__()
        assert caught.value.detail["missing_columns"] == ["nope"]


def test_limit_is_sent_to_the_database(backend: SqlBackend, statements: list[str]) -> None:
    _readings(backend)
    with backend.open() as connector:
        statements.clear()
        assert _read_all(connector, "readings", limit=7).num_rows == 7
        assert _read_all(connector, "readings", limit=0).num_rows == 0
    selects = [s for s in statements if "readings" in s and s.lstrip().upper().startswith("SELECT")]
    assert selects and all("LIMIT" in s.upper() for s in selects)


def test_since_is_filtered_in_the_database(backend: SqlBackend, statements: list[str]) -> None:
    """Rows up to the cursor hold values that cannot be converted, so had they been
    fetched the read would fail; the read succeeds and the statement carries the filter."""
    table = sa.Table(
        "guarded",
        backend.metadata(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=False)),
        sa.Column("amount", sa.Numeric(10, 2)),
        sa.Column("value", sa.Float),
    )
    backend.create(
        table,
        [
            {"id": i, "ts": START + timedelta(minutes=i), "amount": Decimal(i), "value": i * 1.0}
            for i in range(100)
        ],
    )
    poison = (
        "UPDATE {t} SET amount = 'NaN' WHERE id <= 60"
        if backend.name == "postgres"
        else "UPDATE {t} SET value = 'not a number' WHERE id <= 60"
    )
    backend.execute(poison.format(t=backend.qualified("guarded")))
    with backend.open() as connector:
        with pytest.raises(SourceError):
            list(connector.read("guarded"))
        statements.clear()
        by_id = _read_all(connector, "guarded", cursor_column="id", since=60)
        assert by_id.column("id").to_pylist() == list(range(61, 100))
        by_time = _read_all(
            connector, "guarded", cursor_column="ts", since=START + timedelta(minutes=60)
        )
        assert by_time.num_rows == 39
    filtered = [s for s in statements if "guarded" in s and "WHERE" in s.upper()]
    assert len(filtered) == 2
    assert all(">" in s for s in filtered)


def test_timestamps_without_zone_are_read_in_the_source_zone(backend: SqlBackend) -> None:
    _readings(backend)
    with backend.open(timezone="America/New_York") as connector:
        first = _read_all(connector, "readings", limit=1).column("ts")[0].as_py()
        assert first == datetime(2024, 1, 1, 5, tzinfo=UTC)
        # The cursor compares in the same zone: 05:59 UTC is 00:59 in New York.
        since = datetime(2024, 1, 1, 5, 59, tzinfo=UTC)
        later = _read_all(connector, "readings", cursor_column="ts", since=since)
        assert later.column("id").to_pylist()[0] == 60


def test_empty_table_yields_one_empty_table_with_its_schema(backend: SqlBackend) -> None:
    _readings(backend, rows=0)
    with backend.open() as connector:
        tables = list(connector.read("readings"))
        assert len(tables) == 1 and tables[0].num_rows == 0
        assert tables[0].schema.names == ["id", "sensor", "ts", "value", "amount"]
        assert tables[0].schema.field("ts").type == pa.timestamp("us", tz="UTC")
        assert tables[0].schema.field("amount").type == pa.float64()
        selected = list(connector.read("readings", columns=["value", "id"]))
        assert [t.schema.names for t in selected] == [["value", "id"]]


def test_large_tables_come_in_batches_of_one_schema(backend: SqlBackend) -> None:
    _readings(backend, rows=BATCH_ROWS + 25)
    with backend.open() as connector:
        sizes = [t.num_rows for t in connector.read("readings", columns=["id", "value"])]
    assert sizes == [BATCH_ROWS, 25]


def test_unknown_dataset_is_reported(backend: SqlBackend) -> None:
    _readings(backend)
    with backend.open() as connector:
        with pytest.raises(UnknownDataset) as caught:
            connector.read("nope")
        assert caught.value.code == "source.unknown_dataset"
        assert caught.value.detail["dataset"] == "nope"
        assert caught.value.detail["location"] == backend.location


def test_declared_table_that_does_not_exist_is_a_config_error(backend: SqlBackend) -> None:
    _readings(backend, rows=1)
    with backend.open(datasets={"gone": {"table": "missing_table"}}) as connector:
        with pytest.raises(SourceConfigError):
            list(connector.read("gone"))


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM readings",
        "SELECT 1; DROP TABLE readings",
        "WITH d AS (DELETE FROM readings RETURNING *) SELECT * FROM d",
        "SELECT * INTO copy FROM readings",
        "SELECT * FROM readings FOR UPDATE",
    ],
)
def test_queries_that_could_write_are_refused(backend: SqlBackend, query: str) -> None:
    with pytest.raises(SourceConfigError) as caught:
        backend.open(datasets={"q": {"query": query}})
    assert caught.value.detail["option"] == "datasets.q.query"


def test_closed_connector_refuses_reads(backend: SqlBackend) -> None:
    _readings(backend)
    connector = backend.open()
    connector.close()
    connector.close()
    with pytest.raises(SourceError):
        connector.read("readings")


def test_errors_carry_the_placeholder_location_only(backend: SqlBackend) -> None:
    _readings(backend)
    with backend.open() as connector:
        with pytest.raises(SourceError) as caught:
            list(connector.read("readings", columns=["nope"]))
    text = json.dumps(caught.value.as_dict())
    for value in backend.env.values():
        if len(value) > 6:
            assert value not in text
