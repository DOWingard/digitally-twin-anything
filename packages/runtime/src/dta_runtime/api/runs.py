"""The twin's record of its sync runs, newest first, read a window of time at a time.

The store hands out a type's rows filtered, and in uid order only. Runs are
listed by when they started, so they are read in windows of `started_at`
going back from the head, each holding at most `WINDOW_ROWS` runs, and sorted
within the window. A listing holds one window at a time however many runs the
twin has recorded, and reads only their uid and start until it knows which
runs make the page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import UID_FIELD, Ontology
from dta_core.store import Store, VersionRef

from ..sync.records import RUN_SCOPE, RUN_TYPE
from .formats import rows
from .problems import ApiError
from .tables import iso, readable

log = Log("api.runs")

WINDOW_ROWS = 10_000
STARTED = "started_at"
_MICROSECOND = timedelta(microseconds=1)

Triple = tuple[str, str, Any]


@dataclass(frozen=True)
class RunPage:
    data: list[dict[str, Any]]
    # Where the next page starts: the start and uid of the last run of this one.
    next_position: dict[str, str] | None


def _bad_cursor() -> ApiError:
    return ApiError("the cursor is not one this API issued", code="api.bad_cursor")


def position_of(payload: Any) -> tuple[datetime, str] | None:
    """(start, uid) of the last run of the previous page, as a cursor holds them."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise _bad_cursor()
    text, uid = payload.get("t"), payload.get("u")
    if not isinstance(text, str) or not isinstance(uid, str) or not uid.startswith(RUN_SCOPE):
        raise _bad_cursor()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _bad_cursor() from exc
    if moment.tzinfo is None:
        raise _bad_cursor()
    return moment, uid


def _keys(table: pa.Table) -> list[tuple[datetime, str]]:
    return list(
        zip(table.column(STARTED).to_pylist(), table.column(UID_FIELD).to_pylist(), strict=True)
    )


def list_runs(
    store: Store,
    ontology: Ontology,
    version: VersionRef,
    *,
    filters: Sequence[Triple],
    limit: int,
    position: tuple[datetime, str] | None,
    span: timedelta,
    floor: datetime | None,
) -> RunPage:
    """Runs newest first by start, then by uid, from just after `position`.

    `span` is the first window's width: about `limit` sync intervals holds a
    page. `floor` is a time no run started before.
    """
    if floor is None or not ontology.has_type(RUN_TYPE):
        return RunPage([], None)
    need = limit + 1
    found: list[tuple[datetime, str]] = []

    def read(conditions: list[Triple], most: int) -> pa.Table:
        return store.table(
            RUN_TYPE,
            columns=[STARTED],
            filter=[*filters, *conditions],
            limit=most,
            at=version,
            ontology=ontology,
        )

    if position is not None:
        moment, uid = position
        # Runs that started at the same moment follow in uid order, as the store gives them.
        found.extend(_keys(read([(STARTED, "=", moment), (UID_FIELD, ">", uid)], need)))
        high = moment
    else:
        # A run starts at the time of a version, never after the head.
        high = version.t + _MICROSECOND
    width = max(span, _MICROSECOND)
    windows = 0
    while len(found) < need and high > floor:
        low = max(high - width, floor)
        window = read([(STARTED, ">=", low), (STARTED, "<", high)], WINDOW_ROWS + 1)
        windows += 1
        if window.num_rows > WINDOW_ROWS:
            if high - low > _MICROSECOND:
                width = max((high - low) / 2, _MICROSECOND)
                continue
            # More runs began at one moment than a window holds: read on in uid order.
            log.warn(
                "more runs started at one moment than a window holds; read in uid order",
                started_at=iso(low),
                window_rows=WINDOW_ROWS,
            )
            found.extend(_keys(read([(STARTED, "=", low)], need - len(found))))
            high = low
            continue
        ordered = sorted(_keys(window), key=lambda key: key[1])
        ordered.sort(key=lambda key: key[0], reverse=True)
        found.extend(ordered)
        high = low
        width *= 2
    chosen = found[:limit]
    following = None
    if len(found) > limit:
        following = {"t": iso(chosen[-1][0]), "u": chosen[-1][1]}
    log.debug("runs listed", runs=len(chosen), windows=windows, head_seq=version.seq)
    if not chosen:
        return RunPage([], following)
    uids = [uid for _, uid in chosen]
    table = store.table(RUN_TYPE, filter=[(UID_FIELD, "in", uids)], at=version, ontology=ontology)
    by_uid = {row[UID_FIELD]: row for row in rows(readable(table, ontology.type(RUN_TYPE)))}
    return RunPage([by_uid[uid] for uid in uids if uid in by_uid], following)


def read_run(
    store: Store, ontology: Ontology, version: VersionRef, uid: str
) -> dict[str, Any] | None:
    """One run's record as of a version: its stored values and its uid."""
    if not uid.startswith(f"{RUN_SCOPE}:") or not ontology.has_type(RUN_TYPE):
        return None
    found = store.get(uid, at=version, ontology=ontology)
    if found is None or found.type != RUN_TYPE:
        return None
    return {UID_FIELD: uid, **found.values}
