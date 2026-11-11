"""A short telemetry history and, worked out independently, what loading it must produce."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from dta_core.store import EPOCH
from sync_testkit import MINUTE, T0

Reading = tuple[str, datetime, float | None]

QUARTER = timedelta(minutes=15)
SECOND = timedelta(seconds=1)
# The `sync` section that replays history in intervals of a quarter of an hour.
BACKFILL = {"backfill_interval": 900}

READINGS: list[Reading] = [
    # Exactly on a multiple of the interval: the last instant of the interval ending there.
    ("T1.level", T0 + 15 * MINUTE, 1.0),
    # Just after it: the first instant of the next.
    ("T1.level", T0 + 15 * MINUTE + SECOND, 1.1),
    ("T1.level", T0 + 20 * MINUTE, 1.2),
    ("T1.level", T0 + 30 * MINUTE, 1.3),
    # Unchanged over a whole interval.
    ("T1.level", T0 + 31 * MINUTE, 1.3),
    ("T1.level", T0 + 45 * MINUTE, 1.3),
    ("T1.level", T0 + 50 * MINUTE, 2.0),
    ("J1.demand", T0 + 15 * MINUTE, 5.0),
    ("J1.demand", T0 + 40 * MINUTE, 6.0),
    ("J2.demand", T0 + 15 * MINUTE, 7.0),
    ("J2.demand", T0 + 44 * MINUTE, 7.0),
]
# The object and property each signal of `READINGS` is mapped onto.
OBJECT_OF = {
    "T1.level": ("node:T1", "level"),
    "J1.demand": ("node:J1", "demand"),
    "J2.demand": ("node:J2", "demand"),
}
REDUCE: dict[str, Callable[[list[tuple[datetime, float]]], float]] = {
    "last": lambda found: max(found)[1],
    "mean": lambda found: sum(value for _, value in found) / len(found),
    "min": lambda found: min(value for _, value in found),
    "max": lambda found: max(value for _, value in found),
}


def interval_end(moment: datetime, width: timedelta = QUARTER) -> datetime:
    """End of the interval `(end - width, end]` that holds a time."""
    whole, rest = divmod(moment - EPOCH, width)
    return EPOCH + (whole + (1 if rest else 0)) * width


def expected_changes(
    rows: list[Reading], aggregation: str = "last"
) -> dict[str, list[tuple[datetime, float]]]:
    """Per signal, the (version time, value) changes a backfill of `rows` must produce."""
    grouped: dict[str, dict[datetime, list[tuple[datetime, float]]]] = {}
    for signal, moment, value in rows:
        if value is not None:
            by_end = grouped.setdefault(signal, {})
            by_end.setdefault(interval_end(moment), []).append((moment, value))
    result: dict[str, list[tuple[datetime, float]]] = {}
    for signal, by_end in grouped.items():
        changes: list[tuple[datetime, float]] = []
        for end in sorted(by_end):
            value = REDUCE[aggregation](by_end[end])
            if not changes or changes[-1][1] != value:
                changes.append((end, value))
        result[signal] = changes
    return result
