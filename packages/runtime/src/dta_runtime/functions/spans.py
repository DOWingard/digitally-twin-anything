"""The built-ins that span versions: `below` and `above` over a range, `integrate`
and `series`.

Each starts from one table of events (`inforce.events`: the state of the property
at the first version of the range and every change after it up to the last) and
works on that table alone, in DuckDB, so its cost is in the objects and the
changes of the range and never in objects times versions.

A value is in force from the time of the version that set it until the time of
the next version that changed it. Of several versions with the same time, the
latest is the one in force at that time, so the earlier ones of them are in force
for no time at all: they begin no spell and add nothing to an integral. Times
are handled as whole microseconds since the Unix epoch, which is what the store
keeps, so no arithmetic on them depends on a time zone.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.schema import UID_FIELD
from dta_core.store import EPOCH, VersionRef

from .. import inforce
from ..protocol import Answer
from ..sync.units import UnitProblem, conversion
from .pointwise import VALUE, bound_of
from .reads import Reads, Target

log = Log("functions.spans")

# Most rows an answer on a regular grid may have: objects times periods, or times steps.
MAX_GRID_ROWS = 5_000_000
_MICRO = timedelta(microseconds=1)
_INT_TIME = pa.int64()

# The event in force at each time an object has one.
_LATEST = """
latest AS (
    SELECT uid, t, removed, value FROM events
    QUALIFY row_number() OVER (PARTITION BY uid, t ORDER BY seq DESC) = 1
)"""

# A spell is a maximal run of events at which the bound is passed: the events between
# two at which it is not share the count of those seen so far. It ends at the event
# that follows its last one; without one, it still holds at the end of the range.
_SPELLS = f"""
WITH {_LATEST},
steps AS (
    SELECT uid, t, value,
           (NOT removed AND value IS NOT NULL AND value {{compare}} $bound) AS holds,
           lead(t) OVER (PARTITION BY uid ORDER BY t) AS next_t
    FROM latest
),
runs AS (
    SELECT *, sum(CASE WHEN holds THEN 0 ELSE 1 END) OVER (PARTITION BY uid ORDER BY t) AS run
    FROM steps
)
SELECT uid,
       min(t) AS start_t,
       CASE WHEN count(next_t) = count(*) THEN max(next_t) END AS end_t,
       {{extreme}}(value) AS extreme
FROM runs
WHERE holds
GROUP BY uid, run
ORDER BY uid, start_t
"""

# Each event holds its value from its time until the next event's, the last until the
# end of the range.
_HELD = f"""
WITH {_LATEST},
held AS (
    SELECT uid, t AS a, coalesce(lead(t) OVER (PARTITION BY uid ORDER BY t), $end) AS b,
           (NOT removed AND value IS NOT NULL) AS valued, value
    FROM latest
)"""

_INTEGRAL = f"""
{_HELD}
SELECT uid,
       coalesce(sum(value::DOUBLE * (b - a)) FILTER (WHERE valued), 0) / 1e6 AS value,
       coalesce(sum(b - a) FILTER (WHERE valued), 0) / 1e6 AS covered_s
