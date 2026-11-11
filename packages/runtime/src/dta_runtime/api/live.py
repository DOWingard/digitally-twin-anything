"""What the routes of a live twin share: its clock, and what is known at each head version.

Everything here that depends only on a version (the manifest, the sources and
their datasets, the last sync run) is read once per version: a version never
changes. Only the clock is asked on every request.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks import ManifestView
from dta_core.ontology import type_signature
from dta_core.schema import Ontology
from dta_core.store import VersionRef
from pydantic import TypeAdapter

from ..clock import Clock, clock_for
from ..sync.records import ACTOR, DATASET_TYPE, RUN_SCOPE, RUN_TYPE, SOURCE_TYPE
from ..twin import Twin, source_uid
from .jobs import ACTIVE
from .tables import iso

log = Log("api.live")

# A clock that cannot be read is reported at most this often, in seconds on the wall:
# health is polled every second or so.
CLOCK_WARN_EVERY_S = 30.0
DISABLED = "disabled"
SOURCE_COLUMNS = [
    "connector",
    "description",
    "watermark",
    "status",
    "last_success_at",
    "last_error",
]
DATASET_COLUMNS = ["source", "name", "cursor_column", "cursor", "rows_read", "last_read_at"]
LAST_RUN_FIELDS = (
    "status",
    "kind",
    "started_at",
    "finished_at",
    "clock_at",
    "versions_written",
    "rows_written",
    "rows_rejected",
    "rows_late",
    "error",
)
_DURATION = TypeAdapter(timedelta)


def duration_text(value: timedelta | None) -> str | None:
    """ISO 8601, as the manifest is written: `PT15M`."""
    return None if value is None else str(_DURATION.dump_python(value, mode="json"))


def freshness(now: datetime | None, watermark: datetime | None) -> float | None:
    """Seconds from the latest data taken to the clock's now; None when either is unknown."""
    if now is None or watermark is None:
        return None
    return (now - watermark).total_seconds()


# -- the clock ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClockReading:
    kind: str
    available: bool
    now: datetime | None

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "available": self.available,
            "now": iso(self.now) if self.now is not None else None,
        }


class Clocks:
    """The twin's clock, built from the manifest in force on first use and rebuilt
    whenever the manifest changes. A clock given at start is used as it is."""

    def __init__(self, given: Clock | None, env: Mapping[str, str]) -> None:
        self._given = given
        self._env = env
        self._lock = threading.Lock()
        self._built: Clock | None = None
        self._failed: DtaError | None = None
        self._hash: str | None = None
        self._warned_at: float | None = None

    def get(self, manifest: Manifest, manifest_hash: str) -> Clock:
        """The clock `runtime.clock` names; raises the `ConfigError` of a clock that
        cannot be set up, such as one naming a source the manifest does not have."""
        if self._given is not None:
            return self._given
        with self._lock:
            if self._hash == manifest_hash:
                known = self._failed
                if known is not None:
                    raise DtaError(known.message, code=known.code, detail=dict(known.detail))
                if self._built is not None:
                    return self._built
            retired, self._built, self._failed = self._built, None, None
            self._hash = manifest_hash
            try:
                # Opens nothing: a source's clock opens its source on first use.
                clock = clock_for(manifest, self._env)
            except DtaError as exc:
                # Kept, so that a twin polled every second does not log this every second.
                self._failed = exc
                failure: DtaError | None = exc
            else:
                self._built, failure = clock, None
        if retired is not None:
            self._close(retired)
        if failure is not None:
            log.error(
                "the twin's clock cannot be set up",
                code=failure.code,
                clock=manifest.runtime.clock,
                manifest_hash=manifest_hash,
            )
            raise failure
        log.info("the twin's clock is set up", kind=clock.kind, manifest_hash=manifest_hash)
        return clock

    def read(self, manifest: Manifest, manifest_hash: str) -> ClockReading:
        """What the clock tells now. Never raises: a clock that cannot be read is unavailable."""
        kind = manifest.runtime.clock
        try:
            clock = self.get(manifest, manifest_hash)
            kind = clock.kind
            now = clock.now()
        except DtaError as exc:
            self._unavailable(kind, exc.code, None)
            return ClockReading(kind, False, None)
        except Exception as exc:
            self._unavailable(kind, "clock.unexpected", type(exc).__name__)
            return ClockReading(kind, False, None)
        if now.tzinfo is None or now.utcoffset() is None:
            self._unavailable(kind, "clock.no_offset", None)
            return ClockReading(kind, False, None)
        return ClockReading(kind, True, now.astimezone(UTC))

    def _unavailable(self, kind: str, code: str, error_type: str | None) -> None:
        moment = time.monotonic()
        with self._lock:
            due = self._warned_at is None or moment - self._warned_at >= CLOCK_WARN_EVERY_S
            if due:
                self._warned_at = moment
        fields = {"clock": kind, "code": code, "error_type": error_type}
        if due:
            log.warn(
                "the twin's clock cannot be read; it is reported unavailable",
                every_s=CLOCK_WARN_EVERY_S,
                **fields,
            )
        else:
            log.debug("the twin's clock still cannot be read", **fields)

    def close(self) -> None:
        with self._lock:
            built, self._built, self._hash, self._failed = self._built, None, None, None
        if built is not None:
            self._close(built)

    @staticmethod
    def _close(clock: Clock) -> None:
        try:
            clock.close()
        except Exception as exc:
            log.warn(
                "the twin's former clock could not be closed",
                kind=getattr(clock, "kind", None),
                error_type=type(exc).__name__,
            )


