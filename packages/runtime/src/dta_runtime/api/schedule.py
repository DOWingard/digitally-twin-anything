"""What the scheduler is to ask the API for, and how often.

The schedule is built from providers the services hold: each looks at the head and
gives the entries it is responsible for. The scheduler knows nothing of the twin: it
asks for each entry once per boundary of the entry's interval on the twin's clock,
sends `key` with `{boundary}` filled in as its Idempotency-Key, and asks for the
entries that are not a sync only once the sync run of the newest sync boundary has
ended. A part of the twin that needs a loop of its own adds a provider; nothing here
changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from dta_core.log import Log
from pydantic import TypeAdapter

from .. import actions
from ..actions.records import ACTIVE, EXECUTOR
from ..fidelity.records import TYPES as FIDELITY_TYPES
from . import registry
from .docs import API_PREFIX
from .live import HeadState

if TYPE_CHECKING:
    from .services import Services

log = Log("api.schedule")

SYNC_ENTRY = "sync"
FIDELITY_ENTRY = "fidelity"
RECONCILE_ENTRY = "reconcile"
BOUNDARY = "{boundary}"
_DURATION = TypeAdapter(timedelta)


@dataclass(frozen=True)
class ScheduleEntry:
    """One thing asked for at every boundary of `interval` on the twin's clock."""

    id: str
    # `sync` entries come first: the others wait for the sync run of the newest boundary.
    kind: str
    interval: timedelta
    path: str
    # The Idempotency-Key, with `{boundary}` where the boundary goes.
    key: str
    method: str = "POST"
    body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError(f"the interval of schedule entry {self.id} is not positive")
        if not self.path.startswith(f"{API_PREFIX}/"):
            raise ValueError(f"schedule entry {self.id} names a path outside the API")
        if BOUNDARY not in self.key:
            raise ValueError(f"the key of schedule entry {self.id} has no {BOUNDARY}")

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "interval": registry.duration_text(self.interval),
            "method": self.method,
            "path": self.path,
            "key": self.key,
            "body": dict(self.body),
        }


Provider = Callable[["Services", HeadState], list[ScheduleEntry]]


def sync_entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """A live twin's sync run, once per sync interval."""
    if not state.live or state.interval is None:
        return []
    return [
        ScheduleEntry(
            id=SYNC_ENTRY,
            kind="sync",
            interval=state.interval,
            path=f"{API_PREFIX}/sync/runs",
            key=f"sync:{BOUNDARY}",
        )
    ]


def fidelity_entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """A live twin's fidelity check, once per fidelity interval, when the manifest sets
    one and the twin's ontology holds the fidelity records."""
    fidelity = state.manifest.fidelity
    if not state.live or fidelity is None or fidelity.interval is None or fidelity.window is None:
        return []
    if not all(state.ontology.has_type(name) for name in FIDELITY_TYPES):
        log.debug("no fidelity check is scheduled: the ontology lacks the fidelity records")
        return []
    return [
        ScheduleEntry(
            id=FIDELITY_ENTRY,
            kind="fidelity",
            interval=fidelity.interval,
            path=f"{API_PREFIX}/fidelity/checks",
            key=f"fidelity:{BOUNDARY}",
        )
    ]


def _duration(text: Any, what: str) -> timedelta | None:
    """A stored ISO 8601 duration; None, logged, when it cannot be read or is not positive."""
    if not text:
        return None
    try:
        found = _DURATION.validate_python(text)
    except ValueError:
        log.error("a stored interval cannot be read; it is not scheduled", what=what)
        return None
    if found <= timedelta(0):
        log.error("a stored interval is not positive; it is not scheduled", what=what)
        return None
    return found


def reconcile_entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """Reconciliation of executed actions, once per sync interval, when the API makes
    external calls and the twin has an action bound to an executor."""
    if not state.live or state.interval is None or services.executor is None:
        return []
    bound = [
        found
        for found in actions.types(services.twin, at=state.version)
        if found["binding"] == EXECUTOR and found["status"] == ACTIVE
    ]
    if not bound:
        return []
    return [
        ScheduleEntry(
            id=RECONCILE_ENTRY,
            kind="reconcile",
            interval=state.interval,
            path=f"{API_PREFIX}/actions/reconcile",
            key=f"reconcile:{BOUNDARY}",
        )
    ]


def loop_entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """Every enabled control loop, once per its interval."""
    if not state.live:
        return []
    found: list[ScheduleEntry] = []
    for loop in actions.loops(services.twin, at=state.version):
        if not loop["enabled"] or loop["status"] != ACTIVE:
            continue
        interval = _duration(loop["interval"], loop["uid"])
        if interval is None:
            continue
        found.append(
            ScheduleEntry(
                id=f"loop:{loop['id']}",
                kind="loop",
                interval=interval,
                path=f"{API_PREFIX}/control/loops/{loop['id']}/run",
                key=f"loop:{loop['id']}:{BOUNDARY}",
            )
        )
    return found


def function_entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """Every active function whose trigger is `interval`, once per its interval."""
    if not state.live:
        return []
    found: list[ScheduleEntry] = []
    for record in registry.rows(
        services, registry.FUNCTION_TYPE, state.version, [("trigger", "=", "interval")]
    ):
        if record.get("status") == "disabled":
            continue
        interval = _duration(record.get("interval"), record["uid"])
        if interval is None:
            log.warn("a function triggered on an interval states none", function=record["uid"])
            continue
        function_id = registry.bare(record["uid"])
        found.append(
            ScheduleEntry(
                id=f"function:{function_id}",
                kind="function",
                interval=interval,
                path=f"{API_PREFIX}/functions/{function_id}/run",
                key=f"function:{function_id}:{BOUNDARY}",
            )
        )
    return found


DEFAULT_PROVIDERS: tuple[Provider, ...] = (
    sync_entries,
    fidelity_entries,
    reconcile_entries,
    loop_entries,
    function_entries,
)


def entries(services: Services, state: HeadState) -> list[ScheduleEntry]:
    """Every provider's entries, sync entries first. A provider that fails is left out
    and logged: the others are still asked for."""
    found: list[ScheduleEntry] = []
    seen: set[str] = set()
    for provider in services.schedule:
        name = getattr(provider, "__name__", type(provider).__name__)
        try:
            given = provider(services, state)
        except Exception as exc:
            log.error(
                "a schedule provider failed; its entries are left out", exc=exc, provider=name
            )
            continue
        for entry in given:
            if entry.id in seen:
                log.error("two schedule entries share an id; the later is left out", id=entry.id)
                continue
            seen.add(entry.id)
            found.append(entry)
    return sorted(found, key=lambda entry: entry.kind != "sync")
