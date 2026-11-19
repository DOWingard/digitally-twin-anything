"""What the rest of the twin reads of fidelity: the state, the suspect signals, the
incidents, the checks and the errors measured, at the head or at a version.

`standing` is on the path of every proposal, `state` on that of every approval and
`suspects` on that of every sync run: each reads one version of the store.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from dta_core.families import FAMILIES
from dta_core.log import Log
from dta_core.store import EPOCH, VersionRef

from ..twin import Twin
from . import records
from .errors import BAD_REQUEST, refused
from .ladder import HEALTHY, RESOLVED, UNRESOLVED
from .records import (
    CHECK_COLUMNS,
    CHECK_TYPE,
    FIDELITY_TYPE,
    INCIDENT_COLUMNS,
    INCIDENT_TYPE,
    KINDS,
    SIGNAL,
    SIGNAL_COLUMNS,
    SIGNAL_TYPE,
    STATE_UID,
    SUSPECT,
)

log = Log("runtime.fidelity")

At = VersionRef | str | None
Cell = tuple[str, str]
# The incidents not resolved, as a status filter.
UNRESOLVED_FILTER = "unresolved"


def _at(at: At) -> Any:
    return "head" if at is None else at


def _has(twin: Twin, type_name: str) -> bool:
    """Whether this code knows the type. A version written before the type existed
    reads as empty, which the store sees to without another read."""
    return twin.store.ontology.has_type(type_name)


def state(twin: Twin, at: At = None) -> dict[str, Any]:
    """The twin's state: `state` (unknown, healthy, correcting, failed), `since` (the data
    time it began) and `check` (the last check's uid)."""
    found = twin.store.get(STATE_UID, at=_at(at))
    if found is None or found.type != FIDELITY_TYPE:
        return records.state_document(None)
    return records.state_document(found.values)


def standing(twin: Twin, at: At = None) -> dict[str, Any]:
    """What the interlock judges by, both read at one version: the twin's `state` (with
    `since` and `check`, as `state` gives them) and `incidents`, every incident not
    resolved as `{"uid", "kind", "status"}`, oldest first; `version` is the id read. A
    signal incident whose value is unknown also has `withheld`: `{signal, property,
    object}`."""
    started = time.perf_counter()
    version = at if isinstance(at, VersionRef) else twin.store.resolve(_at(at))[0]
    document = state(twin, version)
    rows: list[dict[str, Any]] = []
    if _has(twin, INCIDENT_TYPE):
        rows = twin.store.table(
            INCIDENT_TYPE,
            columns=["kind", "status", "opened_at", "subject"],
            filter=[("status", "!=", RESOLVED)],
            at=version,
        ).to_pylist()
    rows.sort(key=lambda row: (records.utc(row.get("opened_at")) or EPOCH, row["uid"]))
    cells = (
        {found["uid"]: cell for cell, found in unknown(twin, version).items()}
        if any(row["kind"] == SIGNAL for row in rows)
        else {}
    )
    incidents = []
    for row in rows:
        found = {"uid": row["uid"], "kind": row["kind"], "status": row["status"]}
        cell = cells.get(row.get("subject")) if row["kind"] == SIGNAL else None
        if cell is not None:
            found["withheld"] = {"signal": row["subject"], "property": cell[0], "object": cell[1]}
        incidents.append(found)
    log.debug(
        "fidelity standing read",
        state=document["state"],
        incidents=[found["kind"] for found in incidents],
        withheld=sorted(cells),
        seq=version.seq,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return {**document, "incidents": incidents, "version": version.id}


def unhealthy_since(twin: Twin, at: At = None) -> datetime | None:
    """When the twin last stopped being healthy: the `since` of the state that followed
    its last healthy one, or of its first state when it never was healthy. None while it
    is healthy, and before any check. However many states it passes through until it is
    healthy again, the answer stays the same: it names one stretch of not being healthy.
    """
    started = time.perf_counter()
    version = at if isinstance(at, VersionRef) else twin.store.resolve(_at(at))[0]
    rows = twin.store.history(
        STATE_UID, properties=["state", "since"], range=f"#0..{version.id}"
    ).to_pylist()
    rows.sort(key=lambda row: row["_seq"])
    found: datetime | None = None
    previous: str | None = None
    for row in rows:
        current = row.get("state")
        if current == HEALTHY:
            found = None
        elif current != previous and (previous is None or previous == HEALTHY):
            found = records.utc(row.get("since")) or records.utc(row.get("_t"))
        previous = current
    log.debug(
        "the start of the stretch the twin has not been healthy read",
        since=found.isoformat() if found else None,
        states=len(rows),
        seq=version.seq,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return found


def suspects(twin: Twin, at: At = None) -> set[Cell]:
    """(`Type.property`, object uid) of every suspect signal: what sync leaves out."""
    if not _has(twin, SIGNAL_TYPE):
        return set()
    table = twin.store.table(
        SIGNAL_TYPE,
        columns=["property", "object"],
        filter=[("status", "=", SUSPECT)],
        at=_at(at),
    )
    return set(
        zip(table.column("property").to_pylist(), table.column("object").to_pylist(), strict=True)
    )


def unknown(twin: Twin, at: At = None) -> dict[Cell, dict[str, Any]]:
    """(`Type.property`, object uid) of every suspect signal whose value in force is
    unknown, with the signal's document: what nothing may compute from."""
    if not _has(twin, SIGNAL_TYPE):
        return {}
    rows = twin.store.table(
        SIGNAL_TYPE, columns=SIGNAL_COLUMNS, filter=[("status", "=", SUSPECT)], at=_at(at)
    ).to_pylist()
    found: dict[Cell, dict[str, Any]] = {}
    for row in rows:
        document = records.signal_document(row)
        if records.unknown_of(document) is not None:
            found[(document["property"], document["object"])] = document
    return found


def _features(model: Mapping[str, Any]) -> list[str]:
    """The property of each feature of a model record: a bare `Type.property`, or the
    JSON of a feature naming one."""
    found = []
    for text in model.get("features") or []:
        text = str(text)
        if text.lstrip().startswith("{"):
            text = str((records.loads(text, {}) or {}).get("property") or "")
        if text:
            found.append(text)
    return found


def model_inputs(model: Mapping[str, Any]) -> list[str] | None:
    """What a prediction of a model record reads from the twin besides the series it
    forecasts, as written (`Type.property`, an abstract type allowed): its features and
    what it declares in `reads`. None for code of the twin's own that declares no
    `reads`: it is taken to read every property."""
    declared = model.get("reads")
    if declared is None and model.get("family") not in FAMILIES:
        return None
    return _features(model) + [str(text) for text in declared or []]


def reads_property(
    model: Mapping[str, Any],
    property_text: str,
    concrete: Callable[[str], Iterable[str]] | None = None,
) -> bool:
    """Whether a prediction of a model record reads a property (a concrete
    `Type.property`). A shipped family reads its target, its features and what it
    declares in `reads`; code of the twin's own reads its features and what it
    declares; code that declares nothing is taken to read every property. `concrete`
    gives the concrete properties a written one stands for (an abstract type stands for
    its concrete types); a written one stands for itself when absent."""
    written = model_inputs(model)
    if written is None:
        return True
    if model.get("family") in FAMILIES and model.get("target"):
        written = [*written, str(model["target"])]
    expand = concrete or (lambda text: (text,))
    return any(property_text in expand(text) for text in written)


def model_properties(model: Mapping[str, Any]) -> list[str] | None:
    """What a model record computes from, as `Type.property`: its target, the property
    of each feature and what it declares it reads. None for code of the twin's own that
    declares no `reads`: every property."""
    inputs = model_inputs(model)
    if inputs is None:
        return None
    return ([str(model["target"])] if model.get("target") else []) + inputs


def withheld(
    cells: Mapping[Cell, Mapping[str, Any]],
    properties: Iterable[str] | None,
    *,
    uids: Iterable[str] | None = None,
    concrete: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """The unknown signals among `cells` (as `unknown` gives them) on any of `properties`
    (`Type.property`; an abstract type stands for its concrete types in `concrete`; None
    for every property), of the objects `uids` when given: what a computation reading
    those properties may not compute from, as `{signal, property, object, since, why}`."""
    names: set[str] | None = None
    if properties is not None:
        names = set()
        for text in properties:
            type_name, _, name = str(text).partition(".")
            names.add(text)
            for found in (concrete or {}).get(type_name, ()):
                names.add(f"{found}.{name}")
    only = None if uids is None else set(uids)
    hits = []
    for (property_text, uid), document in sorted(cells.items()):
        if (names is not None and property_text not in names) or (
            only is not None and uid not in only
        ):
            continue
        state = records.unknown_of(document) or {}
        hits.append(
            {
                "signal": document["uid"],
                "property": property_text,
                "object": uid,
                "since": state.get("since"),
                "why": state.get("why"),
            }
        )
    return hits


def observed_reading(property_name: str, uid: str, value: Any, *, t: datetime) -> dict[str, Any]:
    """The `TwinSignal` row that keeps a suspect signal's raw reading, for sync to write
    in place of the value: `{"uid", "reading", "reading_at"}`, a partial row."""
    return {
        "uid": records.signal_uid(property_name, uid),
        "reading": records.dumps(value),
        "reading_at": records.aware(t, "t"),
    }


def withhold(
    upserts: Mapping[str, Iterable[Mapping[str, Any]]],
    suspect: set[Cell],
    *,
    t: datetime,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Rows of a sync's change set without the values of suspect signals, and the
    `TwinSignal` rows that keep those readings instead. A row left with only its uid is
    kept: it still says the object is there."""
    kept: dict[str, list[dict[str, Any]]] = {}
    readings: list[dict[str, Any]] = []
    by_type: dict[str, set[str]] = {}
    for text, _ in suspect:
        type_name, _, name = text.partition(".")
        by_type.setdefault(type_name, set()).add(name)
    for type_name, rows in upserts.items():
        names = by_type.get(type_name, set())
        out = []
        for row in rows:
            uid = row.get("uid")
            held = [
                name for name in names if name in row and (f"{type_name}.{name}", uid) in suspect
            ]
            if not held:
                out.append(dict(row))
                continue
            out.append({key: value for key, value in row.items() if key not in held})
            readings.extend(
                observed_reading(f"{type_name}.{name}", uid, row[name], t=t)
                for name in sorted(held)
            )
        kept[type_name] = out
    return kept, readings


def _filters(**equal: Any) -> list[tuple[str, str, Any]]:
    return [(name, "=", value) for name, value in equal.items() if value is not None]


def incidents(
    twin: Twin,
    *,
    at: At = None,
    kind: str | None = None,
    status: str | None = None,
    subject: str | None = None,
) -> list[dict[str, Any]]:
    """Incidents, oldest first; `status` may be `unresolved` for every one not resolved."""
    if kind is not None and kind not in KINDS:
        raise refused(BAD_REQUEST, f"kind is one of {', '.join(KINDS)}", kind=kind)
    if status is not None and status not in (*UNRESOLVED, RESOLVED, UNRESOLVED_FILTER):
        raise refused(BAD_REQUEST, f"no incident status {status}", status=status)
    if not _has(twin, INCIDENT_TYPE):
        return []
    filters = _filters(kind=kind, subject=subject)
    if status == UNRESOLVED_FILTER:
        filters.append(("status", "!=", RESOLVED))
    elif status is not None:
        filters.append(("status", "=", status))
    rows = twin.store.table(
        INCIDENT_TYPE, columns=INCIDENT_COLUMNS, filter=filters or None, at=_at(at)
    ).to_pylist()
    return sorted(
        (records.incident_document(row) for row in rows),
        key=lambda found: (found["opened_at"], found["uid"]),
    )


def incident(twin: Twin, uid: str, at: At = None) -> dict[str, Any] | None:
    """One incident, with `resolved_version`: the version that resolved it, when it is."""
    if not _has(twin, INCIDENT_TYPE):
        return None
    rows = twin.store.table(
        INCIDENT_TYPE, columns=INCIDENT_COLUMNS, filter=[("uid", "=", uid)], at=_at(at)
    ).to_pylist()
    if not rows:
        return None
    document = records.incident_document(rows[0])
    document["resolved_version"] = None
    if document["status"] == RESOLVED:
        started = time.perf_counter()
        history = twin.store.history(
            uid, properties=["status"], range=f"#0..{_version_id(twin, at)}"
        ).to_pylist()
        resolved = [row for row in history if row.get("status") == RESOLVED]
        if resolved:
            document["resolved_version"] = resolved[-1]["_version"]
        log.debug(
            "incident history read",
            incident=uid,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
    return document


def _version_id(twin: Twin, at: At) -> str:
    if isinstance(at, VersionRef):
        return at.id
    return twin.store.resolve(_at(at))[0].id


def signals(twin: Twin, *, at: At = None, status: str | None = None) -> list[dict[str, Any]]:
    """Signals ever found suspect, by property and object."""
    if status is not None and status not in (SUSPECT, records.OK):
        raise refused(BAD_REQUEST, f"no signal status {status}", status=status)
    if not _has(twin, SIGNAL_TYPE):
        return []
    rows = twin.store.table(
        SIGNAL_TYPE, columns=SIGNAL_COLUMNS, filter=_filters(status=status) or None, at=_at(at)
    ).to_pylist()
    return sorted(
        (records.signal_document(row) for row in rows),
        key=lambda found: (found["property"], found["object"]),
    )


def signal(twin: Twin, uid: str, at: At = None) -> dict[str, Any] | None:
    if not _has(twin, SIGNAL_TYPE):
        return None
    rows = twin.store.table(
        SIGNAL_TYPE, columns=SIGNAL_COLUMNS, filter=[("uid", "=", uid)], at=_at(at)
    ).to_pylist()
    return records.signal_document(rows[0]) if rows else None


def checks(
    twin: Twin,
    *,
    at: At = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Checks with `since` <= time <= `until`, newest first."""
    if not _has(twin, CHECK_TYPE):
        return []
    filters: list[tuple[str, str, Any]] = []
    if since is not None:
        filters.append(("at", ">=", records.aware(since, "since")))
    if until is not None:
        filters.append(("at", "<=", records.aware(until, "until")))
    rows = twin.store.table(
        CHECK_TYPE,
        columns=CHECK_COLUMNS,
        filter=filters or None,
        order_by=[("at", "desc")],
        limit=limit,
        at=_at(at),
    ).to_pylist()
    return [records.check_document(row) for row in rows]


def errors(
    twin: Twin,
    question: str,
    *,
    at: At = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """What the checks measured of one question, oldest first: per check its time, the
    origin, whether it was scored and why not, the origin's error, the window error and
    the warning sum."""
    if not _has(twin, CHECK_TYPE):
        return []
    filters: list[tuple[str, str, Any]] = []
    if since is not None:
        filters.append(("at", ">=", records.aware(since, "since")))
    if until is not None:
        filters.append(("at", "<=", records.aware(until, "until")))
    rows = twin.store.table(
        CHECK_TYPE,
        columns=["at", "questions"],
        filter=filters or None,
        order_by=[("at", "asc")],
        at=_at(at),
    ).to_pylist()
    found = []
    for row in rows:
        entry = (records.loads(row["questions"], {}) or {}).get(question)
        if entry is None:
            continue
        window = entry.get("window") or {}
        warning = entry.get("warning") or {}
        found.append(
            {
                "check": row["uid"],
                "at": records.utc(row["at"]),
                "model": entry.get("model"),
                "origin": records.parse_time(entry.get("origin")),
                "scored": bool(entry.get("scored")),
                "reason": entry.get("reason"),
                "error": entry.get("error"),
                "n": entry.get("n"),
                "tolerance": entry.get("tolerance"),
                "window_error": window.get("error"),
                "window_n": window.get("n"),
                "breach": bool(window.get("breach")),
                "warning_sum": warning.get("sum"),
                "warning": bool(warning.get("raised")),
            }
        )
    return found