# -- what is known at a head version --------------------------------------------------


@dataclass(frozen=True)
class SourceState:
    """One source as of a version: what the manifest says of it and what syncing recorded."""

    id: str
    connector: str | None
    description: str | None
    status: str | None
    last_error: str | None
    watermark: datetime | None
    last_success_at: datetime | None
    in_manifest: bool
    datasets: tuple[dict[str, Any], ...]
    mappings: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class QuestionLimit:
    """How old the data a question is answered from may be, and where that data comes from."""

    id: str
    staleness: timedelta
    # Sources that map one of the question's targets; every source when none is known.
    sources: tuple[str, ...]


@dataclass(frozen=True)
class HeadState:
    version: VersionRef
    record: Mapping[str, Any]
    manifest: Manifest
    manifest_hash: str
    ontology: Ontology
    type_signature: str
    interval: timedelta | None
    # The shortest staleness any question allows.
    staleness: timedelta | None
    # One per question that states a staleness, in the contract's order.
    limits: tuple[QuestionLimit, ...]
    # The manifest's sources in its order, then those it no longer names.
    sources: tuple[SourceState, ...]
    last_run: dict[str, Any] | None
    # Time of the oldest version a sync run wrote: no run started before it.
    first_sync_t: datetime | None

    @property
    def live(self) -> bool:
        return self.interval is not None


def _mapping_kind(ontology: Ontology, object_type: str | None, valid_time: str | None) -> str:
    if valid_time is None:
        return "static"
    if object_type and ontology.has_type(object_type) and ontology.type(object_type).event:
        return "event"
    return "state"


def _limits(manifest: Manifest) -> tuple[QuestionLimit, ...]:
    """What each question with a staleness depends on.

    A question is answered from the sources that map its targets, so a source
    that is slow by nature does not make a question stale that never reads it.
    A question whose targets lead to no source is held against all of them.
    """
    view = ManifestView(manifest)
    everything = tuple(view.sources)
    return tuple(
        QuestionLimit(qid, question.staleness, tuple(view.dependencies(question)) or everything)
        for qid, question in view.questions.items()
        if question.staleness is not None
    )


