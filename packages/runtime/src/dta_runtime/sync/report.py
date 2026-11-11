"""What one run of the sync pipeline did, in numbers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dta_core.store.model import iso_utc

SUCCEEDED = "succeeded"
FAILED = "failed"
BACKFILL = "backfill"
INCREMENTAL = "incremental"


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
    status: str = "ok"
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    # Rows read from each dataset of the source in this run.
    datasets: dict[str, int] = field(default_factory=dict)
    watermark: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "datasets": dict(self.datasets),
            "watermark": iso_utc(self.watermark) if self.watermark else None,
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
            "sources": {name: count.as_dict() for name, count in self.sources.items()},
            "mappings": {name: count.as_dict() for name, count in self.mappings.items()},
            "rejects_location": self.rejects_location,
            "watermark": iso_utc(self.watermark) if self.watermark else None,
            "unmapped_columns": list(self.unmapped_columns),
            "duration_ms": self.duration_ms,
            "resumed_run": self.resumed_run,
            "error": dict(self.error) if self.error else None,
        }
