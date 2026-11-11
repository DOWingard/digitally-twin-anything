"""One run of the sync pipeline: plan, extract, map, compare, load, record.

Nothing that ends up in the graph comes from the wall clock except how long
the run took. Version times and valid times come from the data.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from dta_core.config import require_env
from dta_core.log import Log, current_trace_id, trace
from dta_core.manifest.models import Source
from dta_core.store import ChangeSet, CommitMeta, VersionRef

from ..settings import RuntimeSettings
from ..twin import Twin
from .errors import UNEXPECTED, AllRejected, SourceFailed, SyncError
from .extract import Connect, DatasetRead, SourceFailure, extract, required_env
from .loader import Loader, version_time
from .mapper import Prepared, prepare
from .plan import MappingPlan, Plan, RelationKey, build_plan, dataset_uid, unmapped_columns
from .records import (
    ACTOR,
    DATASET_TYPE,
    RUN_TYPE,
    SOURCE_TYPE,
    State,
    dataset_row,
    interrupted_row,
    merge_rows,
    provenance,
    read_state,
    run_key,
    run_uid,
    source_row,
)
from .rejects import RULE_DUPLICATE, RULE_TYPE, RULE_UID, RULE_VALID_TIME, Rejects
from .report import FAILED, SUCCEEDED, LoadReport, MappingCount, SourceCount

log = Log("sync")


def _default_connect() -> Connect:
    # Imported here so that the pipeline loads without the connectors and their
    # dependencies when a caller brings its own way of opening a source.
    try:
        from dta_core.connect import open_connector
    except ImportError as exc:
        log.error(
            "the connectors cannot be imported; nothing was read or written",
            exc=exc,
            module="dta_core.connect",
        )
        raise
    return open_connector


class _Run:
    def __init__(
        self,
        twin: Twin,
        settings: RuntimeSettings,
        env: Mapping[str, str],
        connect: Connect | None,
        trace_id: str,
    ) -> None:
        self.twin = twin
        self.store = twin.store
        self.settings = settings
        self.env = env
        self._connect = connect
        self.trace_id = trace_id
        self.clock = time.perf_counter()
        self.sources: Mapping[str, Source] = {}
        self.plan: Plan | None = None
        self.state: State | None = None
        self.reads: dict[RelationKey, DatasetRead] = {}
        self.prepared: Prepared | None = None
        self.loader: Loader | None = None
        self.rejects: Rejects | None = None
        self.key = ""
        self.uid = ""
        self.unmapped: list[str] = []
        # Versions written outside the loader: the record of a failure.
        self.extra_versions: list[VersionRef] = []
        self.failing: dict[str, str] = {}
        self.error: dict[str, str] | None = None
        self._stored: dict[RelationKey, datetime] | None = None
        # Cursors written by versions that are in place, and those of the version in flight.
        self._confirmed: dict[RelationKey, datetime] = {}
        self._proposed: tuple[int, dict[RelationKey, datetime]] | None = None

    # -- phases -------------------------------------------------------------------

    def _connector(self) -> Connect:
        """How a source is opened. Settled once there is something to read and
        before any of it is, so a run never stops half-way for want of it."""
        if self._connect is None:
            self._connect = _default_connect()
        return self._connect

    def _plan(self) -> Plan:
        started = time.perf_counter()
        manifest = self.twin.manifest()
        self.sources = manifest.sources or {}
        plan = build_plan(manifest, self.twin.ontology)
        names: dict[str, None] = {}
        for source_id in plan.source_ids():
            names.update(dict.fromkeys(required_env(self.sources[source_id])))
        # Before anything is read: a missing variable is named, never defaulted.
        require_env(names, self.env)
        self.plan = plan
        log.info(
            "plan made",
            mappings=len(plan.mappings),
            views=len(plan.views),
            datasets=len(plan.datasets),
            kind=plan.kind,
            aggregation=plan.aggregation,
            interval_s=plan.interval_us / 1e6 if plan.interval_us else None,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return plan

    def _stored_cursors(self) -> dict[RelationKey, datetime]:
        """How far each time series was read by earlier runs."""
        assert self.plan is not None and self.state is not None
        if self._stored is not None:
            return self._stored
        cursors = {}
        for key, column in self.plan.series_relations().items():
            found = self.state.cursor(key)
            if found is not None:
                cursors[key] = found
                stored = self.state.datasets[dataset_uid(*key)].get("cursor_column")
                if stored and stored != column:
                    log.warn(
                        "the valid-time column of a dataset changed; reading on from the "
                        "time already reached",
                        source=key[0],
                        dataset=key[1],
                        was=stored,
                        now=column,
                    )
        self._stored = cursors
        return cursors

    def _extract(self) -> None:
        assert self.plan is not None
        started = time.perf_counter()
        try:
            self.reads = extract(
                self.plan, self.sources, self.env, self._connector(), self._stored_cursors()
            )
        except SourceFailure as failure:
            where = failure.source_id + (f".{failure.dataset}" if failure.dataset else "")
            error = SourceFailed(
                f"source {where} cannot be read: {failure.message}",
                detail={
                    "source": failure.source_id,
                    "dataset": failure.dataset,
                    "cause": failure.code,
                },
            )
            self.failing[failure.source_id] = f"{failure.code}: {failure.message}"
            self._fail(error)
            error.detail["report"] = self._report(FAILED).as_dict()
            raise error from None
        log.info(
            "sources read",
            sources=len(self.plan.source_ids()),
            datasets=len(self.reads),
            rows=sum(read.table.num_rows for read in self.reads.values()),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    def _prepare(self) -> Prepared:
        assert self.plan is not None and self.state is not None
        self.prepared = prepare(
            self.plan, self.reads, self.store, self.state.head, self._stored_cursors()
        )
        self.unmapped = unmapped_columns(self.plan, self.prepared.columns)
        return self.prepared

    def _name(self, first: datetime) -> None:
        assert self.state is not None
        self.key = run_key(first, self.trace_id, self.state.head.seq)
        self.uid = run_uid(self.key)
        self.rejects = Rejects(self.settings.artifacts_dir, self.key, self.uid)

    # -- rows set aside while mapping ---------------------------------------------

    def _record_set_aside(self) -> None:
        assert self.prepared is not None and self.rejects is not None
        for index, table in self.prepared.set_aside.items():
            mapping = self.prepared.plan.mappings[index]
            reasons: list[tuple[str, str | None, str, str]] = [
                ("bk", None, RULE_UID, "the identity is missing, empty or too long for a uid")
            ]
            if mapping.timeseries:
                reasons.append(
                    ("bvt", None, RULE_VALID_TIME, f"{mapping.valid_time} holds no time")
                )
            for j, prop in enumerate(mapping.properties):
                kind = prop.field.kind.value + (" list" if prop.field.multivalued else "")
                reasons.append(
                    (f"b{j}", prop.name, RULE_TYPE, f"the value cannot be read as {kind}")
                )
            columns = table.to_pydict()
            for row in range(table.num_rows):
                broken = [reason for reason in reasons if columns[reason[0]][row]]
                first, rest = broken[0], broken[1:]
                self.rejects.add(
                    mapping=mapping.label,
                    source=mapping.source_id,
                    type_name=mapping.object_type.name,
                    uid=columns["uid"][row],
                    prop=first[1],
                    rule=first[2],
                    message=first[3],
                    t=columns["vt"][row],
                    also=[{"property": r[1], "rule": r[2], "message": r[3]} for r in rest],
                )
        for index, table in self.prepared.duplicates.items():
            mapping = self.prepared.plan.mappings[index]
            for row in table.to_pylist():
                self.rejects.add(
                    mapping=mapping.label,
                    source=mapping.source_id,
                    type_name=mapping.object_type.name,
                    uid=row["uid"],
                    prop=None,
                    rule=RULE_DUPLICATE,
                    message=f"{row['n']} rows that differ claim this identity",
                )
        self.rejects.flush()

    # -- counts -------------------------------------------------------------------

    def _dropped(self, index: int) -> int:
        return self.loader.dropped[index] if self.loader is not None else 0

    def _rejected_mappings(self) -> list[MappingPlan]:
        """Mappings that read rows and of which not one could be written."""
        if self.prepared is None:
            return []
        found = []
        for mapping in self.prepared.plan.mappings:
            tally = self.prepared.tallies[mapping.index]
            dropped = self._dropped(mapping.index)
            if tally.rows and tally.rejected + dropped and tally.candidates - dropped <= 0:
                found.append(mapping)
        return found

    def _rows_read(self) -> int:
        return sum(read.table.num_rows for read in self.reads.values())

    def _rows_rejected(self) -> int:
        if self.prepared is None:
            return 0
        total = sum(tally.rejected for tally in self.prepared.tallies.values())
        if self.loader is not None:
            total += sum(self.loader.dropped) + sum(self.loader.deletes_refused)
        return total

    def _versions(self) -> list[VersionRef]:
        return [*(self.loader.versions if self.loader else []), *self.extra_versions]

    def _watermark(self, source_id: str | None = None) -> datetime | None:
        if self.prepared is None:
            return None
        return max(
            (
                latest
                for key, latest in self.prepared.cursors.items()
                if source_id is None or key[0] == source_id
            ),
            default=None,
        )

    # -- what the run records about itself ------------------------------------------

    def _run_row(
        self, t: datetime, status: str, versions: int, rows_written: int
    ) -> dict[str, Any]:
        assert self.plan is not None and self.rejects is not None
        written = self._versions()
        return {
            "uid": self.uid,
            "kind": self.plan.kind,
            "status": status,
            "started_at": written[0].t if written else t,
            "finished_at": t,
            "duration_ms": int((time.perf_counter() - self.clock) * 1000.0),
            "versions_written": versions,
            "rejects_location": self.rejects.location if self.rejects.written else None,
            "unmapped_columns": list(self.unmapped),
            "watermark": self._watermark(),
            "sources": list(self.plan.source_ids()),
            "rows_read": self._rows_read(),
            "rows_written": rows_written,
            "rows_rejected": self._rows_rejected(),
            "error": f"{self.error['code']}: {self.error['message']}" if self.error else None,
            "trace_id": self.trace_id,
        }

    def _progress(self, end: datetime | None, last: bool) -> dict[str, list[dict[str, Any]]]:
        """How far each time series has been read once the version in flight is in place.

        Written with every version, not only the last: a run that stops
        half-way then leaves the twin knowing where to continue, and the rows
        already loaded are not loaded again.
        """
        assert self.prepared is not None and self.state is not None and self.loader is not None
        if self._proposed is not None and len(self.loader.versions) > self._proposed[0]:
            self._confirmed.update(self._proposed[1])
        proposed: dict[RelationKey, datetime] = {}
        rows = []
        columns = self.prepared.plan.series_relations()
        for key, latest in self.prepared.cursors.items():
            reached = latest if last or end is None else min(end, latest)
            known = self._confirmed.get(key) or self._stored_cursors().get(key)
            if known is None or reached > known:
                proposed[key] = reached
                rows.append(
                    dataset_row(self.state, key, cursor_column=columns[key], cursor=reached)
                )
        self._proposed = (len(self.loader.versions), proposed)
        return {DATASET_TYPE: rows}

    def _final(self, t: datetime) -> dict[str, list[dict[str, Any]]]:
        assert self.prepared is not None and self.state is not None and self.loader is not None
        plan = self.prepared.plan
        columns = plan.series_relations()
        datasets = [
            dataset_row(
                self.state,
                key,
                cursor_column=columns.get(key),
                cursor=None,
                rows_read=rows,
                read_at=t,
            )
            for key, rows in self.prepared.relation_rows.items()
            if key in self.reads or key in columns
        ]
        rejected = self._rejected_mappings()
        status = SUCCEEDED
        if rejected:
            status = FAILED
            self._note_rejected(rejected)
        sources = [
            source_row(
                self.state,
                source_id,
                self.sources[source_id],
                failing=self.failing.get(source_id),
                watermark=self._watermark(source_id),
                t=t,
            )
            for source_id in plan.source_ids()
        ]
        run = self._run_row(
            t, status, len(self._versions()) + 1, self.loader.rows_written + self.loader.pending
        )
        return {RUN_TYPE: [run], DATASET_TYPE: datasets, SOURCE_TYPE: sources}

    def _records(self, t: datetime, key: int | None, last: bool) -> dict[str, list[dict[str, Any]]]:
        assert self.state is not None and self.loader is not None
        groups = []
        if self.state.interrupted is not None and not self._versions():
            groups.append({RUN_TYPE: [interrupted_row(self.state.interrupted)]})
        groups.append(self._progress(self.loader.interval_end(key), last))
        if last:
            groups.append(self._final(t))
        return merge_rows(*groups)

    def _note_rejected(self, mappings: list[MappingPlan]) -> AllRejected:
        labels = [mapping.label for mapping in mappings]
        message = f"every row of {', '.join(labels)} was set aside"
        self.error = {"code": AllRejected.code, "message": message}
        for mapping in mappings:
            self.failing[mapping.source_id] = (
                f"{AllRejected.code}: every row of mapping {mapping.mapping_id} was set aside"
            )
        return AllRejected(message, detail={"mappings": labels})

    # -- failing ------------------------------------------------------------------

    def _fail(self, error: SyncError) -> None:
        """Record a failed run as one version: the run and the sources at fault."""
        self.error = {"code": error.code, "message": error.message}
        if self.state is None or self.plan is None:
            return
        try:
            head = self.loader.head if self.loader is not None else self.state.head
            if not self.uid:
                self._name(head.t)
            meta = CommitMeta(cause="sync", actor=ACTOR, t=head.t, run=self.uid)
            rows_written = self.loader.rows_written if self.loader is not None else 0
            groups: list[dict[str, list[dict[str, Any]]]] = []
            if self.state.interrupted is not None and not self._versions():
                groups.append({RUN_TYPE: [interrupted_row(self.state.interrupted)]})
            groups.append(
                {
                    RUN_TYPE: [
                        self._run_row(meta.t, FAILED, len(self._versions()) + 1, rows_written)
                    ],
                    SOURCE_TYPE: [
                        source_row(
                            self.state,
                            source_id,
                            self.sources[source_id],
                            failing=text,
                            watermark=None,
                            t=meta.t,
                        )
                        for source_id, text in self.failing.items()
                    ],
                }
            )
            version = self.store.commit(
                ChangeSet(upserts=merge_rows(*groups), provenance=provenance(meta.t)), meta
            )
        except Exception as exc:
            log.error(
                "the failure could not be recorded in the twin; the error is raised all the same",
                exc=exc,
                run=self.uid,
                code=error.code,
            )
            return
        self.extra_versions.append(version)
        log.warn(
            "run failed; the failure is recorded as a version",
            run=self.uid,
            code=error.code,
            seq=version.seq,
            failing_sources=sorted(self.failing),
        )

    # -- report -------------------------------------------------------------------

    def _report(self, status: str) -> LoadReport:
        plan = self.plan
        versions = self._versions()
        report = LoadReport(
            run=self.uid,
            status=status,
            kind=plan.kind if plan is not None else "incremental",
            versions_written=len(versions),
            first_seq=versions[0].seq if versions else None,
            last_seq=versions[-1].seq if versions else None,
            rows_read=self._rows_read(),
            rows_written=self.loader.rows_written if self.loader is not None else 0,
            rows_rejected=self._rows_rejected(),
            rejects_location=self.rejects.location
            if self.rejects is not None and self.rejects.written
            else None,
            watermark=self._watermark(),
            unmapped_columns=list(self.unmapped),
            duration_ms=int((time.perf_counter() - self.clock) * 1000.0),
            resumed_run=self.state.interrupted.uid
            if self.state is not None and self.state.interrupted is not None
            else None,
            error=self.error,
        )
        if plan is None:
            return report
        for source_id in plan.source_ids():
            count = SourceCount(status="failing" if source_id in self.failing else "ok")
            for key, read in self.reads.items():
                if key[0] == source_id:
                    count.datasets[key[1]] = read.table.num_rows
                    count.rows_read += read.table.num_rows
            count.watermark = self._watermark(source_id)
            report.sources[source_id] = count
        for mapping in plan.mappings:
            count = MappingCount(mapping.object_type.name)
            if self.prepared is not None:
                tally = self.prepared.tallies[mapping.index]
                count.rows_read = tally.rows
                count.rows_rejected = tally.rejected
            if self.loader is not None:
                count.rows_written = self.loader.written[mapping.index]
                count.rows_deleted = self.loader.deleted[mapping.index]
                count.rows_rejected += (
                    self.loader.dropped[mapping.index] + self.loader.deletes_refused[mapping.index]
                )
            report.mappings[mapping.label] = count
            source = report.sources.get(mapping.source_id)
            if source is not None:
                source.rows_written += count.rows_written
                source.rows_rejected += count.rows_rejected
        return report

    # -- the run ------------------------------------------------------------------

    def _load(self) -> None:
        assert self.state is not None
        prepared = self._prepare()
        first = prepared.keys[0] if prepared.keys else None
        self._name(version_time(prepared, self.state.head.t, first))
        self._record_set_aside()
        rejected = self._rejected_mappings()
        if rejected:
            # Known before anything is written: nothing of the run goes in.
            raise self._note_rejected(rejected)
        assert self.rejects is not None
        self.loader = Loader(
            store=self.store,
            prepared=prepared,
            head=self.state.head,
            run_uid=self.uid,
            rejects=self.rejects,
            records=self._records,
            must_record=bool(prepared.cursors) or self.state.interrupted is not None,
        )
        started = time.perf_counter()
        self.loader.run()
        log.info(
            "versions written",
            versions=len(self.loader.versions),
            rows_written=self.loader.rows_written,
            rows_set_aside=sum(self.loader.dropped),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        rejected = self._rejected_mappings()
        if rejected:
            error = self._note_rejected(rejected)
            if self.loader.versions:
                # The last version already holds the run, recorded as failed.
                error.detail["recorded"] = True
            raise error

    def execute(self) -> LoadReport:
        log.info("sync run started", trace_id=self.trace_id)
        plan = self._plan()
        if not plan.mappings:
            log.warn("the manifest maps nothing; there is nothing to load")
            return self._report(SUCCEEDED)
        self._connector()
        self.state = read_state(self.store)
        try:
            self._extract()
            try:
                self._load()
            except SyncError as exc:
                if not exc.detail.pop("recorded", False):
                    self._fail(exc)
                exc.detail["report"] = self._report(FAILED).as_dict()
                raise
            except Exception as exc:
                # The text of an unexpected error may quote a row; only its type is kept.
                log.error(
                    "run failed unexpectedly",
                    error_type=type(exc).__name__,
                    frames="".join(traceback.format_tb(exc.__traceback__)),
                )
                log.debug("unexpected error in full", error=str(exc))
                error = SyncError(
                    f"the run failed unexpectedly: {type(exc).__name__}", code=UNEXPECTED
                )
                self._fail(error)
                error.detail["report"] = self._report(FAILED).as_dict()
                raise error from None
        finally:
            if self.prepared is not None:
                self.prepared.close()
            if self.rejects is not None:
                self.rejects.close()
        report = self._report(SUCCEEDED)
        log.info(
            "sync run finished",
            run=report.run,
            kind=report.kind,
            versions_written=report.versions_written,
            first_seq=report.first_seq,
            last_seq=report.last_seq,
            rows_read=report.rows_read,
            rows_written=report.rows_written,
            rows_rejected=report.rows_rejected,
            rejects_location=report.rejects_location,
            unmapped_columns=len(report.unmapped_columns),
            duration_ms=report.duration_ms,
        )
        return report


def load(
    twin: Twin,
    settings: RuntimeSettings,
    env: Mapping[str, str],
    *,
    connect: Connect | None = None,
) -> LoadReport:
    """Read every source of the twin's manifest once and write what is new.

    `connect` opens the connector of one source; by default it is
    `dta_core.connect.open_connector`. Raises a `SyncError` when the run
    fails; its `detail["report"]` then holds the counts reached.
    """
    with trace(current_trace_id()) as trace_id:
        return _Run(twin, settings, env, connect, trace_id).execute()


def prepare_load(
    twin: Twin,
    settings: RuntimeSettings,
    env: Mapping[str, str],
    *,
    connect: Connect | None = None,
) -> Prepared:
    """Everything `load` does before it writes: plan, extract, map and compare.

    Nothing is written. The caller closes the result.
    """
    with trace(current_trace_id()) as trace_id:
        run = _Run(twin, settings, env, connect, trace_id)
        run._plan()
        opener = run._connector()
        run.state = read_state(run.store)
        try:
            run.reads = extract(run.plan, run.sources, env, opener, run._stored_cursors())
        except SourceFailure as failure:
            raise SourceFailed(
                f"source {failure.source_id} cannot be read: {failure.message}",
                detail={"source": failure.source_id, "cause": failure.code},
            ) from None
        return run._prepare()