def _sources(
    twin: Twin, manifest: Manifest, ontology: Ontology, version: VersionRef
) -> tuple[SourceState, ...]:
    store = twin.store
    records: dict[str, dict[str, Any]] = {}
    if ontology.has_type(SOURCE_TYPE):
        # `location` is never read: it describes where credentials go.
        table = store.table(SOURCE_TYPE, columns=SOURCE_COLUMNS, at=version, ontology=ontology)
        records = {row["uid"]: row for row in table.to_pylist()}
    datasets: dict[str, list[dict[str, Any]]] = {}
    if ontology.has_type(DATASET_TYPE):
        table = store.table(DATASET_TYPE, columns=DATASET_COLUMNS, at=version, ontology=ontology)
        for row in table.to_pylist():
            datasets.setdefault(row["source"] or "", []).append(
                {
                    "name": row["name"],
                    "cursor_column": row["cursor_column"],
                    "cursor": row["cursor"],
                    "rows_read": row["rows_read"],
                    "last_read_at": row["last_read_at"],
                }
            )
    for found in datasets.values():
        found.sort(key=lambda dataset: str(dataset["name"]))

    declared = manifest.sources or {}
    states: list[SourceState] = []
    for source_id, source in declared.items():
        record = records.get(source_uid(source_id))
        mappings = tuple(
            {
                "id": mapping_id,
                "dataset": mapping.dataset,
                "object_type": mapping.object_type,
                "kind": _mapping_kind(ontology, mapping.object_type, mapping.valid_time),
                "properties": list(mapping.properties or {}),
            }
            for mapping_id, mapping in (source.mappings or {}).items()
        )
        manifest_connector = source.connector.value if source.connector else "custom"
        states.append(
            SourceState(
                id=source_id,
                connector=(record or {}).get("connector") or manifest_connector,
                description=(record or {}).get("description") or source.description,
                status=(record or {}).get("status"),
                last_error=(record or {}).get("last_error"),
                watermark=(record or {}).get("watermark"),
                last_success_at=(record or {}).get("last_success_at"),
                in_manifest=True,
                datasets=tuple(datasets.get(source_uid(source_id), ())),
                mappings=mappings,
            )
        )
    prefix = source_uid("")
    dropped = sorted(uid for uid in records if uid.removeprefix(prefix) not in declared)
    for uid in dropped:
        record = records[uid]
        states.append(
            SourceState(
                id=uid.removeprefix(prefix),
                connector=record.get("connector"),
                description=record.get("description"),
                status=DISABLED,
                last_error=record.get("last_error"),
                watermark=record.get("watermark"),
                last_success_at=record.get("last_success_at"),
                in_manifest=False,
                datasets=tuple(datasets.get(uid, ())),
                mappings=(),
            )
        )
    return tuple(states)


def _runs(
    twin: Twin, ontology: Ontology, version: VersionRef, live: bool
) -> tuple[dict[str, Any] | None, datetime | None]:
    """The last run that recorded itself, and the time of the oldest version a run wrote.

    Each is found with a read of one row, however long the twin's history is.
    """
    store = twin.store
    selector = f"#0..{version.id}"
    oldest = store.versions(selector, version.branch, cause="sync", actor=ACTOR, limit=1)
    first = oldest[0].t if oldest else None
    if not live or not oldest or not ontology.has_type(RUN_TYPE):
        return None, first
    newest = store.versions(
        selector, version.branch, cause="sync", actor=ACTOR, limit=1, newest_first=True
    )
    uid = newest[0].run if newest else None
    if uid and uid.startswith(f"{RUN_SCOPE}:"):
        stored = store.get(uid, at=version, ontology=ontology)
        if stored is not None and stored.type == RUN_TYPE:
            return {
                "uid": uid,
                **{name: stored.values.get(name) for name in LAST_RUN_FIELDS},
            }, first
    # The newest version belongs to a run that has no record: it is in progress or was
    # interrupted. The last one that finished is the newest record.
    table = store.table(
        RUN_TYPE,
        columns=list(LAST_RUN_FIELDS),
        order_by=[("started_at", "desc")],
        limit=1,
        at=version,
        ontology=ontology,
    )
    rows = table.to_pylist()
    if not rows:
        return None, first
    log.debug("the newest version's run has no record; the newest record is taken", run=uid)
    return dict(rows[0]), first


