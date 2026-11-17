"""Frames: a target and its features on a regular grid of times, as each stood at its time.

The rule that makes a frame safe to learn from: a cell for grid time g holds
the value as stored at the last version whose time is at or before g, among
the versions of the range. A lagged cell is read the same way at g less the
lag. Nothing stored later is ever read, so a model trained on a frame never
sees what was not yet known at a row's time.

Each type involved is read once (`inforce.events`: its state at the first
version and its changes up to the last); what was in force at each grid time
is `inforce.at_times`; lags, neighbours and labels are joins in DuckDB on the
position of a time in the grid.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import Store, VersionRef
from dta_core.store.model import iso_utc

from .. import inforce
from ..protocol.families import DS, UNIQUE_ID, Y
from ..protocol.worker import FrameRef, TrainingSetRequest
from ..sync.records import RUN_TYPE
from ..sync.report import BACKFILL
from .spec import LABEL, LABEL_DS, TOO_LARGE, Column, Neighbours, Spec, TrainingError, bad_spec
from .spec import resolve as resolve_spec

log = Log("runtime.training")

# Series times grid points times columns of a frame, at most.
MAX_CELLS = 5_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_KEYS = [inforce.UID, inforce.SEQ, inforce.T, inforce.REMOVED]
_AGGREGATES = ("mean", "sum", "min", "max", "count")
_NARROW = "narrow it with uids, a later start, a longer step or fewer features"

# The neighbours of an object at a grid time and their values then, as one row per object
# and time. A neighbour joined by several links, or from both ends, counts once. Sums are
# taken in the order of the neighbours' uids: the order a hash join yields them in changes
# from run to run, and with it the last bits of a sum of floats.
_NEIGHBOURS = """
SELECT p.o AS uid, p.i AS i,
       avg(v.v ORDER BY p.n) AS "mean", sum(v.v ORDER BY p.n) AS "sum",
       min(v.v) AS "min", max(v.v) AS "max", count(v.v) AS "count"
FROM (SELECT DISTINCT o, n, i FROM pairs WHERE o IN (SELECT uid FROM series)) AS p
JOIN vals AS v ON v.uid = p.n AND v.i = p.i
GROUP BY p.o, p.i
"""
_NO_NEIGHBOURS = pa.schema(
    [
        pa.field("uid", pa.string()),
        pa.field("i", pa.int64()),
        *(pa.field(name, pa.float64()) for name in _AGGREGATES[:-1]),
        pa.field("count", pa.int64()),
    ]
)


@dataclass(frozen=True)
class TrainingSet:
    table: pa.Table
    # JSON values: what was asked as resolved, and what the reader has to know of the rows.
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _Grid:
    """The times of a frame: `base` of them from the first version's time up to the last
    version's, a step apart, then `ahead` more. A time is known by its position."""

    t0_us: int
    step_us: int
    base: int
    ahead: int

    @property
    def points(self) -> int:
        return self.base + self.ahead

    def times(self, count: int) -> pa.Array:
        values = self.t0_us + self.step_us * np.arange(count, dtype=np.int64)
        return pa.array(values, pa.int64()).cast(inforce.TIME)

    def at(self, position: int) -> datetime:
        return _EPOCH + (self.t0_us + position * self.step_us) * _MICROSECOND

    def positions(self, times: pa.ChunkedArray | pa.Array) -> pa.Array:
        elapsed = pc.subtract(times.cast(pa.int64()), self.t0_us)
        return pc.divide(elapsed, self.step_us)


@dataclass(frozen=True)
class _Built:
    table: pa.Table
    spec: Spec
    first: VersionRef
    last: VersionRef
    grid: _Grid
    series: int


class _Reads:
    """The events of each type a frame involves, read once whatever uses them."""

    def __init__(
        self,
        store: Store,
        ontology: Ontology,
        first: VersionRef,
        last: VersionRef,
        needs: dict[str, list[str]],
    ) -> None:
        self._store = store
        self._ontology = ontology
        self._first = first
        self._last = last
        self._needs = needs
        self._found: dict[str, pa.Table] = {}

    def of(self, type_name: str, only: Sequence[str] | None = None) -> pa.Table:
        """Every event of the type over the range. `only` asks the store for these objects
        alone; it is honoured when given at the first read of the type."""
        if type_name not in self._found:
            self._found[type_name] = inforce.events(
                self._store,
                self._ontology,
                type_name,
                self._first,
                self._last,
                properties=self._needs[type_name],
                uids=only,
            )
        return self._found[type_name]

    @property
    def count(self) -> int:
        return len(self._found)


