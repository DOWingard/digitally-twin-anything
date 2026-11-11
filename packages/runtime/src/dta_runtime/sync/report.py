"""What one run of the sync pipeline did, in numbers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dta_core.store import VersionRef
from dta_core.store.model import iso_utc

SUCCEEDED = "succeeded"
# Some sources failed; the others were synced.
PARTIAL = "partial"
FAILED = "failed"
BACKFILL = "backfill"
INCREMENTAL = "incremental"
REPLAY = "replay"
OK = "ok"
FAILING = "failing"


def _time(value: datetime | None) -> str | None:
    return iso_utc(value) if value is not None else None


@dataclass
class MappingCount:
    """Counts of one mapping.

    `rows_read` and the rows set aside while mapping count source rows.
    `rows_written`, and the rows set aside when a version was refused, count
    rows of the versions written: one per object, or per object and interval
    of a time series.
    """

    object_type: str
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    rows_deleted: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "rows_deleted": self.rows_deleted,
        }


@dataclass
class SourceCount:
    status: str = OK
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    # Rows that arrived behind the cursor of their series: counted, not written.
    rows_late: int = 0
    # Rows read from each dataset of the source in this run.
    datasets: dict[str, int] = field(default_factory=dict)
    watermark: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "rows_late": self.rows_late,
            "datasets": dict(self.datasets),
            "watermark": _time(self.watermark),
        }


@dataclass
class LoadReport:
    # uid of the run's `TwinSyncRun`; the object exists only when the run wrote a version.
    run: str
    status: str
    kind: str
    versions_written: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    rows_late: int = 0
    sources: dict[str, SourceCount] = field(default_factory=dict)
    # Keyed `<source>.<mapping>`.
    mappings: dict[str, MappingCount] = field(default_factory=dict)
    # Relative to the artifacts directory; None when no row was set aside.
    rejects_location: str | None = None
    watermark: datetime | None = None
    unmapped_columns: list[str] = field(default_factory=list)
    duration_ms: int = 0
    # uid of an earlier run that stopped half-way and that this run continued after.
    resumed_run: str | None = None
    error: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "status": self.status,
            "kind": self.kind,
            "versions_written": self.versions_written,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "rows_late": self.rows_late,
            "sources": {name: count.as_dict() for name, count in self.sources.items()},
            "mappings": {name: count.as_dict() for name, count in self.mappings.items()},
            "rejects_location": self.rejects_location,
            "watermark": _time(self.watermark),
            "unmapped_columns": list(self.unmapped_columns),
            "duration_ms": self.duration_ms,
            "resumed_run": self.resumed_run,
            "error": dict(self.error) if self.error else None,
        }


@dataclass
class DatasetRun:
    """One dataset or view of a source, as a run left it."""

    # Rows the run read from it; 0 when the source could not be read.
    rows_read: int = 0
    cursor_column: str | None = None
    # How far it has been read, as it is stored: a time or a whole number.
    cursor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "cursor_column": self.cursor_column,
            "cursor": self.cursor,
        }


@dataclass
class SourceRun:
    """One source in a run that repeats."""

    status: str = OK
    # Code and message of what kept the source from being synced; None when it was.
    error: dict[str, str] | None = None
    # Latest valid time taken from the source by this run or an earlier one;
    # for a source that is only read whole, the time of its last successful read.
    watermark: datetime | None = None
    # `now` less the watermark, in seconds; None while nothing has been taken.
    freshness_s: float | None = None
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    rows_late: int = 0
    datasets: dict[str, DatasetRun] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": dict(self.error) if self.error else None,
            "watermark": _time(self.watermark),
            "freshness_s": self.freshness_s,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "rows_late": self.rows_late,
            "datasets": {name: found.as_dict() for name, found in self.datasets.items()},
        }


@dataclass
class RunReport:
    """What a run that repeats did. Returned whether it succeeded or not."""

    # uid of the run's `TwinSyncRun`.
    run: str
    kind: str
    # The time the run was given by its clock.
    now: datetime
    status: str
    # Code and message; None unless the run failed.
    error: dict[str, str] | None = None
    versions_written: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    # The head of the twin after the run.
    head: VersionRef | None = None
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    rows_late: int = 0
    # Relative to the artifacts directory; None when no row was set aside.
    rejects_location: str | None = None
    # Latest valid time the run took from any source.
    watermark: datetime | None = None
    duration_ms: int = 0
    unmapped_columns: list[str] = field(default_factory=list)
    sources: dict[str, SourceRun] = field(default_factory=dict)
    # Keyed `<source>.<mapping>`.
    mappings: dict[str, MappingCount] = field(default_factory=dict)
    # uid of an earlier run that stopped half-way and that this run continued after.
    resumed_run: str | None = None

    def as_dict(self) -> dict[str, Any]:
        head = self.head
        return {
            "run": self.run,
            "kind": self.kind,
            "now": iso_utc(self.now),
            "status": self.status,
            "error": dict(self.error) if self.error else None,
            "versions_written": self.versions_written,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "head": {"id": head.id, "seq": head.seq, "t": iso_utc(head.t)} if head else None,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "rows_late": self.rows_late,
            "rejects_location": self.rejects_location,
            "watermark": _time(self.watermark),
            "duration_ms": self.duration_ms,
            "unmapped_columns": list(self.unmapped_columns),
            "sources": {name: found.as_dict() for name, found in self.sources.items()},
            "mappings": {name: count.as_dict() for name, count in self.mappings.items()},
            "resumed_run": self.resumed_run,
        }
