"""What is done to incidents and signals between checks: a correction reported back,
a retry or a resolution by hand, a signal cleared by hand, and the incidents other
parts of the twin open and close (an action that did not have its effect).

Each writes one version holding what changed and, when it moved, the twin's state;
each returns the record as it now stands with `events` (as a check gives them) and
`version` (the id of the version written, or null when nothing was).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from dta_core.log import Log
from dta_core.store import VersionRef
from dta_core.store.model import iso_utc

from ..twin import Twin
from . import ladder, reading, records
from .errors import BAD_REQUEST, CONFLICT, NOT_FOUND, refused
from .ladder import OPEN, RESOLVED
from .outcome import incident_events, state_event
from .records import (
    BREACH,
    FIDELITY_TYPE,
    INCIDENT_COLUMNS,
    INCIDENT_TYPE,
    RECONCILE,
    SIGNAL,
    SIGNAL_COLUMNS,
    SIGNAL_TYPE,
    STALE,
    STATE_UID,
    SUSPECT,
    WRITES,
    iso,
)
from .values import CorrectionRequest, CorrectionResult

log = Log("runtime.fidelity")

REGISTRATION, DERIVED, MANUAL = "registration", "derived", "manual"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _text(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise refused(BAD_REQUEST, f"{name} is a non-empty text", argument=name)
    return value


def _incident(twin: Twin, uid: str, head: VersionRef) -> dict[str, Any]:
    rows = twin.store.table(
        INCIDENT_TYPE, columns=INCIDENT_COLUMNS, filter=[("uid", "=", uid)], at=head
    ).to_pylist()
    if not rows:
        log.info("no such incident", incident=uid)
        raise refused(NOT_FOUND, f"there is no incident {uid}", incident=uid)
    return records.incident_document(rows[0])


def _signal(twin: Twin, uid: str, head: VersionRef) -> dict[str, Any] | None:
    rows = twin.store.table(
        SIGNAL_TYPE, columns=SIGNAL_COLUMNS, filter=[("uid", "=", uid)], at=head
    ).to_pylist()
    return records.signal_document(rows[0]) if rows else None


def _unresolved(twin: Twin, kind: str, subject: str, head: VersionRef) -> dict[str, Any] | None:
    rows = twin.store.table(
        INCIDENT_TYPE,
        columns=INCIDENT_COLUMNS,
        filter=[("kind", "=", kind), ("subject", "=", subject), ("status", "!=", RESOLVED)],
        at=head,
    ).to_pylist()
    if len(rows) > 1:
        log.warn(
            "more than one unresolved incident of one subject; the oldest is taken",
            kind=kind,
            subject=subject,
            incidents=sorted(row["uid"] for row in rows),
        )
    found = sorted((records.incident_document(row) for row in rows), key=lambda d: d["opened_at"])
    return found[0] if found else None


def _step(now: datetime, action: str, **fields: Any) -> dict[str, Any]:
    return {"at": iso_utc(now), "action": action, **fields}


def _cleared(document: Mapping[str, Any], *, by: str, note: str | None, now: datetime) -> dict:
    """A suspect signal as it is once it is cleared."""
    detail = dict(document.get("detail") or {})
    episodes = [dict(item) for item in detail.get("episodes") or []]
    if episodes and episodes[-1].get("until") is None:
        episodes[-1].update(until=iso_utc(now), cleared_by=by)
    detail.update(episodes=episodes, cleared={"by": by, "note": note, "at": iso_utc(now)})
    return {**document, "status": records.OK, "clear_checks": 0, "detail": detail}


def _write(
    twin: Twin,
    head: VersionRef,
    *,
    incidents: list[tuple[dict[str, Any] | None, dict[str, Any]]],
    signals: list[dict[str, Any]],
    now: datetime,
    cause: str,
    note: str,
    extra: Mapping[str, Any],
) -> tuple[VersionRef, list[dict[str, Any]]]:
    """Write changed incidents and signals and, when it moved, the state; the events."""
    statuses = {found["uid"]: found["status"] for found in reading.unresolved_incidents(twin, head)}
    statuses.update({after["uid"]: after["status"] for _, after in incidents})
    before = records.state_document(reading.state_row(twin, head))
    checked = reading.latest_check_time(twin, head) is not None
    after = ladder.state_of(statuses.values(), checked=checked)
    upserts: dict[str, list[dict[str, Any]]] = {
        INCIDENT_TYPE: [records.incident_row(document) for _, document in incidents],
        SIGNAL_TYPE: [records.signal_row(document) for document in signals],
    }
    if after != before["state"]:
        upserts[FIDELITY_TYPE] = [
            {"uid": STATE_UID, "state": after, "since": now, "check": before["check"]}
        ]
        log.info(
            "the twin's fidelity state moved", previous=before["state"], state=after, note=note
        )
    version = records.write(twin, upserts, now=now, cause=cause, note=note, extra=extra)
    events, _ = incident_events(incidents, check=None, at=now)
    moved = state_event(before["state"], after, since=now, check=None)
    if moved is not None:
        events.append(moved)
    return version, events


def _answer(document: Mapping[str, Any], version: VersionRef | None, events: list) -> dict:
    return {**document, "events": events, "version": None if version is None else version.id}


def corrected(
    twin: Twin, request: CorrectionRequest, result: CorrectionResult, *, now: datetime
) -> dict[str, Any]:
    """Record what a correction came to on its breach incident.

    Accepted: the model it put in place is awaited (`awaiting` is the end of its data)
    until three origins after it say whether it holds. Not accepted: the incident moves
    on, and the next check takes the next step. A warning's correction has no incident:
    nothing is written and {} is returned. A result the incident is not waiting for (a
    job reported twice, or after the incident moved on) changes nothing."""
    now = records.aware(now)
    if request.warning:
        log.info(
            "a pre-emptive recalibration was reported back; there is no incident to step",
            key=request.key,
            question=request.question,
            accepted=result.accepted,
            model=result.model,
        )
        return {}
    fitted_to = records.aware(result.fitted_to, "fitted_to") if result.fitted_to else None
    if result.accepted and fitted_to is None:
        log.warn("an accepted correction gave no end of its data", key=request.key)
        raise refused(
            BAD_REQUEST,
            "an accepted correction names the end of the data of the model it put in place",
            key=request.key,
        )
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        document = _incident(twin, request.incident, head)
        if document["kind"] != BREACH:
            raise refused(
                BAD_REQUEST,
                f"{request.incident} is a {document['kind']} incident; only a breach is corrected",
                incident=request.incident,
            )
        position = records.position_of(document)
        if position.pending is None or position.pending.key != request.key:
            log.warn(
                "a correction was reported back that its incident is not waiting for; "
                "nothing is written",
                incident=request.incident,
                key=request.key,
                pending=position.pending.key if position.pending else None,
                status=document["status"],
            )
            return _answer(document, None, [])
        rung = ladder.RUNG_NAMES.get(position.pending.rung)
        updated = records.with_position(
            document, ladder.answered(position, accepted=result.accepted, fitted_to=fitted_to)
        )
        detail = dict(updated["detail"])
        detail["last_result"] = {
            "key": request.key,
            "accepted": result.accepted,
            "model": result.model,
            "fitted_to": iso(fitted_to),
            "error": result.error,
            "detail": dict(result.detail),
        }
        updated["detail"] = detail
        updated["steps"] = [
            *document["steps"],
            _step(
                now,
                "result",
                key=request.key,
                rung=rung,
                accepted=result.accepted,
                model=result.model,
                fitted_to=iso(fitted_to),
                error=result.error,
            ),
        ]
        version, events = _write(
            twin,
            head,
            incidents=[(document, updated)],
            signals=[],
            now=now,
            cause=REGISTRATION,
            note=f"correction {request.key} {'accepted' if result.accepted else 'not accepted'}",
            extra={"fidelity": "corrected", "incident": request.incident, "key": request.key},
        )
    log.info(
        "correction recorded on its incident",
        incident=request.incident,
        key=request.key,
        accepted=result.accepted,
        model=result.model,
        fitted_to=iso(fitted_to),
        attempt=updated["attempt"],
        rung=updated["rung"],
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(updated, version, events)


def retry(twin: Twin, incident: str, *, by: str, note: str | None, now: datetime) -> dict[str, Any]:
    """Put a breach or stale incident back to its start: cycle 1, rung 1, nothing pending
    or awaited (breach); no stale check counted (stale). The next check takes it on."""
    now = records.aware(now)
    by = _text(by, "by") or ""
    note = _text(note, "note", optional=True)
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        document = _incident(twin, incident, head)
        kind, status = document["kind"], document["status"]
        if kind not in (BREACH, STALE):
            raise refused(
                BAD_REQUEST,
                f"{incident} is a {kind} incident; only breach and stale incidents are retried",
                incident=incident,
            )
        if status == RESOLVED:
            raise refused(CONFLICT, f"{incident} is resolved", incident=incident)
        step = _step(
            now,
            "retry",
            by=by,
            note=note,
            previous={
                "status": status,
                "rung": document["rung"],
                "attempt": document["attempt"],
                "cycle": document["cycle"],
            },
        )
        if kind == BREACH:
            updated = records.with_position(document, ladder.retried(records.position_of(document)))
        else:
            detail = {**document["detail"], "failed_at": None}
            updated = {**document, "status": OPEN, "attempt": 0, "detail": detail}
        updated["steps"] = [*document["steps"], step]
        version, events = _write(
            twin,
            head,
            incidents=[(document, updated)],
            signals=[],
            now=now,
            cause=MANUAL,
            note=f"incident {incident} retried by {by}",
            extra={"fidelity": "retry", "incident": incident, "by": by},
        )
    log.info(
        "incident retried",
        incident=incident,
        kind=kind,
        previous=status,
        by=by,
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(updated, version, events)


def resolve(
    twin: Twin, incident: str, *, by: str, note: str | None, now: datetime
) -> dict[str, Any]:
    """Resolve a signal or reconcile incident by hand. A signal incident's signal is
    cleared with it."""
    now = records.aware(now)
    by = _text(by, "by") or ""
    note = _text(note, "note", optional=True)
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        document = _incident(twin, incident, head)
        kind = document["kind"]
        if kind not in (SIGNAL, RECONCILE):
            raise refused(
                BAD_REQUEST,
                f"{incident} is a {kind} incident; only signal and reconcile incidents are "
                "resolved by hand",
                incident=incident,
            )
        if document["status"] == RESOLVED:
            raise refused(CONFLICT, f"{incident} is resolved", incident=incident)
        updated = {
            **document,
            "status": RESOLVED,
            "resolved_at": now,
            "detail": {**document["detail"], "resolved_by": by},
            "steps": [*document["steps"], _step(now, "resolve", by=by, note=note)],
        }
        signals = []
        if kind == SIGNAL:
            signal = _signal(twin, document["subject"], head)
            if signal is not None and signal["status"] == SUSPECT:
                signals.append(_cleared(signal, by=by, note=note, now=now))
        version, events = _write(
            twin,
            head,
            incidents=[(document, updated)],
            signals=signals,
            now=now,
            cause=MANUAL,
            note=f"incident {incident} resolved by {by}",
            extra={"fidelity": "resolve", "incident": incident, "by": by},
        )
    log.info(
        "incident resolved by hand",
        incident=incident,
        kind=kind,
        by=by,
        signals_cleared=[found["uid"] for found in signals],
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(updated, version, events)


def clear_signal(
    twin: Twin, signal: str, *, by: str, note: str | None, now: datetime
) -> dict[str, Any]:
    """Clear a suspect signal by hand; its incident is resolved with it."""
    now = records.aware(now)
    by = _text(by, "by") or ""
    note = _text(note, "note", optional=True)
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        document = _signal(twin, signal, head)
        if document is None:
            log.info("no such signal", signal=signal)
            raise refused(NOT_FOUND, f"there is no signal {signal}", signal=signal)
        if document["status"] != SUSPECT:
            raise refused(CONFLICT, f"{signal} is not suspect", signal=signal)
        cleared = _cleared(document, by=by, note=note, now=now)
        incidents = []
        found = _unresolved(twin, SIGNAL, signal, head)
        if found is not None:
            incidents.append(
                (
                    found,
                    {
                        **found,
                        "status": RESOLVED,
                        "resolved_at": now,
                        "detail": {**found["detail"], "resolved_by": by},
                        "steps": [*found["steps"], _step(now, "cleared", by=by, note=note)],
                    },
                )
            )
        version, events = _write(
            twin,
            head,
            incidents=incidents,
            signals=[cleared],
            now=now,
            cause=MANUAL,
            note=f"signal {signal} cleared by {by}",
            extra={"fidelity": "clear_signal", "signal": signal, "by": by},
        )
    log.info(
        "signal cleared by hand",
        signal=signal,
        property=document["property"],
        object=document["object"],
        by=by,
        incident=found["uid"] if found else None,
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(cleared, version, events)


def _reconcile_only(kind: str) -> None:
    if kind != RECONCILE:
        raise refused(
            BAD_REQUEST,
            f"incidents of kind {kind} are opened and closed by the checks; only reconcile "
            "incidents are opened and closed from outside",
            kind=kind,
        )


def open_incident(
    twin: Twin, kind: str, subject: str, detail: Mapping[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Open an incident of a subject (for a reconcile incident, the action record), or
    give back the one already open: nothing is written then."""
    now = records.aware(now)
    _reconcile_only(kind)
    subject = _text(subject, "subject") or ""
    try:
        opened = records.loads(records.dumps(dict(detail)), {})
    except (TypeError, ValueError) as exc:
        log.warn("an incident's detail is not JSON", kind=kind, subject=subject, error=str(exc))
        raise refused(BAD_REQUEST, f"detail cannot be written as JSON: {exc}") from exc
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        existing = _unresolved(twin, kind, subject, head)
        if existing is not None:
            log.info(
                "an incident of this subject is open already; it is given back",
                incident=existing["uid"],
                kind=kind,
                subject=subject,
            )
            return _answer(existing, None, [])
        number = 1
        uid = records.incident_uid(kind, subject, now)
        while twin.store.get(uid, at=head) is not None:
            number += 1
            uid = records.incident_uid(kind, subject, now, number)
        document = {
            "uid": uid,
            "kind": kind,
            "subject": subject,
            "status": OPEN,
            "opened_at": now,
            "opened_version": head.id,
            "resolved_at": None,
            "rung": 0,
            "attempt": 0,
            "cycle": 0,
            "awaiting": None,
            "requested_at": None,
            "detail": {"opened": opened},
            "steps": [_step(now, "opened")],
        }
        version, events = _write(
            twin,
            head,
            incidents=[(None, document)],
            signals=[],
            now=now,
            cause=DERIVED,
            note=f"{kind} incident opened on {subject}",
            extra={"fidelity": "open_incident", "incident": uid, "kind": kind},
        )
    log.info(
        "incident opened",
        incident=uid,
        kind=kind,
        subject=subject,
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(document, version, events)


def close_incident(
    twin: Twin,
    kind: str,
    subject: str,
    *,
    by: str,
    note: str | None,
    now: datetime,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve the open incident of a subject, as the part of the twin that opened it
    found it put right; None when none is open."""
    now = records.aware(now)
    _reconcile_only(kind)
    subject = _text(subject, "subject") or ""
    by = _text(by, "by") or ""
    note = _text(note, "note", optional=True)
    try:
        closing = records.loads(records.dumps(dict(detail or {})), {})
    except (TypeError, ValueError) as exc:
        log.warn("an incident's detail is not JSON", kind=kind, subject=subject, error=str(exc))
        raise refused(BAD_REQUEST, f"detail cannot be written as JSON: {exc}") from exc
    started = time.perf_counter()
    with WRITES:
        head = twin.store.head()
        reading.require(twin, head)
        existing = _unresolved(twin, kind, subject, head)
        if existing is None:
            log.debug("no open incident to close", kind=kind, subject=subject)
            return None
        updated = {
            **existing,
            "status": RESOLVED,
            "resolved_at": now,
            "detail": {**existing["detail"], "closed": closing, "resolved_by": by},
            "steps": [*existing["steps"], _step(now, "closed", by=by, note=note)],
        }
        version, events = _write(
            twin,
            head,
            incidents=[(existing, updated)],
            signals=[],
            now=now,
            cause=DERIVED,
            note=f"{kind} incident on {subject} closed by {by}",
            extra={"fidelity": "close_incident", "incident": existing["uid"], "kind": kind},
        )
    log.info(
        "incident closed",
        incident=existing["uid"],
        kind=kind,
        subject=subject,
        by=by,
        seq=version.seq,
        elapsed_ms=_ms(started),
    )
    return _answer(updated, version, events)
