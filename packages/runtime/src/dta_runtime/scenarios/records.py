"""The record a scenario keeps of itself on the live line, and the names it goes by.

The record is an object of the graph like any other, so the twin's history shows
when a scenario was made, run and removed. Its versions are written the way the
twin's other registrations are: with cause `registration`, at the time of the live
head, since making or running a scenario brings no newer data.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from dta_core.log import Log
from dta_core.schema import FactKind
from dta_core.store import (
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    Provenance,
    StoredObject,
    TimeRegression,
    VersionRef,
)

from ..twin import Twin
from .errors import NOT_FOUND, ScenarioError, refused
from .request import duration_of

log = Log("runtime.scenarios")

TYPE = "TwinScenario"
MODEL_TYPE = "TwinModel"
SCOPE = "twin_scenario"
BRANCH_PREFIX = "scenario-"
# Recorded as the actor of every version this package writes, and as the origin of the
# record's values.
ACTOR = "scenarios"

CREATED = "created"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
DELETED = "deleted"

# Fields of the registration's version: the scenario it is about, and the step of its
# grid. The record has no property for the step, and the grid cannot be told without it.
EXTRA_SCENARIO = "scenario"
EXTRA_STATUS = "status"
EXTRA_STEP_MS = "step_ms"
_MILLISECOND = timedelta(milliseconds=1)


def scenario_uid(scenario_id: str) -> str:
    return f"{SCOPE}:{scenario_id}"


def branch_name(scenario_id: str) -> str:
    return f"{BRANCH_PREFIX}{scenario_id}"


def id_of(uid: str) -> str:
    return uid.split(":", 1)[1]


def document(found: StoredObject) -> dict[str, Any]:
    """The record as it is handed out: its uid, its properties, and its id."""
    return {"uid": found.uid, **found.values, "id": id_of(found.uid)}


def find(twin: Twin, uid: Any, at: Any = "head") -> dict[str, Any] | None:
    """The record at a version of the live line; None when there is none."""
    if not isinstance(uid, str) or not uid.startswith(f"{SCOPE}:"):
        return None
    found = twin.store.get(uid, at=at, branch=LIVE_BRANCH)
    if found is None or found.type != TYPE:
        return None
    return document(found)


def require(twin: Twin, uid: Any, at: Any = "head") -> dict[str, Any]:
    found = find(twin, uid, at)
    if found is None:
        log.warn("no such scenario", scenario=str(uid), at=str(at))
        raise refused(NOT_FOUND, f"no scenario {uid!r}", scenario=str(uid))
    return found


def write(
    twin: Twin, row: dict[str, Any], *, status: str, step: timedelta | None = None, note: str = ""
) -> VersionRef:
    """Write the record, or some of its properties, as one version of the live line at
    the time of its head."""
    store = twin.store
    extra: dict[str, Any] = {EXTRA_SCENARIO: row["uid"], EXTRA_STATUS: status}
    if step is not None:
        extra[EXTRA_STEP_MS] = step // _MILLISECOND
    for attempt in (1, 2):
        head = store.head()
        changes = ChangeSet(
            upserts={TYPE: [row]},
            provenance=Provenance(source=ACTOR, kind=FactKind.inferred, t_valid=head.t),
        )
        meta = CommitMeta(cause="registration", actor=ACTOR, t=head.t, note=note, extra=extra)
        try:
            return store.commit(changes, meta)
        except TimeRegression as exc:
            # A sync wrote a later version between reading the head and committing.
            if attempt == 2:
                log.error(
                    "the scenario's record could not be written: the live line keeps moving",
                    exc=exc,
                    scenario=row["uid"],
                    status=status,
                )
                raise
            log.warn(
                "the live line moved on while the scenario's record was being written; "
                "writing it again at the new head",
                scenario=row["uid"],
                status=status,
                attempt=attempt,
            )
    raise AssertionError("unreachable")


def set_status(twin: Twin, uid: str, status: str, error: str | None = None) -> VersionRef:
    version = write(twin, {"uid": uid, "status": status, "error": error}, status=status)
    log.debug("scenario status recorded", scenario=uid, status=status, seq=version.seq)
    return version


def step_of(twin: Twin, uid: str, record: dict[str, Any] | None = None) -> timedelta:
    """The step of the scenario's grid: the record's own, or, for a record written
    before it had one, what its latest registration noted."""
    kept = (record if record is not None else require(twin, uid)).get("step")
    if kept:
        return duration_of(kept)
    for version in twin.store.versions(
        cause="registration", actor=ACTOR, extra={EXTRA_SCENARIO: uid}, newest_first=True
    ):
        found = version.extra.get(EXTRA_STEP_MS)
        if isinstance(found, int) and not isinstance(found, bool) and found > 0:
            return found * _MILLISECOND
    log.error("the version that registered a scenario is not on the live line", scenario=uid)
    raise ScenarioError(
        f"the registration of {uid} is not on the live line, so its grid is not known",
        code=NOT_FOUND,
        detail={"scenario": uid},
    )
