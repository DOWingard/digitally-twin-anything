"""The grid of a scenario and what is assumed at each of its times.

Grid time j is the base time plus j steps; the base is grid time 0. An
intervention takes effect at the first grid time at or after its start and stops
at the first at or after its end: on a branch that has one version per grid time
nothing can be told apart more finely than that. When it stops, the property goes
back to the value it had at the base version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# One written cell: (concrete type, uid, property, value).
Cell = tuple[str, str, str, Any]


@dataclass(frozen=True)
class Assumed:
    """One intervention, placed on the grid of its scenario."""

    # The concrete type of the object, whatever class the request named.
    type: str
    uid: str
    property: str
    value: Any
    # Grid time it takes effect at; 0 is the base.
    on: int
    # Grid time the property is set back at; None when it holds to the end of the horizon.
    off: int | None
    # What the property held at the base version.
    base_value: Any

    def in_force(self, j: int) -> bool:
        return self.on <= j and (self.off is None or j < self.off)


def steps_until(moment: datetime, base: datetime, step: timedelta) -> int:
    """How many steps after the base the first grid time at or after `moment` is."""
    return -((base - moment) // step)


def grid(base: datetime, step: timedelta, steps: int, first: int = 1) -> list[datetime]:
    return [base + j * step for j in range(first, steps + 1)]


def place(
    type_name: str,
    uid: str,
    property_name: str,
    value: Any,
    start: datetime,
    end: datetime | None,
    base_value: Any,
    *,
    base: datetime,
    step: timedelta,
) -> Assumed:
    return Assumed(
        type=type_name,
        uid=uid,
        property=property_name,
        value=value,
        on=steps_until(start, base, step),
        off=None if end is None else steps_until(end, base, step),
        base_value=base_value,
    )


def assumed_at(assumptions: Sequence[Assumed], j: int) -> list[Cell]:
    """The assumed facts that change at grid time j: the cells written there.

    Per property of an object: the value of the intervention that takes effect at j,
    else the base value when one stops at j and none holds there. One that starts and
    ends between the same two grid times never shows, and leaves the base value.
    Interventions on one property do not overlap, so at most one holds at a time.
    """
    cells: dict[tuple[str, str], Cell] = {}
    stopped: dict[tuple[str, str], Assumed] = {}
    held: set[tuple[str, str]] = set()
    for found in assumptions:
        key = (found.uid, found.property)
        if found.in_force(j):
            held.add(key)
            if found.on == j:
                cells[key] = (found.type, found.uid, found.property, found.value)
        elif found.off == j:
            stopped[key] = found
    for key, found in stopped.items():
        if key not in held:
            cells[key] = (found.type, found.uid, found.property, found.base_value)
    return [cells[key] for key in sorted(cells)]