def describe_head(twin: Twin, ontology: Ontology, version: VersionRef) -> HeadState:
    """Everything the health and sync routes need that depends on the version alone."""
    started = time.perf_counter()
    record = twin.manifest_record(at=version, branch=version.branch)
    manifest = twin.manifest(at=version, branch=version.branch)
    interval = manifest.sync.interval if manifest.sync else None
    sources = _sources(twin, manifest, ontology, version)
    limits = _limits(manifest)
    last_run, first_sync_t = _runs(twin, ontology, version, interval is not None)
    state = HeadState(
        version=version,
        record=record,
        manifest=manifest,
        manifest_hash=record["manifest_hash"],
        ontology=ontology,
        type_signature=type_signature(ontology),
        interval=interval,
        staleness=min((limit.staleness for limit in limits), default=None),
        limits=limits,
        sources=sources,
        last_run=last_run,
        first_sync_t=first_sync_t,
    )
    log.debug(
        "head described",
        head_seq=version.seq,
        branch=version.branch,
        sources=len(sources),
        live=state.live,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return state


# -- documents ----------------------------------------------------------------------


def _time(value: datetime | None) -> str | None:
    return iso(value) if value is not None else None


def source_document(source: SourceState, now: datetime | None) -> dict[str, Any]:
    return {
        "id": source.id,
        "connector": source.connector,
        "description": source.description,
        "status": source.status,
        "last_error": source.last_error,
        "watermark": _time(source.watermark),
        "last_success_at": _time(source.last_success_at),
        "freshness_s": freshness(now, source.watermark),
        "datasets": [
            {**dataset, "last_read_at": _time(dataset["last_read_at"])}
            for dataset in source.datasets
        ],
        "mappings": [dict(mapping) for mapping in source.mappings],
    }


def _question_document(limit: QuestionLimit, ages: Mapping[str, float | None]) -> dict[str, Any]:
    """How old the data of one question is: that of the source it waits for longest."""
    known = [(age, source) for source in limit.sources if (age := ages.get(source)) is not None]
    age, behind = max(known) if known else (None, None)
    allowed = limit.staleness.total_seconds()
    return {
        "id": limit.id,
        "staleness": duration_text(limit.staleness),
        "freshness_s": age,
        "fresh": None if age is None else age <= allowed,
        "source": behind,
        "margin_s": None if age is None else allowed - age,
    }


def sync_document(
    state: HeadState, reading: ClockReading, job: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The `sync` member of the health document; None for a twin that is not live."""
    if not state.live:
        return None
    now = reading.now if reading.available else None
    sources = [
        {
            "id": source.id,
            "status": source.status,
            "watermark": _time(source.watermark),
            "freshness_s": freshness(now, source.watermark),
        }
        for source in state.sources
        if source.in_manifest and source.status != DISABLED
    ]
    ages = {source["id"]: source["freshness_s"] for source in sources}
    questions = [_question_document(limit, ages) for limit in state.limits]
    measured = [found for found in questions if found["fresh"] is not None]
    staleness = duration_text(state.staleness)
    if measured:
        # The question nearest its limit, or furthest beyond it, speaks for the twin.
        nearest = min(measured, key=lambda found: (found["margin_s"], found["id"]))
        worst, staleness = nearest["freshness_s"], nearest["staleness"]
        fresh: bool | None = all(found["fresh"] for found in measured)
    elif state.limits:
        worst, fresh = None, None
    else:
        # No question sets a limit: the age of the source furthest behind is still told.
        known = [age for age in ages.values() if age is not None]
        worst, fresh = (max(known) if known else None), None
    for found in questions:
        del found["margin_s"]
    last_run = None
    if state.last_run is not None:
        last_run = {
            name: _time(value) if isinstance(value, datetime) else value
            for name, value in state.last_run.items()
        }
    return {
        "interval": duration_text(state.interval),
        "staleness": staleness,
        "freshness_s": worst,
        "fresh": fresh,
        "questions": questions,
        "sources": sources,
        "last_run": last_run,
        # The job may have ended since it was found; then there is none in progress.
        "job": {"id": job["id"], "state": job["state"]}
        if job is not None and job["state"] in ACTIVE
        else None,
    }
