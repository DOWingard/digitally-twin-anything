"""What a write of fidelity's records means to those it notifies.

The events are worked out from the records before and after the version that wrote
them, so a check that is asked for again gives back the same events as when it was
made. Order: one `twin.fidelity.incident` per incident that was opened, changed or
resolved (breach, then stale, signal and reconcile, each by subject), each followed by
`twin.fidelity.remodel_requested` when it asked for a re-model; then one
`twin.fidelity.signal_unknown` per suspect signal whose value became unknown (by signal
uid); then `twin.fidelity.state` when the twin's state moved.

Data of the events:

    twin.fidelity.incident            {"action": opened | changed | resolved, "incident",
                                       "kind", "subject", "status", "previous_status",
                                       "rung", "attempt", "cycle", "next_cycle_at",
                                       "at", "check"}
    twin.fidelity.remodel_requested   {"incident", "question", "spec", "requested_at",
                                       "timeout_s", "check"}
    twin.fidelity.signal_unknown      {"signal", "property", "object", "reason", "since",
                                       "why", "message", "questions", "check"}: nothing is
                                       inferred for the signal any more and nothing that
                                       reads it computes; `questions` are those left
                                       without an answer
    twin.fidelity.state               {"from", "to", "since", "check"}

An incident counts as changed when its status, rung, cycle, awaited fit, re-model
request or pending correction moved, or, for a breach, its attempt or the instant its
next cycle is due; a stale incident
counting one more stale check, or any incident only noting the latest measurement,
does not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .ladder import REMODEL_REQUESTED, RESOLVED, RUNG_NAMES
from .records import BREACH, KINDS, iso, unknown_of
from .values import CorrectionRequest

STATE_EVENT = "twin.fidelity.state"
INCIDENT_EVENT = "twin.fidelity.incident"
REMODEL_EVENT = "twin.fidelity.remodel_requested"
UNKNOWN_EVENT = "twin.fidelity.signal_unknown"
OPENED, CHANGED, RESOLVED_ACTION = "opened", "changed", "resolved"

Pair = tuple[Mapping[str, Any] | None, Mapping[str, Any]]


def _pending_key(document: Mapping[str, Any] | None) -> str | None:
    if document is None:
        return None
    pending = (document.get("detail") or {}).get("pending")
    return pending.get("key") if isinstance(pending, Mapping) else None


def _notable(document: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = [
        document.get("status"),
        document.get("rung"),
        document.get("cycle"),
        document.get("awaiting"),
        document.get("requested_at"),
        _pending_key(document),
    ]
    if document.get("kind") == BREACH:
        fields += [document.get("attempt"), _next_cycle_at(document)]
    return tuple(fields)


def _next_cycle_at(document: Mapping[str, Any] | None) -> str | None:
    return ((document or {}).get("detail") or {}).get("next_cycle_at")


def _order(pair: Pair) -> tuple[int, str, str]:
    after = pair[1]
    kind = after.get("kind")
    return (KINDS.index(kind) if kind in KINDS else len(KINDS), str(after["subject"]), after["uid"])


def classify(pair: Pair) -> str | None:
    before, after = pair
    if before is None:
        return OPENED
    if after.get("status") == RESOLVED and before.get("status") != RESOLVED:
        return RESOLVED_ACTION
    if _notable(before) != _notable(after):
        return CHANGED
    return None


def incident_events(
    pairs: Sequence[Pair], *, check: str | None, at: datetime | None
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """The incident events in order, and the incidents by what happened to them."""
    events: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {OPENED: [], CHANGED: [], RESOLVED_ACTION: []}
    for before, after in sorted(pairs, key=_order):
        action = classify((before, after))
        if action is None:
            continue
        grouped[action].append(dict(after))
        events.append(
            {
                "type": INCIDENT_EVENT,
                "subject": after["uid"],
                "data": {
                    "action": action,
                    "incident": after["uid"],
                    "kind": after.get("kind"),
                    "subject": after.get("subject"),
                    "status": after.get("status"),
                    "previous_status": None if before is None else before.get("status"),
                    "rung": after.get("rung"),
                    "attempt": after.get("attempt"),
                    "cycle": after.get("cycle"),
                    "next_cycle_at": _next_cycle_at(after),
                    "at": iso(at),
                    "check": check,
                },
            }
        )
        if after.get("status") == REMODEL_REQUESTED and (
            before is None or before.get("status") != REMODEL_REQUESTED
        ):
            detail = after.get("detail") or {}
            events.append(
                {
                    "type": REMODEL_EVENT,
                    "subject": after["uid"],
                    "data": {
                        "incident": after["uid"],
                        "question": detail.get("question"),
                        "spec": detail.get("spec"),
                        "requested_at": iso(after.get("requested_at")),
                        "timeout_s": (detail.get("remodel") or {}).get("timeout_s"),
                        "check": check,
                    },
                }
            )
    return events, grouped


def unknown_events(pairs: Sequence[Pair], *, check: str | None) -> list[dict[str, Any]]:
    """One event per signal whose value became unknown, by signal uid: said once, when
    it does, however long it stays so."""
    events = []
    for before, after in sorted(pairs, key=lambda pair: pair[1]["uid"]):
        state = unknown_of(after)
        if state is None or (before is not None and unknown_of(before) is not None):
            continue
        events.append(
            {
                "type": UNKNOWN_EVENT,
                "subject": after["uid"],
                "data": {
                    "signal": after["uid"],
                    "property": after.get("property"),
                    "object": after.get("object"),
                    "reason": after.get("reason"),
                    "since": state.get("since"),
                    "why": state.get("why"),
                    "message": state.get("message"),
                    "questions": list(state.get("questions") or []),
                    "check": check,
                },
            }
        )
    return events


def state_event(
    before: str, after: str, *, since: datetime | None, check: str | None
) -> dict[str, Any] | None:
    if before == after:
        return None
    return {
        "type": STATE_EVENT,
        "subject": "twin_fidelity:state",
        "data": {"from": before, "to": after, "since": iso(since), "check": check},
    }


def corrections_of(pairs: Sequence[Pair]) -> list[CorrectionRequest]:
    """The corrections asked for by a write: a pending correction an incident did not
    have before it."""
    found = []
    for before, after in sorted(pairs, key=_order):
        key = _pending_key(after)
        if key is None or key == _pending_key(before):
            continue
        detail = after.get("detail") or {}
        pending = detail["pending"]
        found.append(
            CorrectionRequest(
                incident=after["uid"],
                question=str(detail.get("question")),
                spec=str(detail.get("spec")),
                rung=RUNG_NAMES[int(pending["rung_number"])],  # type: ignore[arg-type]
                cycle=int(pending["cycle"]),
                attempt=int(pending["attempt"]),
                key=key,
                warning=False,
            )
        )
    return found
