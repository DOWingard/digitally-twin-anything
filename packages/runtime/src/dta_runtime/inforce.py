"""What was in force when: a type's objects over a run of versions.

The state at every version of a range is never read version by version. The
table at the first version (one read) and every change after it up to the
last (one read) are taken as events per object. An as-of join in DuckDB then
gives, for each object and each version or time asked for, the last event at
or before it. The cost is in the objects and the changes of the range, not in
objects times versions.

This is the one place the twin works this out: the API's slices, the
functions that span versions and the frames models learn from all read
through it, so that "the value in force" means the same thing in each.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import Store, VersionRef

log = Log("runtime.inforce")

TIME = pa.timestamp("us", tz="UTC")
# Columns of an events table that are not properties.
SEQ = "_seq"
T = "_t"
REMOVED = "_removed"
UID = "uid"
_KEYS = (UID, SEQ, T, REMOVED)

_AT_VERSIONS = """
SELECT g.i AS i, e.row AS row
FROM (SELECT o.uid, a.i, a.seq FROM objects o CROSS JOIN axis a) AS g
ASOF JOIN events AS e ON g.uid = e.uid AND g.seq >= e.seq
WHERE NOT e.removed
ORDER BY g.uid, g.seq
"""

# Of several versions with the same time, the latest is the one in force at that time.
_AT_TIMES = """
WITH latest AS (
    SELECT uid, t, row, removed FROM events
    QUALIFY row_number() OVER (PARTITION BY uid, t ORDER BY seq DESC) = 1
)
SELECT g.i AS i, e.row AS row
FROM (SELECT o.uid, a.i, a.t FROM objects o CROSS JOIN axis a) AS g
ASOF JOIN latest AS e ON g.uid = e.uid AND g.t >= e.t
WHERE NOT e.removed
ORDER BY g.uid, g.t
"""


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB that can read nothing but the tables registered with it."""
    connection = duckdb.connect(
        ":memory:",
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "threads": 2,
        },
    )
    # Session time zone of the host otherwise, which would change any time read back.
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET lock_configuration = true")
    return connection


def events(
    store: Store,
    ontology: Ontology,
    type_name: str,
    first: VersionRef,
    last: VersionRef,
    *,
    properties: Sequence[str] | None = None,
    uids: Sequence[str] | None = None,
) -> pa.Table:
    """The objects of a type at `first`, then every change after it up to `last`.

    One row per event: `uid`, `_seq` and `_t` (the version it happened at; for the
    objects at `first`, that version), `_removed`, then the properties as they were
    after it (nulls when removed). Two reads of the store, whatever the range.
    """
    started = time.perf_counter()
    columns = None if properties is None else list(properties)
    initial = store.table(
        type_name,
        columns=columns,
        filter=[(UID, "in", list(uids))] if uids else None,
        at=first,
        ontology=ontology,
    )
    names = [name for name in initial.column_names if name != UID]
    value_columns = [UID, *names]
    parts = [initial.select(value_columns)]
    seqs = [pa.array(np.full(initial.num_rows, first.seq, dtype=np.int64))]
    times = [pa.array([first.t] * initial.num_rows, TIME)]
    removed = [pa.array(np.zeros(initial.num_rows, dtype=bool))]
    if last.seq > first.seq:
        changes = store.changes(
            type_name,
            f"{first.id}..{last.id}",
            branch=first.branch,
            columns=columns,
            ontology=ontology,
        )
        changes = changes.filter(pc.greater(changes.column("_seq"), first.seq))
        if uids:
            changes = changes.filter(
                pc.is_in(changes.column(UID), value_set=pa.array(list(uids), pa.string()))
            )
        if changes.num_rows:
            parts.append(changes.select(value_columns))
            seqs.append(changes.column("_seq").combine_chunks())
            times.append(changes.column("_t").combine_chunks().cast(TIME))
            removed.append(pc.equal(changes.column("_change"), "removed").combine_chunks())
    values = pa.concat_tables(parts, promote_options="permissive").combine_chunks()
    table = pa.table(
        {
            UID: values.column(UID),
            SEQ: pa.concat_arrays(seqs),
            T: pa.concat_arrays(times),
            REMOVED: pa.concat_arrays(removed),
        }
    )
    for name in names:
        table = table.append_column(values.schema.field(name), values.column(name))
    log.debug(
        "events read",
        type=type_name,
        first=first.seq,
        last=last.seq,
        events=table.num_rows,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return table


def objects(events_table: pa.Table) -> pa.Array:
    """The uids that have an event, in order."""
    return pc.unique(events_table.column(UID)).sort()


def properties(events_table: pa.Table) -> list[str]:
    return [name for name in events_table.column_names if name not in _KEYS]


def _as_of(events_table: pa.Table, axis: pa.Table, query: str) -> tuple[pa.Array, pa.Table]:
    """For each object and point of the axis at which the object exists: the index of
    the point, and the object's row as of it (`uid` and the properties)."""
    keys = pa.table(
        {
            "row": pa.array(np.arange(events_table.num_rows, dtype=np.int64)),
            "uid": events_table.column(UID),
            "seq": events_table.column(SEQ),
            "t": events_table.column(T),
            "removed": events_table.column(REMOVED),
        }
    )
    connection = connect()
    try:
        connection.register("events", keys)
        connection.register("axis", axis)
        connection.register("objects", pa.table({"uid": objects(events_table)}))
        found = connection.sql(query).to_arrow_table()
    finally:
        connection.close()
    values = events_table.select([UID, *properties(events_table)])
    return found.column("i").combine_chunks(), values.take(found.column("row").combine_chunks())


def at_versions(events_table: pa.Table, versions: Sequence[VersionRef]) -> pa.Table:
    """One row per object and version of `versions` at which the object exists:
    `_version`, `_seq`, `_t`, `uid`, then its properties as of that version.
    Sorted by `uid`, then version."""
    axis = pa.table(
        {
            "i": pa.array(np.arange(len(versions), dtype=np.int64)),
            "seq": pa.array([version.seq for version in versions], pa.int64()),
        }
    )
    index, values = _as_of(events_table, axis, _AT_VERSIONS)
    result = pa.table(
        {
            "_version": pa.array([version.id for version in versions], pa.string()).take(index),
            "_seq": pa.array([version.seq for version in versions], pa.int64()).take(index),
            "_t": pa.array([version.t for version in versions], TIME).take(index),
        }
    )
    for name in values.column_names:
        result = result.append_column(values.schema.field(name), values.column(name))
    return result


def at_times(events_table: pa.Table, times: Sequence[datetime] | pa.Array) -> pa.Table:
    """One row per object and time of `times` at which the object exists: `_t` (the time
    asked for), `uid`, then its properties as stored at the last version whose time is at
    or before it. Sorted by `uid`, then time.

    A time before the first event of an object gives no row for it; the events say
    nothing of what came before them.
    """
    asked = times if isinstance(times, pa.Array) else pa.array(list(times), TIME)
    asked = asked.cast(TIME)
    axis = pa.table({"i": pa.array(np.arange(len(asked), dtype=np.int64)), "t": asked})
    index, values = _as_of(events_table, axis, _AT_TIMES)
    result = pa.table({"_t": asked.take(index)})
    for name in values.column_names:
        result = result.append_column(values.schema.field(name), values.column(name))
    return result


__all__ = [
    "REMOVED",
    "SEQ",
    "TIME",
    "UID",
    "T",
    "at_times",
    "at_versions",
    "connect",
    "events",
    "objects",
    "properties",
]