def _us(moment: datetime) -> int:
    return (moment - _EPOCH) // _MICROSECOND


def _too_large(message: str, **detail: Any) -> TrainingError:
    return TrainingError(f"{message}; {_NARROW}", code=TOO_LARGE, detail=detail)


def _of_objects(events: pa.Table, uids: pa.Array) -> pa.Table:
    return events.filter(pc.is_in(events.column(inforce.UID), value_set=uids))


def _needs(spec: Spec) -> dict[str, list[str]]:
    """Per type involved, the properties to read."""
    needs: dict[str, list[str]] = {spec.type.name: spec.own_properties()}

    def add(type_name: str, names: Sequence[str]) -> None:
        known = needs.setdefault(type_name, [])
        known.extend(name for name in names if name not in known)

    for column in spec.features:
        if column.neighbours is None:
            continue
        for hop in column.neighbours.hops:
            add(hop.link_type, [hop.row_field, hop.neighbour_field])
        for neighbour in column.neighbours.types:
            add(neighbour, [column.neighbours.property])
    return needs


def _neighbour_values(
    connection: duckdb.DuckDBPyConnection,
    reads: _Reads,
    neighbours: Neighbours,
    series: pa.Array,
    grid: _Grid,
) -> pa.Table:
    """Per series object and grid time: the aggregates of the property over the objects
    that links in force at that time join to it, each neighbour read as of that time."""
    times = grid.times(grid.base)
    pairs = []
    for link_type in dict.fromkeys(hop.link_type for hop in neighbours.hops):
        hops = [hop for hop in neighbours.hops if hop.link_type == link_type]
        events = reads.of(link_type)
        # Only the links that ever touch a series object: the others can add nothing,
        # and a network has far more links than a frame has series.
        touching = None
        for name in dict.fromkeys(hop.row_field for hop in hops):
            held = pc.fill_null(pc.is_in(events.column(name), value_set=series), False)
            touching = held if touching is None else pc.or_(touching, held)
        touched = pc.unique(events.column(inforce.UID).filter(touching))
        if len(touched) * grid.base > MAX_CELLS:
            raise _too_large(
                f"the neighbour feature would follow {len(touched)} links of {link_type} at "
                f"{grid.base} grid times, more than {MAX_CELLS} cells",
                links=len(touched),
                points=grid.base,
            )
        if not len(touched):
            continue
        ends = list(
            dict.fromkeys(name for hop in hops for name in (hop.row_field, hop.neighbour_field))
        )
        state = inforce.at_times(_of_objects(events, touched).select([*_KEYS, *ends]), times)
        positions = grid.positions(state.column("_t"))
        pairs.extend(
            pa.table(
                {
                    "o": state.column(hop.row_field),
                    "n": state.column(hop.neighbour_field),
                    "i": positions,
                }
            )
            for hop in hops
        )
    if not pairs:
        return _NO_NEIGHBOURS.empty_table()
    joined = pa.concat_tables(pairs)
    reached = pc.unique(joined.column("n").filter(pc.is_in(joined.column("o"), value_set=series)))
    values = []
    for type_name in neighbours.types:
        events = _of_objects(reads.of(type_name), reached)
        if not events.num_rows:
            continue
        state = inforce.at_times(events.select([*_KEYS, neighbours.property]), times)
        values.append(
            pa.table(
                {
                    "uid": state.column(inforce.UID),
                    "i": grid.positions(state.column("_t")),
                    "v": state.column(neighbours.property).cast(pa.float64()),
                }
            )
        )
    if not values:
        return _NO_NEIGHBOURS.empty_table()
    connection.register("pairs", joined)
    connection.register("vals", pa.concat_tables(values))
    try:
        return connection.sql(_NEIGHBOURS).to_arrow_table()
    finally:
        connection.unregister("pairs")
        connection.unregister("vals")