FROM held
GROUP BY uid
ORDER BY uid
"""

# `k` numbers the periods from the epoch. A held value is cut into the periods it
# touches; the grid of every object and every period of the range is then filled
# from those pieces. The division rounds towards zero, so it is corrected by hand
# to round down for times before the epoch.
_INTEGRAL_BY_PERIOD = f"""
{_HELD},
pieces AS (
    SELECT uid, value, a, b,
           unnest(range((a - ((a % $p) + $p) % $p) // $p,
                        (b - 1 - (((b - 1) % $p) + $p) % $p) // $p + 1)) AS k
    FROM held
    WHERE valued AND b > a
),
sums AS (
    SELECT uid, k,
           sum(value::DOUBLE * (least(b, (k + 1) * $p) - greatest(a, k * $p))) / 1e6 AS value,
           sum(least(b, (k + 1) * $p) - greatest(a, k * $p)) / 1e6 AS covered_s
    FROM pieces
    GROUP BY uid, k
)
SELECT o.uid AS uid, p.k AS k,
       coalesce(s.value, 0.0) AS value, coalesce(s.covered_s, 0.0) AS covered_s
FROM (SELECT DISTINCT uid FROM events) AS o
CROSS JOIN (SELECT unnest(range($first_k, $last_k + 1)) AS k) AS p
LEFT JOIN sums AS s ON s.uid = o.uid AND s.k = p.k
ORDER BY o.uid, p.k
"""

# An object that is not there has no value, as one whose value is null has none: a
# row is a version at which what is in force is not what it was.
_CHANGES = """
SELECT row FROM (
    SELECT row, uid, seq,
           seq = $first AS initial,
           v IS DISTINCT FROM lag(v) OVER (PARTITION BY uid ORDER BY seq) AS changed
    FROM (SELECT row, uid, seq, CASE WHEN removed THEN NULL ELSE value END AS v FROM events)
)
WHERE initial OR changed
ORDER BY uid, seq
"""


def _micros(moment: datetime) -> int:
    return (moment - EPOCH) // _MICRO


def _times(micros: pa.Array | pa.ChunkedArray) -> pa.Array | pa.ChunkedArray:
    return micros.cast(_INT_TIME).cast(inforce.TIME)


def _query(events: pa.Table, target: Target, query: str, **parameters: Any) -> pa.Table:
    """Run a query over the events of one property, as the table `events`:
    `row`, `uid`, `seq`, `t` (microseconds), `removed`, `value`."""
    keys = pa.table(
        {
            "row": pa.array(np.arange(events.num_rows, dtype=np.int64)),
            "uid": events.column(inforce.UID),
            "seq": events.column(inforce.SEQ),
            "t": events.column(inforce.T).cast(_INT_TIME),
            "removed": events.column(inforce.REMOVED),
            "value": events.column(target.property),
        }
    )
    connection = inforce.connect()
    try:
        connection.register("events", keys)
        return connection.sql(query, params=parameters or None).to_arrow_table()
    finally:
        connection.close()


def _too_many(reads: Reads, parameter: str, rows: int, what: str) -> None:
    if rows > MAX_GRID_ROWS:
        raise reads.bad(
            parameter,
            f"{what} would give {rows} rows, more than {MAX_GRID_ROWS}; use a longer "
            f"{parameter}, a shorter range or fewer objects",
            rows=rows,
            most=MAX_GRID_ROWS,
        )


def _done(reads: Reads, what: str, started: float, events: pa.Table, rows: int) -> None:
    log.debug(
        what,
        function=reads.function,
        events=events.num_rows,
        rows=rows,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )


def threshold_over(
    reads: Reads,
    direction: str,
    target: Target,
    first: VersionRef,
    last: VersionRef,
    params: Mapping[str, Any],
) -> Answer:
    """The spells over which the value in force stayed beyond the bound."""
    bound = bound_of(reads, target, params["threshold"], params.get("unit"))
    events = reads.events(target, first, last, params.get("uids"), params.get("where"))
    started = time.perf_counter()
    query = _SPELLS.format(
        compare="<" if direction == "below" else ">",
        extreme="min" if direction == "below" else "max",
    )
    found = _query(events, target, query, bound=bound)
    result = pa.table(
        {
            UID_FIELD: found.column("uid"),
            "start": _times(found.column("start_t")),
            "end": _times(found.column("end_t")),
            "extreme": found.column("extreme").cast(events.schema.field(target.property).type),
        }
    )
    _done(reads, "spells found", started, events, result.num_rows)
    return Answer(value=result, unit=target.unit)


def _integral_unit(reads: Reads, target: Target, unit: str | None) -> tuple[str | None, float]:
    """The unit of the integral and the factor that takes the property's unit times
    seconds to it."""
    own = None if target.unit is None else f"{target.unit}.s"
    if unit is None or unit == own:
        return own, 1.0
    if own is None:
        raise reads.bad_unit(
            "unit", f"{target.name} has no unit; its integral cannot be given in {unit}"
        )
    try:
        found = conversion(own, unit)
    except UnitProblem as exc:
        raise reads.bad_unit(
            "unit",
            f"the integral of {target.name} is in {own}, which cannot be given in {unit}: {exc}",
            unit=unit,
            property_unit=target.unit,
        ) from exc
    if found.offset != 0.0:
        # A sum over time has no zero point to move.
        raise reads.bad_unit(
            "unit",
            f"{own} becomes {unit} with an offset, which an integral cannot take",
            unit=unit,
            property_unit=target.unit,
        )
    return unit, found.factor


def integrate(reads: Reads, params: Mapping[str, Any]) -> Answer:
    target = reads.target("property", params["property"], numeric=True)
    unit, factor = _integral_unit(reads, target, params.get("unit"))
    first, last = reads.span(params["from"], params["to"])
    period: timedelta | None = params.get("period")
    events = reads.events(target, first, last, params.get("uids"))
    started = time.perf_counter()
    end = _micros(last.t)
    if period is None:
        found = _query(events, target, _INTEGRAL, end=end)
        columns = {UID_FIELD: found.column("uid")}
    else:
        width = period // _MICRO
        if width < 1:
            raise reads.bad("period", "period is shorter than a microsecond")
        first_k = _micros(first.t) // width
        # A period that begins where the range ends holds none of it.
        last_k = max(first_k, (end - 1) // width)
        objects = len(inforce.objects(events))
        _too_many(reads, "period", objects * (last_k - first_k + 1), "one row per period")
        found = _query(
            events, target, _INTEGRAL_BY_PERIOD, end=end, p=width, first_k=first_k, last_k=last_k
        )
        columns = {
            UID_FIELD: found.column("uid"),
            "period_start": _times(pc.multiply(found.column("k"), pa.scalar(width, pa.int64()))),
        }
    values = found.column("value").cast(pa.float64())
    columns[VALUE] = values if factor == 1.0 else pc.multiply(values, pa.scalar(factor))
    columns["covered_s"] = found.column("covered_s").cast(pa.float64())
    result = pa.table(columns)
    _done(reads, "integrals computed", started, events, result.num_rows)
    return Answer(value=result, unit=unit)


def series(reads: Reads, params: Mapping[str, Any]) -> Answer:
    target = reads.target("property", params["property"])
    first, last = reads.span(params["from"], params["to"])
    step: timedelta | None = params.get("step")
    events = reads.events(target, first, last, params.get("uids"))
    started = time.perf_counter()
    if step is None:
        rows = _query(events, target, _CHANGES, first=first.seq).column("row")
        kept = events.take(rows)
        result = pa.table(
            {
                UID_FIELD: kept.column(inforce.UID),
                "t": kept.column(inforce.T),
                VALUE: kept.column(target.property),
            }
        )
    else:
        width = step // _MICRO
        if width < 1:
            raise reads.bad("step", "step is shorter than a microsecond")
        start, end = _micros(first.t), _micros(last.t)
        objects = len(inforce.objects(events))
        _too_many(reads, "step", objects * ((end - start) // width + 1), "one row per step")
        grid = pa.array(np.arange(start, end + 1, width, dtype=np.int64))
        found = inforce.at_times(events, _times(grid))
        result = pa.table(
            {
                UID_FIELD: found.column(inforce.UID),
                "t": found.column("_t"),
                VALUE: found.column(target.property),
            }
        )
    _done(reads, "series read", started, events, result.num_rows)
    return Answer(value=result, unit=target.unit)
