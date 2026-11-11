"""A type's objects over a run of versions, as tables for analysis and learning.

The state at every version of a range is never read version by version. The
table at the first version (one read) and every change over the range (one
read) are taken as events per object; an as-of join in DuckDB then gives, for
each object and each version of the range, the last event at or before it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.schema import Kind, ObjectType, Ontology
from dta_core.store import Store, VersionRef

from .problems import ApiError
from .tables import version_json

log = Log("api.slice")

# Objects times versions of the state grid, at most.
MAX_SLICE_CELLS = 5_000_000
_TIME = pa.timestamp("us", tz="UTC")

_AS_OF = """
SELECT g.vi AS vi, e.row AS row
FROM (SELECT o.uid, a.vi, a.seq FROM objects o CROSS JOIN axis a) AS g
ASOF JOIN events AS e ON g.uid = e.uid AND g.seq >= e.seq
WHERE NOT e.removed
ORDER BY g.uid, g.seq
"""


def numeric_properties(object_type: ObjectType) -> list[str]:
    return [
        field.name
        for field in object_type.fields
        if field.kind in (Kind.integer, Kind.float) and not field.multivalued
    ]


def _connect() -> duckdb.DuckDBPyConnection:
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


def _uid_filter(uids: Sequence[str] | None) -> list[tuple[str, str, object]] | None:
    return [("uid", "in", list(uids))] if uids else None


def state_slice(
    store: Store,
    ontology: Ontology,
    versions: Sequence[VersionRef],
    type_name: str,
    properties: Sequence[str] | None,
    uids: Sequence[str] | None,
) -> pa.Table:
    """One row per object and version of `versions` at which the object exists:
    `_version`, `_seq`, `_t`, `uid`, then its properties as of that version."""
    started = time.perf_counter()
    first, last = versions[0], versions[-1]
    columns = None if properties is None else list(properties)
    initial = store.table(
        type_name, columns=columns, filter=_uid_filter(uids), at=first, ontology=ontology
    )
    names = [name for name in initial.column_names if name != "uid"]
    value_columns = ["uid", *names]
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
                pc.is_in(changes.column("uid"), value_set=pa.array(list(uids), pa.string()))
            )
    else:
        changes = None
    parts = [initial.select(value_columns)]
    seqs = [pa.array(np.full(initial.num_rows, first.seq, dtype=np.int64))]
    removed = [pa.array(np.zeros(initial.num_rows, dtype=bool))]
    if changes is not None and changes.num_rows:
        parts.append(changes.select(value_columns))
        seqs.append(changes.column("_seq").combine_chunks())
        removed.append(pc.equal(changes.column("_change"), "removed").combine_chunks())
    events = pa.concat_tables(parts, promote_options="permissive").combine_chunks()
    objects = pc.unique(events.column("uid")).sort()
    cells = len(objects) * len(versions)
    if cells > MAX_SLICE_CELLS:
        raise ApiError(
            f"the slice would hold {len(objects)} objects at {len(versions)} versions, more "
            f"than {MAX_SLICE_CELLS} cells; narrow it with uids, a shorter range or a step",
            code="api.slice_too_large",
        )
    keys = pa.table(
        {
            "row": pa.array(np.arange(events.num_rows, dtype=np.int64)),
            "uid": events.column("uid"),
            "seq": pa.concat_arrays(seqs),
            "removed": pa.concat_arrays(removed),
        }
    )
    axis = pa.table(
        {
            "vi": pa.array(np.arange(len(versions), dtype=np.int64)),
            "seq": pa.array([version.seq for version in versions], pa.int64()),
        }
    )
    connection = _connect()
    try:
        connection.register("events", keys)
        connection.register("axis", axis)
        connection.register("objects", pa.table({"uid": objects}))
        found = connection.sql(_AS_OF).to_arrow_table()
    finally:
        connection.close()
    at_version = found.column("vi").combine_chunks()
    values = events.take(found.column("row").combine_chunks())
    result = pa.table(
        {
            "_version": pa.array([version.id for version in versions], pa.string()).take(
                at_version
            ),
            "_seq": pa.array([version.seq for version in versions], pa.int64()).take(at_version),
            "_t": pa.array([version.t for version in versions], _TIME).take(at_version),
        }
    )
    for name in value_columns:
        result = result.append_column(values.schema.field(name), values.column(name))
    log.debug(
        "state slice computed",
        type=type_name,
        versions=len(versions),
        objects=len(objects),
        events=events.num_rows,
        rows=result.num_rows,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return result


def changes_slice(
    store: Store,
    ontology: Ontology,
    pinned: str,
    branch: str,
    type_name: str,
    properties: Sequence[str] | None,
    uids: Sequence[str] | None,
) -> pa.Table:
    """Every change of the type over the selected versions, as the store records it."""
    table = store.changes(
        type_name,
        pinned,
        branch=branch,
        columns=None if properties is None else list(properties),
        ontology=ontology,
    )
    if uids:
        table = table.filter(
            pc.is_in(table.column("uid"), value_set=pa.array(list(uids), pa.string()))
        )
    return table


def tensor(long: pa.Table, versions: Sequence[VersionRef], properties: Sequence[str]) -> pa.Table:
    """Coordinate rows `(uid_index, property_index, version_index, value)` of the non-null
    numeric values of a long slice. The three index tables are in the schema metadata as
    JSON: `dta:uids`, `dta:properties`, `dta:versions`."""
    objects = pc.unique(long.column("uid")).sort()
    seqs = pa.array([version.seq for version in versions], pa.int64())
    uid_index = pc.index_in(long.column("uid"), value_set=objects).cast(pa.int32())
    version_index = pc.index_in(long.column("_seq"), value_set=seqs).cast(pa.int32())
    parts = []
    for position, name in enumerate(properties):
        values = long.column(name)
        present = pc.and_(pc.is_valid(values), pc.is_valid(version_index))
        count = pc.sum(pc.cast(present, pa.int64())).as_py() or 0
        parts.append(
            pa.table(
                {
                    "uid_index": uid_index.filter(present),
                    "property_index": pa.array(np.full(count, position, dtype=np.int32)),
                    "version_index": version_index.filter(present),
                    "value": values.filter(present).cast(pa.float64()),
                }
            )
        )
    schema = pa.schema(
        [
            pa.field("uid_index", pa.int32()),
            pa.field("property_index", pa.int32()),
            pa.field("version_index", pa.int32()),
            pa.field("value", pa.float64()),
        ]
    )
    table = (
        pa.concat_tables([part.cast(schema) for part in parts]) if parts else schema.empty_table()
    )
    if table.num_rows:
        order = pc.sort_indices(
            table,
            sort_keys=[
                ("uid_index", "ascending"),
                ("version_index", "ascending"),
                ("property_index", "ascending"),
            ],
        )
        table = table.take(order)
    return table.replace_schema_metadata(index_metadata(objects.to_pylist(), properties, versions))


def index_tables(
    uids: Sequence[str], properties: Sequence[str], versions: Sequence[VersionRef]
) -> dict[str, list]:
    return {
        "uids": list(uids),
        "properties": list(properties),
        "versions": [version_json(version) for version in versions],
    }


def index_metadata(
    uids: Sequence[str], properties: Sequence[str], versions: Sequence[VersionRef]
) -> dict[bytes, bytes]:
    tables = index_tables(uids, properties, versions)
    return {f"dta:{name}".encode(): json.dumps(value).encode() for name, value in tables.items()}


def tensor_indexes(table: pa.Table) -> dict[str, list]:
    """The index tables of a tensor slice, read back from its schema metadata."""
    metadata = table.schema.metadata or {}
    return {
        name: json.loads(metadata[f"dta:{name}".encode()])
        for name in ("uids", "properties", "versions")
    }


__all__ = [
    "MAX_SLICE_CELLS",
    "changes_slice",
    "numeric_properties",
    "state_slice",
    "tensor",
    "tensor_indexes",
]
