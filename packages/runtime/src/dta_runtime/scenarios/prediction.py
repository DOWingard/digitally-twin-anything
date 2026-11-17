"""What a predictor answered, checked before any of it is written.

A prediction is written as it is given, one version per grid time, so it has to
be exactly a value per series at each of the scenario's grid times: nothing off
the grid, nothing twice, nothing missing and nothing that is not a number.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from ..protocol.families import DS, MEAN, UNIQUE_ID
from .errors import ScenarioError, bad_prediction
from .plan import grid

TIME = pa.timestamp("us", tz="UTC")
# How many offending series or times a refusal names.
NAMED = 5
_TEXT = (pa.string(), pa.large_string())


def _column(table: pa.Table, name: str) -> pa.Array:
    return table.column(name).combine_chunks()


def _named(values: pa.Array) -> list[Any]:
    return pc.unique(values).slice(0, NAMED).to_pylist()


def check(table: Any, *, model: str, base: datetime, step: timedelta, horizon: int) -> pa.Table:
    """The prediction as `unique_id`, `ds`, `mean`, or `scenario.bad_prediction` naming
    what is wrong with it. Further columns (intervals) are let through unread."""

    def bad(message: str, **detail: Any) -> ScenarioError:
        return bad_prediction(message, model=model, **detail)

    if not isinstance(table, pa.Table):
        raise bad(f"the predictor gave a {type(table).__name__}, not a table")
    missing = [name for name in (UNIQUE_ID, DS, MEAN) if name not in table.column_names]
    if missing:
        raise bad(f"the prediction has no column {', '.join(missing)}", missing=missing)
    wrong = []
    if table.schema.field(UNIQUE_ID).type not in _TEXT:
        wrong.append(f"{UNIQUE_ID} is {table.schema.field(UNIQUE_ID).type}, not string")
    if table.schema.field(DS).type != TIME:
        wrong.append(f"{DS} is {table.schema.field(DS).type}, not {TIME}")
    if table.schema.field(MEAN).type != pa.float64():
        wrong.append(f"{MEAN} is {table.schema.field(MEAN).type}, not double")
    if wrong:
        raise bad(f"the prediction's columns have other types: {'; '.join(wrong)}", types=wrong)
    if table.num_rows == 0:
        raise bad("the prediction holds no rows")

    uids = _column(table, UNIQUE_ID).cast(pa.string())
    times = _column(table, DS)
    means = _column(table, MEAN)
    for name, values in ((UNIQUE_ID, uids), (DS, times)):
        if values.null_count:
            raise bad(f"{values.null_count} rows have no {name}", rows=values.null_count)

    unusable = pc.invert(pc.fill_null(pc.is_finite(means), False))
    if pc.any(unusable).as_py():
        rows = pc.sum(unusable).as_py()
        raise bad(
            f"{rows} predicted values are null or not finite, the first for "
            f"{', '.join(_named(uids.filter(unusable)))}",
            rows=rows,
        )

    expected = pa.array(grid(base, step, horizon), TIME)
    off_grid = pc.invert(pc.is_in(times, value_set=expected))
    if pc.any(off_grid).as_py():
        found = [moment.isoformat() for moment in _named(times.filter(off_grid))]
        raise bad(
            f"{pc.sum(off_grid).as_py()} rows are at times that are not the base time plus "
            f"1 to {horizon} steps of {step.total_seconds():g} s: {', '.join(found)}",
            times=found,
        )

    plain = pa.table({UNIQUE_ID: uids, DS: times, MEAN: means})
    counted = plain.group_by([UNIQUE_ID, DS]).aggregate([([], "count_all")])
    repeated = counted.filter(pc.greater(counted.column("count_all"), 1))
    if repeated.num_rows:
        series = _named(repeated.column(UNIQUE_ID).combine_chunks())
        raise bad(
            f"{repeated.num_rows} pairs of series and time are given more than once, the "
            f"first for {', '.join(series)}",
            series=series,
        )
    per_series = plain.group_by([UNIQUE_ID]).aggregate([([], "count_all")])
    short = per_series.filter(pc.not_equal(per_series.column("count_all"), horizon))
    if short.num_rows:
        series = _named(short.column(UNIQUE_ID).combine_chunks())
        raise bad(
            f"{short.num_rows} series do not have a value at each of the {horizon} steps, "
            f"the first {', '.join(series)}",
            series=series,
        )
    return plain