def _query(spec: Spec, own: Sequence[str], groups: dict[Neighbours, str], last: int) -> str:
    """The frame as one statement over `rows` (an object's state at each grid position it
    exists at) and the neighbour tables. `last` is the position of the last time at which
    anything is known: a cell read at a later position, or before the first, is null."""
    position = {name: index for index, name in enumerate(own)}
    selects = [
        "r.uid AS c0",
        "r.t AS c1",
        f"CASE WHEN r.i <= {last} THEN CAST(r.p0 AS DOUBLE) END AS c2",
    ]
    joins: dict[str, str] = {}

    def own_at(lag: int) -> str:
        alias = f"s{'m' if lag < 0 else ''}{abs(lag)}"
        joins[alias] = (
            f"LEFT JOIN rows AS {alias} ON {alias}.uid = r.uid "
            f"AND {alias}.i = r.i - ({lag}) AND {alias}.i <= {last}"
        )
        return alias

    columns = list(spec.features)
    if spec.label is not None:
        columns.append(Column(LABEL, spec.target, -spec.label))
    for index, column in enumerate(columns, start=3):
        if column.neighbours is None:
            source = f"p{position[column.property]}"
            if column.lag == 0:
                value = f"CASE WHEN r.i <= {last} THEN CAST(r.{source} AS DOUBLE) END"
            else:
                value = f"CAST({own_at(column.lag)}.{source} AS DOUBLE)"
        else:
            alias = f"{groups[column.neighbours]}_{column.lag}"
            joins[alias] = (
                f"LEFT JOIN {groups[column.neighbours]} AS {alias} ON {alias}.uid = r.uid "
                f"AND {alias}.i = r.i - {column.lag}"
            )
            if column.agg == "count":
                # No neighbour at a time that is known is a count of zero, not an unknown.
                value = (
                    f"CASE WHEN r.i - {column.lag} BETWEEN 0 AND {last} "
                    f'THEN CAST(coalesce({alias}."count", 0) AS DOUBLE) END'
                )
            else:
                value = f'CAST({alias}."{column.agg}" AS DOUBLE)'
        selects.append(f"{value} AS c{index}")
    where = f"WHERE r.i + {spec.label} <= {last}" if spec.label is not None else ""
    return (
        f"SELECT {', '.join(selects)} FROM rows AS r {' '.join(joins.values())} {where} "
        "ORDER BY r.uid, r.i"
    )


