"""A scenario beside the live line, or beside another scenario of the same base.

Both sides are read on the scenario's grid, from the base time to the end of
the horizon. A value is what was in force at that grid time on that side. A
side knows nothing later than its head: a grid time after the head's time is
null there, never the last value carried forward as if it were the future.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import Ontology
from dta_core.store import LIVE_BRANCH, Selector

from .. import inforce
from ..twin import Twin
from . import errors, records
from .errors import bad_request, problem, refused
from .plan import grid
from .records import BRANCH_PREFIX, DELETED, SCOPE
from .request import duration_of
from .targets import Target, resolve

log = Log("runtime.scenarios")

TARGET = "target"
UID = "uid"
T = "t"
SCENARIO = "scenario"
AGAINST = "against"
DIFFERENCE = "difference"
SCHEMA = pa.schema(
    [
        pa.field(TARGET, pa.string()),
        pa.field(UID, pa.string()),
        pa.field(T, inforce.TIME),
        pa.field(SCENARIO, pa.float64()),
        pa.field(AGAINST, pa.float64()),
        pa.field(DIFFERENCE, pa.float64()),
    ]
)
_SIDE = pa.schema(
    [
        pa.field(TARGET, pa.string()),
        pa.field(UID, pa.string()),
        pa.field(T, inforce.TIME),
        pa.field("v", pa.float64()),
    ]
)

# Every series that either side has, at every grid time, with each side's value beside it.
_JOIN = f"""
WITH series AS (
    SELECT DISTINCT {TARGET}, {UID} FROM (
        SELECT {TARGET}, {UID} FROM ours UNION ALL SELECT {TARGET}, {UID} FROM theirs
    )
)
SELECT s.{TARGET} AS {TARGET}, s.{UID} AS {UID}, a.{T} AS {T},
       l.v AS {SCENARIO}, r.v AS {AGAINST}, l.v - r.v AS {DIFFERENCE}
FROM series AS s CROSS JOIN axis AS a
LEFT JOIN ours AS l ON l.{TARGET} = s.{TARGET} AND l.{UID} = s.{UID} AND l.{T} = a.{T}
LEFT JOIN theirs AS r ON r.{TARGET} = s.{TARGET} AND r.{UID} = s.{UID} AND r.{T} = a.{T}
ORDER BY s.{TARGET}, s.{UID}, a.{T}
"""


def _targets(ontology: Ontology, targets: Sequence[str]) -> list[Target]:
    problems: list[dict[str, str]] = []
    found: list[Target] = []
    if isinstance(targets, str) or not targets:
        problems.append(problem("targets", "names at least one Type.property"))
    else:
        for index, text in enumerate(dict.fromkeys(targets)):
            try:
                target = resolve(ontology, text)
            except ValueError as exc:
                problems.append(problem(f"targets[{index}]", str(exc)))
                continue
            if not target.numeric:
                problems.append(
                    problem(
                        f"targets[{index}]",
                        f"{text} holds {'lists of ' if target.field.multivalued else ''}"
                        f"{target.field.kind.value} values; a trajectory is of numbers",
                    )
                )
                continue
            found.append(target)
    if problems:
        log.warn("comparison refused", problems=problems)
        raise bad_request(problems)
    return found


def _other_branch(twin: Twin, record: dict[str, Any], against: Any) -> str:
    """The branch a scenario is compared against: the live one, or another scenario's,
    named by the branch or by the scenario's uid."""
    if against == LIVE_BRANCH:
        return LIVE_BRANCH
    if isinstance(against, str) and against.startswith(f"{SCOPE}:"):
        other_uid = against
    elif isinstance(against, str) and against.startswith(BRANCH_PREFIX):
        other_uid = records.scenario_uid(against.removeprefix(BRANCH_PREFIX))
    else:
        found = [
            problem(
                "against",
                f"{against!r} is neither {LIVE_BRANCH}, a scenario's uid nor a scenario's branch",
            )
        ]
        log.warn("comparison refused", scenario=record["uid"], problems=found)
        raise bad_request(found, scenario=record["uid"])
    other = records.require(twin, other_uid)
    if other["status"] == DELETED:
        log.warn("comparison refused: the other scenario was deleted", scenario=other_uid)
        raise refused(errors.DELETED, f"{other_uid} was deleted", scenario=other_uid)
    if other["base_version"] != record["base_version"]:
        found = [
            problem(
                "against",
                f"{other_uid} starts at version {other['base_version']}, {record['uid']} at "
                f"{record['base_version']}; scenarios are compared from one base",
            )
        ]
        log.warn("comparison refused", scenario=record["uid"], problems=found)
        raise bad_request(found, scenario=record["uid"])
    return other["branch"]


