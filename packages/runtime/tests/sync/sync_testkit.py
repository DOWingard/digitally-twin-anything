"""Stand-ins for data sources and small builders shared by the sync tests.

The store is always the real one. Only the sources are replaced: a connector
here serves Arrow tables held in memory through the same protocol a real
connector implements.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.manifest.models import Source
from dta_core.sources import BATCH_ROWS, Connector, SourceUnavailable, UnknownDataset
from dta_core.store import Store

T0 = datetime(2026, 1, 5, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
TIME = pa.timestamp("us", tz="UTC")
# The trace every test run is made under, so that the names of runs repeat.
TRACE = "0123456789abcdef0123456789abcdef"


class MemoryConnector:
    """Serves tables held in memory; records every read it is asked for."""

    def __init__(
        self,
        source_id: str,
        tables: Mapping[str, pa.Table],
        *,
        fail: Exception | None = None,
    ) -> None:
        self.source_id = source_id
        self.tables = dict(tables)
        self.fail = fail
        self.reads: list[dict[str, Any]] = []
        self.closed = 0

    def datasets(self) -> list[str]:
        return sorted(self.tables)

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        self.reads.append(
            {"dataset": dataset, "since": since, "cursor_column": cursor_column, "limit": limit}
        )
        if self.fail is not None:
            raise self.fail
        if dataset not in self.tables:
            raise UnknownDataset(f"{self.source_id} has no dataset {dataset}")
        table = self.tables[dataset]
        if cursor_column is not None and since is not None:
            newer = pc.greater(table[cursor_column], pa.scalar(since, table[cursor_column].type))
            table = table.filter(newer)
        if columns is not None:
            table = table.select(list(columns))
        if limit is not None:
            table = table.slice(0, limit)
        if table.num_rows == 0:
            yield table
            return
        for start in range(0, table.num_rows, BATCH_ROWS):
            yield table.slice(start, BATCH_ROWS)

    def close(self) -> None:
        self.closed += 1

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Sources:
    """The connectors of a test, handed to `load` as its `connect`."""

    def __init__(self, **tables: Mapping[str, pa.Table]) -> None:
        self.connectors = {name: MemoryConnector(name, found) for name, found in tables.items()}
        self.opened: list[str] = []

    def __call__(self, source_id: str, source: Source, env: Mapping[str, str]) -> Connector:
        self.opened.append(source_id)
        return self.connectors[source_id]

    def __getitem__(self, source_id: str) -> MemoryConnector:
        return self.connectors[source_id]

    def put(self, source_id: str, dataset: str, table: pa.Table) -> None:
        self.connectors[source_id].tables[dataset] = table

    def break_(self, source_id: str, message: str = "connection refused") -> None:
        self.connectors[source_id].fail = SourceUnavailable(message)

    def mend(self, source_id: str) -> None:
        self.connectors[source_id].fail = None


def table(rows: Sequence[Mapping[str, Any]], **types: pa.DataType) -> pa.Table:
    """An Arrow table from rows; `types` fixes the type of a column Arrow would guess wrong."""
    names: dict[str, None] = {}
    for row in rows:
        names.update(dict.fromkeys(row))
    columns = {}
    for name in names:
        values = [row.get(name) for row in rows]
        columns[name] = pa.array(values, types.get(name)) if name in types else pa.array(values)
    return pa.table(columns)


def source(mappings: Mapping[str, Any], views: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One source of a manifest, as the manifest writes it."""
    document: dict[str, Any] = {
        "connector": "custom",
        "location": "memory://test",
        "mappings": dict(mappings),
    }
    if views:
        document["views"] = dict(views)
    return document


def columns(*names: str, **renamed: str) -> dict[str, dict[str, str]]:
    """Property mappings: each name read from the column of the same name, or as renamed."""
    found = {name: {"column": name} for name in names}
    found.update({name: {"column": column} for name, column in renamed.items()})
    return found


def objects(store: Store, type_name: str, at: Any = "head") -> dict[str, dict[str, Any]]:
    """Stored objects of a type by uid, without the uid in each row."""
    rows = store.table(type_name, at=at).to_pylist()
    return {row.pop("uid"): row for row in rows}


def sync_versions(store: Store) -> list[Any]:
    return store.versions(cause="sync")


def changes_of(store: Store, uid: str, prop: str) -> list[tuple[datetime, Any]]:
    """(version time, value) of every version in which a property of an object changed."""
    history = store.history(uid, properties=[prop])
    return list(
        zip(history.column("_t").to_pylist(), history.column(prop).to_pylist(), strict=True)
    )