def _build(
    store: Store, ontology: Ontology, ref: FrameRef, horizon: timedelta | None = None
) -> _Built:
    started = time.perf_counter()
    spec = resolve_spec(ontology, ref, horizon)
    first = store.resolve(ref.start, ref.branch)[0]
    last = store.resolve(ref.end, ref.branch)[0]
    if first.seq > last.seq:
        raise bad_spec("start", f"the start (version #{first.seq}) is after the end (#{last.seq})")
    step_us = ref.step // _MICROSECOND
    t0_us = _us(first.t)
    grid = _Grid(t0_us, step_us, (_us(last.t) - t0_us) // step_us + 1, ref.ahead)
    names = spec.names
    if grid.points * len(names) > MAX_CELLS:
        raise _too_large(
            f"the frame would hold {grid.points} grid times of {len(names)} columns for each "
            f"series, more than {MAX_CELLS} cells",
            points=grid.points,
            columns=len(names),
        )
    needs = _needs(spec)
    reads = _Reads(store, ontology, first, last, needs)
    target_type = spec.type.name
    # A type that is also read for its neighbours is read whole, once, and narrowed here.
    shared = any(
        target_type in column.neighbours.types
        or target_type in {h.link_type for h in column.neighbours.hops}
        for column in spec.features
        if column.neighbours is not None
    )
    events = reads.of(target_type, None if shared else ref.uids)
    if ref.uids and shared:
        events = _of_objects(events, pa.array(list(ref.uids), pa.string()))
    series = inforce.objects(events)
    if len(series) * grid.points * len(names) > MAX_CELLS:
        raise _too_large(
            f"the frame would hold {len(series)} series at {grid.points} grid times in "
            f"{len(names)} columns, more than {MAX_CELLS} cells",
            series=len(series),
            points=grid.points,
            columns=len(names),
        )
    own = spec.own_properties()
    state = inforce.at_times(events.select([*_KEYS, *own]), grid.times(grid.points))
    rows = pa.table(
        {
            "uid": state.column(inforce.UID),
            "t": state.column("_t"),
            "i": grid.positions(state.column("_t")),
            **{f"p{index}": state.column(name) for index, name in enumerate(own)},
        }
    )
    connection = inforce.connect()
    try:
        connection.register("series", pa.table({"uid": series}))
        groups: dict[Neighbours, str] = {}
        for column in spec.features:
            if column.neighbours is not None and column.neighbours not in groups:
                name = f"a{len(groups)}"
                found = _neighbour_values(connection, reads, column.neighbours, series, grid)
                connection.register(name, found)
                groups[column.neighbours] = name
        connection.register("rows", rows)
        result = connection.sql(_query(spec, own, groups, grid.base - 1)).to_arrow_table()
    finally:
        connection.close()
    table = pa.table(
        {
            UNIQUE_ID: result.column(0).cast(pa.string()),
            DS: result.column(1).cast(inforce.TIME),
            Y: result.column(2).cast(pa.float64()),
            **{
                column.name: result.column(index).cast(pa.float64())
                for index, column in enumerate(spec.features, start=3)
            },
        }
    )
    if spec.label is not None and horizon is not None:
        table = table.append_column(LABEL, result.column(3 + len(spec.features)).cast(pa.float64()))
        ahead = pa.scalar(horizon // _MICROSECOND, pa.duration("us"))
        table = table.append_column(LABEL_DS, pc.add(table.column(DS), ahead).cast(inforce.TIME))
    log.info(
        "frame built",
        target=ref.target,
        first=first.seq,
        last=last.seq,
        series=len(series),
        points=grid.points,
        columns=len(names),
        rows=table.num_rows,
        types_read=reads.count,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return _Built(table.combine_chunks(), spec, first, last, grid, len(series))


def _refused(error: DtaError, ref: FrameRef) -> None:
    log.warn(
        "frame refused",
        code=error.code,
        reason=error.message,
        target=ref.target,
        start=ref.start,
        end=ref.end,
    )


def frame(store: Store, ontology: Ontology, ref: FrameRef) -> pa.Table:
    """The frame a reference names: `unique_id`, `ds`, `y`, then a column per feature in
    the order given, every one a float; rows sorted by `unique_id`, `ds`.

    A row exists for a series at each grid time at which its object exists (after the
    last version's time: if it exists at the last version). `y` is null after the last
    version's time; a feature there is known where its lag reaches back to it.
    """
    try:
        return _build(store, ontology, ref).table
    except DtaError as error:
        _refused(error, ref)
        raise


def _backfill_until(store: Store, ontology: Ontology, last: VersionRef) -> datetime | None:
    """The data time up to which the history was loaded from what a source held of its
    past: the end of the last backfill run recorded by the last version."""
    runs = store.table(
        RUN_TYPE,
        columns=["finished_at"],
        filter=[("kind", "=", BACKFILL)],
        at=last,
        branch=last.branch,
        ontology=ontology,
    )
    return pc.max(runs.column("finished_at")).as_py() if runs.num_rows else None


def _version(version: VersionRef) -> dict[str, Any]:
    return {"id": version.id, "seq": version.seq, "t": iso_utc(version.t)}


def _metadata(
    built: _Built, request: TrainingSetRequest, backfill_until: datetime | None
) -> dict[str, Any]:
    spec, grid = built.spec, built.grid
    target = f"{spec.type.name}.{spec.target}"
    columns = [
        {"name": UNIQUE_ID, "is": f"uid of the {spec.type.name}"},
        {"name": DS, "is": "grid time"},
        {"name": Y, "is": f"{target} in force at ds"},
        *({"name": column.name, "is": column.described} for column in spec.features),
    ]
    if spec.label is not None:
        columns += [
            {"name": LABEL, "is": f"{target} in force at label_ds"},
            {"name": LABEL_DS, "is": "ds plus the horizon"},
        ]
    if backfill_until is None:
        note = (
            "every value is the one stored at the last version at or before its time; "
            "no backfill wrote any of this history"
        )
    else:
        note = (
            "every value is the one stored at the last version at or before its time; "
            f"versions up to {iso_utc(backfill_until)} were written by a backfill, so rows at "
            "or before it hold what the source reported of its past when it was loaded, not "
            "necessarily what stood at their time: point-in-time correct only if the source "
            "never rewrote its past"
        )
    return {
        "target": target,
        "branch": built.last.branch,
        "start": _version(built.first),
        "end": _version(built.last),
        "step_s": request.step.total_seconds(),
        "horizon_s": None if request.horizon is None else request.horizon.total_seconds(),
        "ahead": request.ahead,
        "grid": {
            "first": iso_utc(grid.at(0)),
            "last": iso_utc(grid.at(grid.points - 1)),
            "points": grid.points,
        },
        "series": built.series,
        "rows": built.table.num_rows,
        "columns": columns,
        "backfill_until": None if backfill_until is None else iso_utc(backfill_until),
        "note": note,
    }


def training_set(store: Store, ontology: Ontology, request: TrainingSetRequest) -> TrainingSet:
    """The frame of the request and, with a horizon, the label of each row: `label` (the
    target as stored at the last version at or before `ds` plus the horizon) and
    `label_ds`. Rows whose label time is after the last version's time are left out.
    """
    try:
        built = _build(store, ontology, request, request.horizon)
        backfill_until = _backfill_until(store, ontology, built.last)
    except DtaError as error:
        _refused(error, request)
        raise
    return TrainingSet(built.table, _metadata(built, request, backfill_until))