def _side(
    twin: Twin,
    ontology: Ontology,
    branch: str,
    base: str,
    times: Sequence[datetime],
    targets: Sequence[Target],
    uids: Sequence[str] | None,
) -> pa.Table:
    """One side's values in force at the grid times it knows of: a row per target,
    object and time. Two reads of the store per concrete type, whatever the horizon."""
    store = twin.store
    first = store.resolve(base, branch)[0]
    head = store.head(branch)
    # Nothing after the end of the grid is needed, however far the side has gone on.
    last = head
    if head.t > times[-1]:
        last = store.resolve(Selector("time", time=times[-1]), branch)[0]
    known = [moment for moment in times if moment <= head.t]
    needs: dict[str, list[str]] = {}
    for target in targets:
        for member in target.members:
            names = needs.setdefault(member, [])
            if target.property not in names:
                names.append(target.property)
    parts = []
    for member, names in needs.items():
        events = inforce.events(
            store, ontology, member, first, last, properties=names, uids=uids or None
        )
        held = inforce.at_times(events, known)
        for target in targets:
            if member not in target.members:
                continue
            parts.append(
                pa.table(
                    {
                        TARGET: pa.array([target.text] * held.num_rows, pa.string()),
                        UID: held.column(inforce.UID),
                        T: held.column(inforce.T).cast(inforce.TIME),
                        "v": held.column(target.property).cast(pa.float64()),
                    },
                    schema=_SIDE,
                )
            )
    return pa.concat_tables(parts).combine_chunks() if parts else _SIDE.empty_table()


def compare(
    twin: Twin,
    uid: str,
    *,
    targets: Sequence[str],
    against: str = LIVE_BRANCH,
    uids: Sequence[str] | None = None,
) -> pa.Table:
    """The trajectories of `targets` in the scenario and on the side it is compared
    against, on the scenario's grid from its base time to the end of its horizon.

    `target`, `uid`, `t`, `scenario`, `against` and `difference` (scenario less
    against), sorted by target, object and time. `against` is the live branch, or
    another scenario of the same base by uid or branch name; `uids` keeps those objects.
    """
    started = time.perf_counter()
    record = records.require(twin, uid)
    if record["status"] == DELETED:
        log.warn("comparison refused: the scenario was deleted", scenario=uid)
        raise refused(errors.DELETED, f"{uid} was deleted", scenario=uid)
    other = _other_branch(twin, record, against)
    base_t: datetime = record["base_t"]
    step = records.step_of(twin, uid)
    steps = duration_of(record["horizon"]) // step
    times = grid(base_t, step, steps, first=0)
    # Both sides start at the base version, so its ontology shapes what both give.
    ontology = twin.store.ontology_at(record["base_version"])
    wanted = _targets(ontology, targets)
    if uids is not None and not uids:
        return SCHEMA.empty_table()

    ours = _side(twin, ontology, record["branch"], record["base_version"], times, wanted, uids)
    theirs = _side(twin, ontology, other, record["base_version"], times, wanted, uids)
    connection = inforce.connect()
    try:
        connection.register("ours", ours)
        connection.register("theirs", theirs)
        connection.register("axis", pa.table({T: pa.array(times, inforce.TIME)}))
        table = connection.sql(_JOIN).to_arrow_table().cast(SCHEMA)
    finally:
        connection.close()
    log.info(
        "scenario compared",
        scenario=uid,
        against=other,
        targets=[target.text for target in wanted],
        rows=table.num_rows,
        steps=steps,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return table
