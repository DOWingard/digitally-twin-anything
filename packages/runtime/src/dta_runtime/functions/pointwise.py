"""The built-ins that read one version: `latest`, `below` and `above` without a range,
and `aggregate`. Each is one read per concrete type and arithmetic on its columns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.schema import UID_FIELD

from ..protocol import Answer
from ..sync.units import UnitProblem, conversion
from .reads import Reads, Target

VALUE = "value"
_COMPARE = {"below": pc.less, "above": pc.greater}
_SCALAR_OPS = {"sum": pc.sum, "mean": pc.mean, "min": pc.min, "max": pc.max}


def latest(reads: Reads, params: Mapping[str, Any]) -> Answer:
    target = reads.target("property", params["property"])
    uids, against = params.get("uids"), params.get("against")
    # Resolved before anything is read, so that a version that cannot be compared
    # with costs nothing.
    earlier = None if against is None else reads.version("against", against)
    now = reads.objects(target, [target.property], uids, params.get("where"))
    columns = {UID_FIELD: now.column(UID_FIELD), VALUE: now.column(target.property)}
    if earlier is not None:
        # Whether an object passes `where` is decided at the version read; the earlier
        # version only says what its value was then.
        before = reads.objects(target, [target.property], uids, None, at=earlier)
        position = pc.index_in(
            now.column(UID_FIELD), value_set=before.column(UID_FIELD).combine_chunks()
        )
        # An object that was not there then has no position, and so a null.
        previous = before.column(target.property).combine_chunks().take(position)
        columns["previous"] = previous
        if target.numeric:
            columns["change"] = pc.subtract(columns[VALUE], previous)
    return Answer(value=pa.table(columns), unit=target.unit)


def bound_of(reads: Reads, target: Target, threshold: float, unit: str | None) -> float:
    """The threshold in the property's unit: it is converted once, never the data."""
    if unit is None or unit == target.unit:
        return threshold
    if target.unit is None:
        raise reads.bad_unit(
            "unit", f"{target.name} has no unit; a threshold in {unit} cannot be compared with it"
        )
    try:
        return conversion(unit, target.unit).apply(threshold)
    except UnitProblem as exc:
        raise reads.bad_unit(
            "unit",
            f"a threshold in {unit} cannot be compared with {target.name}, which is in "
            f"{target.unit}: {exc}",
            unit=unit,
            property_unit=target.unit,
        ) from exc


def threshold_at(reads: Reads, direction: str, target: Target, params: Mapping[str, Any]) -> Answer:
    """The objects whose value is strictly beyond the bound at the pinned version."""
    bound = bound_of(reads, target, params["threshold"], params.get("unit"))
    table = reads.objects(target, [target.property], params.get("uids"), params.get("where"))
    values = table.column(target.property)
    # An object with no value compares as null and is left out with the rest.
    kept = table.filter(_COMPARE[direction](values, pa.scalar(bound, pa.float64())))
    return Answer(
        value=pa.table({UID_FIELD: kept.column(UID_FIELD), VALUE: kept.column(target.property)}),
        unit=target.unit,
    )


def aggregate(reads: Reads, params: Mapping[str, Any]) -> Answer:
    op, group_by = params["op"], params.get("group_by")
    target = reads.target("property", params["property"], numeric=op != "count")
    columns = [target.property]
    if group_by is not None:
        key = reads.field("group_by", target.type, target.members, group_by)
        if key.stored_as_json:
            raise reads.bad(
                "group_by",
                f"{target.type}.{group_by} holds lists or structured values and cannot be "
                "grouped by",
                type=target.type,
                property=group_by,
            )
        if group_by != target.property:
            columns.append(group_by)
    table = reads.objects(target, columns, params.get("uids"), params.get("where"))
    values = table.column(target.property)
    unit = None if op == "count" else target.unit
    if group_by is None:
        count = len(values) - values.null_count
        found = pa.scalar(count, pa.int64()) if op == "count" else _SCALAR_OPS[op](values)
        result = pa.table(
            {
                VALUE: pa.array([found.as_py()], found.type),
                "count": pa.array([count], pa.int64()),
            }
        )
        return Answer(value=result, unit=unit)
    keyed = pa.table({"group": table.column(group_by), VALUE: values})
    # Nulls of the key form a group of their own; of the values, only those that are
    # not null are counted and combined.
    functions = [(VALUE, "count")] if op == "count" else [(VALUE, op), (VALUE, "count")]
    grouped = keyed.group_by("group", use_threads=False).aggregate(functions)
    result = pa.table(
        {
            "group": grouped.column("group"),
            VALUE: grouped.column(f"{VALUE}_{op}"),
            "count": grouped.column(f"{VALUE}_count"),
        }
    )
    order = pc.sort_indices(result, sort_keys=[("group", "ascending", "at_end")])
    return Answer(value=result.take(order), unit=unit)
