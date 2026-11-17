"""The records this package keeps on the live line, the names they go by, and how
they are written.

Action types (`TwinAction`) and control loops (`TwinControlLoop`) are registered from
the manifest; every proposal is a `TwinActionRecord`. All are objects of the graph,
so the twin's history shows what was proposed, decided, sent and observed. A write
is one version of the live line at the time of its head: deciding or sending an
action brings no newer data, and a version later than the head would refuse a sync
that is still writing the intervals before it. The data time a record speaks of is
in its own fields (`proposed_at`, `effective_at`, `reconciled_at`).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from dta_core.log import Log
from dta_core.schema import FactKind
from dta_core.store import (
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    Provenance,
    TimeRegression,
    VersionRef,
)
from dta_core.store.model import iso_utc
from pydantic import TypeAdapter

from ..twin import Twin

log = Log("runtime.actions")

ACTION_TYPE = "TwinAction"
RECORD_TYPE = "TwinActionRecord"
LOOP_TYPE = "TwinControlLoop"
FUNCTION_TYPE = "TwinFunction"
ACTION_SCOPE = "twin_action"
RECORD_SCOPE = "twin_action_record"
LOOP_SCOPE = "twin_control_loop"
FUNCTION_SCOPE = "twin_function"
# Recorded as the actor of every version this package writes, and as the origin of
# the values it writes.
ACTOR = "actions"
MANIFEST = "manifest"
ACTIVE = "active"
DISABLED = "disabled"

# What became of a proposal.
PROPOSED = "proposed"
REJECTED = "rejected"
APPROVED = "approved"
BLOCKED = "blocked"
EXECUTED = "executed"
FAILED = "failed"
# Whether an executed action had its effect.
PENDING = "pending"
OK = "ok"
MISMATCH = "mismatch"

# The twin's fidelity states, as they are handed in.
UNKNOWN = "unknown"
HEALTHY = "healthy"
CORRECTING = "correcting"
FAILED_STATE = "failed"
STATES = (UNKNOWN, HEALTHY, CORRECTING, FAILED_STATE)

# Autonomies and bindings of an action type.
ADVISORY = "advisory"
SUPERVISED = "supervised"
AUTONOMOUS = "autonomous"
EXECUTOR = "executor"

# A record named by a caller's key gets this many hexadecimal characters of its hash.
ID_LENGTH = 32
_DURATION = TypeAdapter(timedelta)


def scoped(scope: str, name: str) -> str:
    """The uid of an object of a scope, from its id or its uid."""
    return name if name.startswith(f"{scope}:") else f"{scope}:{name}"


def action_uid(action_id: str) -> str:
    return scoped(ACTION_SCOPE, action_id)


def loop_uid(loop_id: str) -> str:
    return scoped(LOOP_SCOPE, loop_id)


def function_uid(function_id: str) -> str:
    return scoped(FUNCTION_SCOPE, function_id)


def record_uid(record_id: str) -> str:
    return scoped(RECORD_SCOPE, record_id)


def bare(uid: str | None) -> str | None:
    """A uid without its scope."""
    return None if uid is None else uid.split(":", 1)[-1]


def keyed_id(key: str) -> str:
    """The id of the record a caller's key names: the same key, the same record."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:ID_LENGTH]


def new_id() -> str:
    return uuid.uuid4().hex


def duration_text(value: timedelta | None) -> str | None:
    return None if value is None else str(_DURATION.dump_python(value, mode="json"))


def duration_of(text: str | None) -> timedelta | None:
    return None if not text else _DURATION.validate_python(text)


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no JSON form")


def canonical(document: Any) -> str:
    """Sorted keys, no spaces, times as ISO 8601 UTC."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_plain
    )


def from_json(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        log.warn("a record holds text that is not JSON; it is given as it is")
        return text


def utc(value: Any) -> Any:
    """A time as UTC, whatever zone object a read gave it; anything else as it is."""
    return value.astimezone(UTC) if isinstance(value, datetime) else value


# -- documents ---------------------------------------------------------------------------


def record_document(uid: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """A `TwinActionRecord` as it is handed out: its JSON fields parsed, its action by id."""
    return {
        "id": bare(uid),
        "uid": uid,
        "action": bare(values.get("action")),
        "object": values.get("object"),
        "params": from_json(values.get("params"), {}),
        "reason": values.get("reason"),
        "source": values.get("source"),
        "proposed_at": utc(values.get("proposed_at")),
        "at_version": values.get("at_version"),
        "fidelity": values.get("fidelity"),
        "status": values.get("status"),
        "reasons": from_json(values.get("reasons"), []),
        "approval": from_json(values.get("approval")),
        "execution": from_json(values.get("execution")),
        "effective_at": utc(values.get("effective_at")),
        "reconciliation": values.get("reconciliation"),
        "observed": from_json(values.get("observed")),
        "reconciled_at": utc(values.get("reconciled_at")),
    }


def get(twin: Twin, uid: str, type_name: str, at: Any = "head") -> dict[str, Any] | None:
    """The values of an object of a type at a version of the live line, with its uid;
    None when there is none."""
    found = twin.store.get(uid, at=at, branch=LIVE_BRANCH)
    if found is None or found.type != type_name:
        return None
    return {"uid": found.uid, **found.values}


# -- writing -----------------------------------------------------------------------------


def write(
    twin: Twin,
    upserts: Mapping[str, list[dict[str, Any]]],
    *,
    now: datetime,
    cause: str = "action",
    note: str = "",
    extra: Mapping[str, Any] | None = None,
) -> VersionRef:
    """Write rows as one version of the live line at the time of its head.

    The values are true from `now`; a value cannot be recorded as true later than the
    version that records it, so their valid time is the earlier of the two.
    """
    store = twin.store
    for attempt in (1, 2):
        head = store.head()
        changes = ChangeSet(
            upserts={name: list(rows) for name, rows in upserts.items() if rows},
            provenance=Provenance(source=ACTOR, kind=FactKind.inferred, t_valid=min(now, head.t)),
        )
        meta = CommitMeta(cause=cause, actor=ACTOR, t=head.t, note=note, extra=dict(extra or {}))
        try:
            return store.commit(changes, meta)
        except TimeRegression as exc:
            # A sync wrote a later version between reading the head and committing.
            if attempt == 2:
                log.error(
                    "records could not be written: the live line keeps moving",
                    exc=exc,
                    note=note,
                )
                raise
            log.warn(
                "the live line moved on while records were being written; writing them "
                "again at the new head",
                note=note,
                attempt=attempt,
            )
    raise AssertionError("unreachable")


class Locks:
    """One lock per name, held while a record or a loop is judged, written and sent, so
    that two requests about the same one in this process cannot both act on it."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        with self._guard:
            lock, users = self._held.get(name, (threading.Lock(), 0))
            self._held[name] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._guard:
                lock, users = self._held[name]
                if users == 1:
                    del self._held[name]
                else:
                    self._held[name] = (lock, users - 1)


LOCKS = Locks()
