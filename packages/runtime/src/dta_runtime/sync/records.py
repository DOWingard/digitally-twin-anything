"""What the twin records about its own syncing: runs, datasets read, sources.

These are objects of the graph like any other, so the history of the twin
covers its syncing too. Their times are data times: a run begins and ends at
the times of its first and last version. Only a run's duration is measured on
the wall clock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dta_core.log import Log
from dta_core.manifest.models import Source
from dta_core.schema import FactKind
from dta_core.store import Provenance, Store, Version, VersionRef
from dta_core.store.model import iso_utc

from ..twin import source_uid
from .errors import STORE_FAILED, SyncError
from .plan import RelationKey, dataset_uid
from .report import BACKFILL, FAILED, INCREMENTAL

log = Log("sync.records")

RUN_TYPE = "TwinSyncRun"
DATASET_TYPE = "TwinDataset"
SOURCE_TYPE = "TwinSource"
RUN_SCOPE = "twin_sync_run"
ACTOR = "sync"
# Recorded as the origin of everything the pipeline writes about itself.
ORIGIN = "sync"
INTERRUPTED = "sync.interrupted"


def run_uid(key: str) -> str:
    return f"{RUN_SCOPE}:{key}"


def run_key(first: datetime, trace_id: str, head_seq: int) -> str:
    """Names a run by the time of its first version and where it started.

    Nothing of the wall clock goes in, so loading the same data into a fresh
    twin under the same trace gives the same name.
    """
    digest = hashlib.sha256(f"{trace_id}:{head_seq}".encode()).hexdigest()[:8]
    return f"{first:%Y%m%dT%H%M%S}Z-{digest}"


def provenance(t: datetime) -> Provenance:
    return Provenance(source=ORIGIN, kind=FactKind.inferred, t_valid=t)


@dataclass(frozen=True)
class Interrupted:
    """An earlier run whose versions are in the store but which never recorded its end."""

    uid: str
    started_at: datetime
    finished_at: datetime
    versions: int
    backfill: bool


@dataclass
class State:
    """What the store held about syncing when the run began."""

    head: VersionRef
    # uid -> stored row
    datasets: dict[str, dict[str, Any]]
    sources: dict[str, dict[str, Any]]
    interrupted: Interrupted | None = None

    def cursor(self, key: RelationKey) -> datetime | None:
        """Latest valid time already read from a dataset or view."""
        row = self.datasets.get(dataset_uid(*key))
        text = row.get("cursor") if row else None
        if not text:
            return None
        try:
            found = datetime.fromisoformat(text)
        except ValueError as exc:
            log.error("stored cursor cannot be read", exc=exc, dataset=dataset_uid(*key))
            raise SyncError(
                f"the cursor stored for {key[0]}.{key[1]} is not a time; reading on from it "
                "could load history twice",
                code=STORE_FAILED,
                detail={"dataset": dataset_uid(*key)},
            ) from exc
        if found.tzinfo is None:
            raise SyncError(
                f"the cursor stored for {key[0]}.{key[1]} has no time zone",
                code=STORE_FAILED,
                detail={"dataset": dataset_uid(*key)},
            )
        return found


def _interrupted(store: Store) -> Interrupted | None:
    latest: list[Version] = store.versions(cause="sync", actor=ACTOR, newest_first=True, limit=1)
    if not latest or not latest[0].run or not latest[0].run.startswith(f"{RUN_SCOPE}:"):
        return None
    uid = latest[0].run
    if store.get(uid) is not None:
        return None
    versions = store.versions(run=uid)
    log.warn(
        "an earlier run stopped before it finished; its versions are kept and this run "
        "continues after them",
        run=uid,
        versions=len(versions),
        first_seq=versions[0].seq,
        last_seq=versions[-1].seq,
    )
    return Interrupted(
        uid=uid,
        started_at=versions[0].t,
        finished_at=versions[-1].t,
        versions=len(versions),
        backfill=bool(versions[0].extra.get("backfill")),
    )


def read_state(store: Store) -> State:
    head = store.head()
    datasets = {
        row["uid"]: row
        for row in store.table(
            DATASET_TYPE, columns=["cursor_column", "cursor", "rows_read"], at=head
        ).to_pylist()
    }
    sources = {
        row["uid"]: row
        for row in store.table(SOURCE_TYPE, columns=["watermark", "status"], at=head).to_pylist()
    }
    return State(head, datasets, sources, _interrupted(store))


# -- rows --------------------------------------------------------------------------


def interrupted_row(found: Interrupted) -> dict[str, Any]:
    return {
        "uid": found.uid,
        "kind": BACKFILL if found.backfill else INCREMENTAL,
        "status": FAILED,
        "started_at": found.started_at,
        "finished_at": found.finished_at,
        "versions_written": found.versions,
        "error": f"{INTERRUPTED}: the run stopped before it finished; the versions it wrote "
        "are kept and the next run continued after them",
    }


def dataset_row(
    state: State,
    key: RelationKey,
    *,
    cursor_column: str | None,
    cursor: datetime | None,
    rows_read: int | None = None,
    read_at: datetime | None = None,
) -> dict[str, Any]:
    """The row of one dataset or view. Fields left out keep what is stored."""
    uid = dataset_uid(*key)
    row: dict[str, Any] = {"uid": uid}
    stored = state.datasets.get(uid)
    if stored is None:
        row["source"] = source_uid(key[0])
        row["name"] = key[1]
    if cursor_column is not None and (stored or {}).get("cursor_column") != cursor_column:
        row["cursor_column"] = cursor_column
    if cursor is not None:
        row["cursor"] = iso_utc(cursor)
    if rows_read is not None:
        row["rows_read"] = int((stored or {}).get("rows_read") or 0) + rows_read
    if read_at is not None:
        row["last_read_at"] = read_at
    return row


def source_row(
    state: State,
    source_id: str,
    source: Source,
    *,
    failing: str | None,
    watermark: datetime | None,
    t: datetime,
) -> dict[str, Any]:
    uid = source_uid(source_id)
    row: dict[str, Any] = {"uid": uid}
    stored = state.sources.get(uid)
    if stored is None:
        # Normally written when the manifest was registered; a twin without the
        # object still gets a complete one.
        row["connector"] = source.connector.value if source.connector else "custom"
        row["description"] = source.description
        row["location"] = source.location
    if failing is not None:
        row["status"] = "failing"
        row["last_error"] = failing
        return row
    row["status"] = "ok"
    row["last_error"] = None
    row["last_success_at"] = t
    known = (stored or {}).get("watermark")
    if watermark is not None and (known is None or watermark > known):
        row["watermark"] = watermark
    return row


def merge_rows(*groups: Mapping[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Rows by type with one row per uid: a later group's fields win over an earlier one's."""
    merged: dict[str, dict[str, dict[str, Any]]] = {}
    for group in groups:
        for type_name, rows in group.items():
            by_uid = merged.setdefault(type_name, {})
            for row in rows:
                by_uid.setdefault(row["uid"], {}).update(row)
    return {type_name: list(by_uid.values()) for type_name, by_uid in merged.items() if by_uid}
