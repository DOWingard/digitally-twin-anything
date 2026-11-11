"""One run of the sync pipeline: plan, extract, map, compare, load, record.

There are two ways in. `load` reads existing history once: everything it finds
lies in the past, and a source that cannot be read stops it. `run` is what a
live twin repeats: it is handed a clock, takes only the intervals that have
ended, lets a failing source fail alone, and always leaves a record of itself.
Both are the same pipeline.

Nothing that ends up in the graph comes from the wall clock except how long
the run took. Version times and valid times come from the data; `now`, read
once from the clock a run is handed, only decides which intervals have ended.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dta_core.config import require_env
from dta_core.log import Log, current_trace_id, trace
from dta_core.manifest.models import Source
from dta_core.store import EPOCH, ChangeSet, CommitMeta, VersionRef
from dta_core.store.model import iso_utc

from ..settings import RuntimeSettings
from ..twin import Twin
from .errors import (
    ALL_REJECTED,
    PARTIAL,
    REPLAY_UNKNOWN,
    SOURCE_FAILED,
    UNEXPECTED,
    UNKNOWN_SOURCE,
    AllRejected,
    ReplayUnsupported,
    SourceFailed,
    SyncError,
)
from .extract import Connect, DatasetRead, SourceFailure, extract, required_env
from .loader import Loader, version_time
from .mapper import Prepared, Window, prepare
from .plan import MappingPlan, Plan, RelationKey, build_plan, dataset_uid, unmapped_columns
from .records import (
    ACTOR,
    DATASET_TYPE,
    RUN_TYPE,
    SOURCE_TYPE,
    Cursor,
    State,
    cursor_text,
    dataset_row,
    interrupted_row,
    merge_rows,
    provenance,
    read_state,
    run_key,
    run_uid,
    source_row,
)
from .rejects import (
    RULE_DUPLICATE,
    RULE_RANGE,
    RULE_TYPE,
    RULE_UID,
    RULE_VALID_TIME,
    Rejects,
)
from .report import (
    FAILED,
    FAILING,
    INCREMENTAL,
    OK,
    REPLAY,
    SUCCEEDED,
    DatasetRun,
    LoadReport,
    MappingCount,
    RunReport,
    SourceCount,
    SourceRun,
)
from .report import PARTIAL as PARTLY

if TYPE_CHECKING:
    from ..clock import Clock

log = Log("sync")

_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class Replay:
    """One dataset of a source to read again whole, whatever its cursor says.

    Only what feeds static mappings or events can be read again: both are
    compared with what is stored, so a replay writes the differences alone.
    """

    source: str
    dataset: str


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


def _micros(moment: datetime) -> int:
    return (moment - EPOCH) // _MICROSECOND


class _Run:
    def __init__(
        self,
        twin: Twin,
        settings: RuntimeSettings,
        env: Mapping[str, str],
        connect: Connect | None,
        trace_id: str,
        *,
        now: datetime | None = None,
        selected: Sequence[str] | None = None,
        replay: Replay | None = None,
    ) -> None:
        self.twin = twin
        self.store = twin.store
        self.settings = settings
        self.env = env
        self._connect = connect
        self.trace_id = trace_id
        # The time a run that repeats was given; None for a load of existing history.
        self.now = now
        self.recurring = now is not None
        self.selected = frozenset(selected) if selected is not None else None
        self.replay = replay
        self.clock = time.perf_counter()
        self.sources: Mapping[str, Source] = {}
        self.plan: Plan | None = None
        self.state: State | None = None
        # The mappings the run evaluates, and the sources they belong to.
        self.active: tuple[MappingPlan, ...] = ()
        self.run_sources: tuple[str, ...] = ()
        # Every source a dataset is asked of, in the order they are read.
        self.read_sources: tuple[str, ...] = ()
        # Datasets and views read whole although a cursor is stored for them.
        self.whole: frozenset[RelationKey] = frozenset()
        # Series read again from the start of an interval, with the cursor they had.
        self.resumed: dict[RelationKey, datetime] = {}
        self.window = Window()
        self.reads: dict[RelationKey, DatasetRead] = {}
        self.prepared: Prepared | None = None
        self.loader: Loader | None = None
        self.rejects: Rejects | None = None
        self.key = ""
        self.uid = ""
        self.unmapped: list[str] = []
        # Versions written outside the loader: the record of a failure.
        self.extra_versions: list[VersionRef] = []
        # Source -> (code, message) of what went wrong with it in this run.
        self.failing: dict[str, tuple[str, str]] = {}
        # Sources of which nothing was taken: they keep their cursors.
        self.left_out: set[str] = set()
        self.error: dict[str, str] | None = None
        self._stored: dict[RelationKey, Cursor] | None = None
        # Cursors written by versions that are in place, and those of the version in flight.
        self._confirmed: dict[RelationKey, Cursor] = {}
        self._proposed: tuple[int, dict[RelationKey, Cursor]] | None = None

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
        plan = build_plan(manifest, self.twin.ontology, recurring=self.recurring)
        self.active = self._selection(plan)
        needed = plan.needed(self.active)
        self.read_sources = tuple(
            dict.fromkeys(dataset.source_id for dataset in plan.datasets if dataset.key in needed)
        )
        mapped = {mapping.source_id for mapping in self.active}
        self.run_sources = tuple(source_id for source_id in self.sources if source_id in mapped)
        names: dict[str, None] = {}
        for source_id in self.read_sources:
            names.update(dict.fromkeys(required_env(self.sources[source_id])))
        # Before anything is read: a missing variable is named, never defaulted.
        require_env(names, self.env)
        self.plan = plan
        log.info(
            "plan made",
            mappings=len(self.active),
            views=len(plan.views),
            datasets=len(plan.datasets),
            kind=self._kind(),
            aggregation=plan.aggregation,
            interval_s=plan.interval_us / 1e6 if plan.interval_us else None,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return plan

    def _selection(self, plan: Plan) -> tuple[MappingPlan, ...]:
        """The mappings the caller asked for; raises when what was asked cannot be done."""
        mappings = plan.mappings
        if self.selected is not None:
            unknown = sorted(self.selected - set(self.sources))
            if unknown:
                log.error("a run was asked for sources the manifest does not have", sources=unknown)
                raise SyncError(
                    f"the manifest has no source {', '.join(unknown)}",
                    code=UNKNOWN_SOURCE,
                    detail={"sources": unknown, "known": sorted(self.sources)},
                )
            mappings = tuple(m for m in mappings if m.source_id in self.selected)
        if self.replay is None:
            return mappings
        target = (self.replay.source, self.replay.dataset)
        where = f"{target[0]}.{target[1]}"
        affected = tuple(m for m in mappings if target in plan.depends_on(m))
        if not affected:
            log.error("a replay names a dataset no mapping of the run reads", dataset=where)
            raise SyncError(
                f"no mapping of the run reads {where}; there is nothing to replay",
                code=REPLAY_UNKNOWN,
                detail={"source": target[0], "dataset": target[1]},
            )
        series = [m.label for m in affected if m.state]
        if series:
            log.error(
                "a replay names a dataset that feeds a state series",
                dataset=where,
                mappings=series,
            )
            raise ReplayUnsupported(
                f"{where} feeds a state series ({', '.join(series)}): a state property holds "
                "the latest value, so its history cannot be read again behind it",
                detail={"source": target[0], "dataset": target[1], "mappings": series},
            )
        self.whole = frozenset({target, *(m.key for m in affected)})
        return affected

    def _kind(self) -> str:
        if not self.recurring:
            return self.plan.kind if self.plan is not None else INCREMENTAL
        return REPLAY if self.replay is not None else INCREMENTAL

    def _window(self) -> None:
        """Which intervals have ended, and how far the twin had come before the run."""
        assert self.plan is not None and self.state is not None
        reached_us = _micros(self.state.head.t)
        if self.now is None:
            self.window = Window(reached_us=reached_us)
            return
        interval_us = self.plan.interval_us
        assert interval_us is not None
        now_us = _micros(self.now)
        limit_us = now_us - now_us % interval_us
        self.window = Window(limit_us=limit_us, reached_us=reached_us)
        if limit_us < reached_us:
            # After a load of history, whose last version is the end of an
            # interval that may still be open; or the clock went back.
            log.warn(
                "the last interval that has ended lies before the twin's latest version; "
                "what the run writes is recorded at that version's time",
                now=iso_utc(self.now),
                last_closed=iso_utc(EPOCH + timedelta(microseconds=limit_us)),
                head_t=iso_utc(self.state.head.t),
            )

    def _known_cursors(self) -> dict[RelationKey, Cursor]:
        """How far each dataset or view was read by earlier runs."""
        assert self.plan is not None and self.state is not None
        if self._stored is not None:
            return self._stored
        cursors: dict[RelationKey, Cursor] = {}
        for key, planned in self.plan.cursors.items():
            found = self.state.cursor(key, planned.time)
            if found is None:
                continue
            stored = self.state.datasets[dataset_uid(*key)].get("cursor_column")
            if stored and stored != planned.column:
                if not planned.time:
                    # A cursor of another column says nothing about this one.
                    log.warn(
                        "the cursor column of a dataset changed; the dataset is read whole "
                        "and compared with what is stored",
                        source=key[0],
                        dataset=key[1],
                        was=stored,
                        now=planned.column,
                    )
                    continue
                log.warn(
                    "the valid-time column of a dataset changed; reading on from the "
                    "time already reached",
                    source=key[0],
                    dataset=key[1],
                    was=stored,
                    now=planned.column,
                )
            if planned.time and not isinstance(found, datetime):
                continue
            cursors[key] = found
        self._stored = cursors
        return cursors

    def _reading_cursors(self) -> dict[RelationKey, Cursor]:
        """The cursors the run reads on from: the stored ones, less what is replayed.

        A run that repeats takes whole intervals. A series last read to a time
        inside an interval, by a load, which ends wherever its data ends, is
        read again from the start of that interval: the interval's value then
        comes from all of its rows, not from those that arrived since.
        """
        assert self.plan is not None
        interval_us = self.plan.interval_us if self.recurring else None
        cursors: dict[RelationKey, Cursor] = {}
        for key, found in self._known_cursors().items():
            if key in self.whole:
                continue
            if interval_us and isinstance(found, datetime) and self.plan.cursors[key].time:
                inside = _micros(found) % interval_us
                if inside:
                    if key not in self.resumed:
                        log.info(
                            "a series was last read to a time inside an interval; "
                            "the interval is read again from its start",
                            source=key[0],
                            dataset=key[1],
                            cursor=iso_utc(found),
                        )
                    self.resumed[key] = found
                    found = found - timedelta(microseconds=inside)
            cursors[key] = found
        return cursors

    def _extract(self) -> None:
        assert self.plan is not None
        started = time.perf_counter()
        try:
            extraction = extract(
                self.plan,
                self.sources,
                self.env,
                self._connector(),
                self._reading_cursors(),
                wanted=self.plan.needed(self.active),
                alone=self.recurring,
            )
        except SourceFailure as failure:
            where = failure.source_id + (f".{failure.dataset}" if failure.dataset else "")
            self.failing[failure.source_id] = (failure.code, failure.message)
            raise SourceFailed(
                f"source {where} cannot be read: {failure.message}",
                detail={
                    "source": failure.source_id,
                    "dataset": failure.dataset,
                    "cause": failure.code,
                },
            ) from None
        self.reads = extraction.reads
        for source_id, failure in extraction.failures.items():
            self.failing[source_id] = (failure.code, failure.message)
            self.left_out.add(source_id)
        self.active = tuple(m for m in self.active if m.source_id not in self.left_out)
        log.info(
            "sources read",
            sources=len(self.read_sources),
            failed=sorted(extraction.failures),
            datasets=len(self.reads),
            rows=sum(read.table.num_rows for read in self.reads.values()),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    def _prepare(self) -> Prepared:
        assert self.plan is not None and self.state is not None
        self.prepared = prepare(
            self.plan,
            self.reads,
            self.store,
            self.state.head,
            self._reading_cursors(),
            self.window,
            active=self.active,
            alone=self.recurring,
            accounted={key: self.state.rows_read(key) for key in self.plan.cursors},
            resumed=self.resumed,
        )
        for source_id, trouble in self.prepared.troubles.items():
            self.failing.setdefault(source_id, (trouble.code, trouble.message))
            self.left_out.add(source_id)
        self.active = self.prepared.active
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
                if f"o{j}" in table.column_names:
                    reasons.append(
                        (
                            f"o{j}",
                            prop.name,
                            RULE_RANGE,
                            "the value is outside the property's bounds",
                        )
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
        for mapping in self.prepared.active:
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

    def _rows_late(self, source_id: str | None = None) -> int:
        if self.prepared is None:
            return 0
        return sum(
            rows
            for key, rows in self.prepared.late.items()
            if source_id is None or key[0] == source_id
        )

    def _versions(self) -> list[VersionRef]:
        return [*(self.loader.versions if self.loader else []), *self.extra_versions]

    def _head(self) -> VersionRef | None:
        versions = self._versions()
        if versions:
            return versions[-1]
        return self.state.head if self.state is not None else None

    def _watermark(self, source_id: str | None = None) -> datetime | None:
        """Latest valid time the run took, from one source or from any."""
        if self.prepared is None:
            return None
        return max(
            (
                latest
                for key, latest in self.prepared.latest.items()
                if source_id is None or key[0] == source_id
            ),
            default=None,
        )

    def _source_watermark(self, source_id: str) -> datetime | None:
        """What the run adds to a source's watermark; None when it adds nothing.

        A source with a valid time of its own is as fresh as the latest one
        taken from it. One that is only read whole has none: it is as fresh
        as its last successful read.
        """
        if source_id in self.left_out:
            return None
        taken = self._watermark(source_id)
        if taken is not None or self.now is None or self.plan is None:
            return taken
        mine = [m for m in self.plan.mappings if m.source_id == source_id]
        if mine and not any(m.timeseries for m in mine):
            return self.now
        return None

    def _taken(self, key: RelationKey, end: datetime | None, last: bool) -> int:
        """Rows of a state series taken account of once the version in flight is in place."""
        assert self.prepared is not None
        consumed = self.prepared.consumed.get(key, [])
        if last or end is None:
            rows = sum(count for _, count in consumed)
        else:
            end_us = _micros(end)
            rows = sum(count for tk, count in consumed if tk <= end_us)
        return rows + self.prepared.late.get(key, 0)

    def _settled_cursors(self) -> dict[RelationKey, Cursor]:
        """Where every cursor stands in the versions that are in place."""
        if (
            self._proposed is not None
            and self.loader is not None
            and len(self.loader.versions) > self._proposed[0]
        ):
            self._confirmed.update(self._proposed[1])
            self._proposed = None
        return {**(self._stored or {}), **self._confirmed}

    # -- how the run ended ------------------------------------------------------------

    def _outcome(self) -> tuple[str, dict[str, str] | None]:
        """(status, error) of a run that repeats, from what is known so far."""
        if self.error is not None:
            return FAILED, self.error
        failed = [source_id for source_id in self.run_sources if source_id in self.failing]
        if failed and len(failed) == len(self.run_sources):
            causes = ", ".join(
                f"{source_id} ({self.failing[source_id][0]})" for source_id in failed
            )
            return FAILED, {
                "code": SOURCE_FAILED,
                "message": f"every source of the run failed: {causes}",
            }
        if self.failing:
            return PARTLY, None
        return SUCCEEDED, None

    def _recorded_outcome(self) -> tuple[str, str | None]:
        """(status, error) as the run's own record holds them.

        A run in which some sources failed is kept as partial; its error names
        those sources and what was wrong with each.
        """
        status, error = self._outcome()
        if status == SUCCEEDED:
            return SUCCEEDED, None
        if error is not None:
            return FAILED, f"{error['code']}: {error['message']}"
        causes = ", ".join(f"{name} ({code})" for name, (code, _) in sorted(self.failing.items()))
        return PARTLY, f"{PARTIAL}: some sources failed and the others were synced: {causes}"

    # -- what the run records about itself ------------------------------------------

    def _run_row(
        self, t: datetime, status: str, versions: int, rows_written: int
    ) -> dict[str, Any]:
        assert self.plan is not None and self.rejects is not None
        written = self._versions()
        error = f"{self.error['code']}: {self.error['message']}" if self.error else None
        if self.recurring and status != FAILED:
            status, error = self._recorded_outcome()
        return {
            "uid": self.uid,
            "kind": self._kind(),
            "status": status,
            "started_at": written[0].t if written else t,
            "finished_at": t,
            "duration_ms": int((time.perf_counter() - self.clock) * 1000.0),
            "versions_written": versions,
            "rejects_location": self.rejects.location if self.rejects.written else None,
            "unmapped_columns": list(self.unmapped),
            "watermark": self._watermark(),
            "sources": list(self.read_sources),
            "rows_read": self._rows_read(),
            "rows_written": rows_written,
            "rows_rejected": self._rows_rejected(),
            "rows_late": self._rows_late(),
            "clock_at": self.now,
            "error": error,
            "trace_id": self.trace_id,
        }

    def _progress(self, end: datetime | None, last: bool) -> dict[str, list[dict[str, Any]]]:
        """How far each dataset has been read once the version in flight is in place.

        Written with every version, not only the last: a run that stops
        half-way then leaves the twin knowing where to continue, and the rows
        already loaded are not loaded again.
        """
        assert self.prepared is not None and self.state is not None and self.loader is not None
        known = self._settled_cursors()
        proposed: dict[RelationKey, Cursor] = {}
        rows = []
        for key, final in self.prepared.cursors.items():
            planned = self.prepared.plan.cursors[key]
            if planned.time and isinstance(final, datetime):
                reached: Cursor = final if last or end is None else min(end, final)
            elif last:
                # A cursor that is not a time says nothing about an interval.
                reached = final
            else:
                continue
            before = known.get(key)
            if before is None or reached > before:
                proposed[key] = reached
                rows.append(
                    dataset_row(
                        self.state,
                        key,
                        cursor_column=planned.column,
                        cursor=reached,
                        rows_read=self._taken(key, end, last) if planned.time else None,
                    )
                )
        self._proposed = (len(self.loader.versions), proposed)
        return {DATASET_TYPE: rows}

    def _final(self, t: datetime) -> dict[str, list[dict[str, Any]]]:
        assert self.prepared is not None and self.state is not None and self.loader is not None
        plan = self.prepared.plan
        recorded = {mapping.key for mapping in self.prepared.active if mapping.timeseries}
        datasets = []
        for key, rows in self.prepared.relation_rows.items():
            if key[0] in self.left_out or not (key in self.reads or key in recorded):
                continue
            planned = plan.cursors.get(key)
            series = planned is not None and planned.time
            datasets.append(
                dataset_row(
                    self.state,
                    key,
                    cursor_column=planned.column if planned is not None else None,
                    cursor=None,
                    # A state series counts each row once, when it is taken; what
                    # is read whole every time counts every read.
                    rows_read=self._taken(key, None, True) if series else rows,
                    read_at=t,
                )
            )
        rejected = self._rejected_mappings()
        status = SUCCEEDED
        if rejected:
            self._note_rejected(rejected)
            if not self.recurring:
                status = FAILED
        reported = list(plan.source_ids()) if not self.recurring else list(self.run_sources)
        reported += [source_id for source_id in self.failing if source_id not in reported]
        sources = [
            source_row(
                self.state,
                source_id,
                self.sources[source_id],
                failing=self._failing_text(source_id),
                watermark=self._source_watermark(source_id),
                t=t,
            )
            for source_id in reported
        ]
        run = self._run_row(
            t, status, len(self._versions()) + 1, self.loader.rows_written + self.loader.pending
        )
        return {RUN_TYPE: [run], DATASET_TYPE: datasets, SOURCE_TYPE: sources}

    def _failing_text(self, source_id: str) -> str | None:
        found = self.failing.get(source_id)
        return f"{found[0]}: {found[1]}" if found is not None else None

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
        """Every row of these mappings was set aside: their sources are marked for it.

        In a load that fails the run. In a run that repeats it is the source
        that is failing: its cursor has moved past the rows, so the next run
        reads newer ones and the failure is recorded this once.
        """
        labels = [mapping.label for mapping in mappings]
        message = f"every row of {', '.join(labels)} was set aside"
        if not self.recurring:
            self.error = {"code": AllRejected.code, "message": message}
        for mapping in mappings:
            if mapping.source_id not in self.failing:
                log.warn(
                    "every row of a mapping was set aside",
                    mapping=mapping.label,
                    source=mapping.source_id,
                    code=ALL_REJECTED,
                )
            self.failing[mapping.source_id] = (
                ALL_REJECTED,
                f"every row of mapping {mapping.mapping_id} was set aside",
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
            # At the time reached, not at the end the run was heading for: the
            # next run can then still write each interval left over at its own end.
            moment = head.t
            if not self.uid:
                self._name(moment)
            meta = CommitMeta(cause="sync", actor=ACTOR, t=moment, run=self.uid)
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
                            failing=self._failing_text(source_id),
                            watermark=None,
                            t=meta.t,
                        )
                        for source_id in self.failing
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

    def _mapping_counts(self) -> dict[str, MappingCount]:
        assert self.plan is not None
        counts = {}
        for mapping in self.plan.mappings:
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
            counts[mapping.label] = count
        return counts

    def _report(self, status: str) -> LoadReport:
        plan = self.plan
        versions = self._versions()
        report = LoadReport(
            run=self.uid,
            status=status,
            kind=plan.kind if plan is not None else INCREMENTAL,
            versions_written=len(versions),
            first_seq=versions[0].seq if versions else None,
            last_seq=versions[-1].seq if versions else None,
            rows_read=self._rows_read(),
            rows_written=self.loader.rows_written if self.loader is not None else 0,
            rows_rejected=self._rows_rejected(),
            rows_late=self._rows_late(),
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
            count = SourceCount(status=FAILING if source_id in self.failing else OK)
            for key, read in self.reads.items():
                if key[0] == source_id:
                    count.datasets[key[1]] = read.table.num_rows
                    count.rows_read += read.table.num_rows
            count.watermark = self._watermark(source_id)
            count.rows_late = self._rows_late(source_id)
            report.sources[source_id] = count
        report.mappings = self._mapping_counts()
        for mapping in plan.mappings:
            source = report.sources.get(mapping.source_id)
            if source is not None:
                source.rows_written += report.mappings[mapping.label].rows_written
                source.rows_rejected += report.mappings[mapping.label].rows_rejected
        return report

    def _source_run(self, source_id: str, cursors: Mapping[RelationKey, Cursor]) -> SourceRun:
        assert self.plan is not None and self.now is not None
        found = SourceRun(status=FAILING if source_id in self.failing else OK)
        failure = self.failing.get(source_id)
        if failure is not None:
            found.error = {"code": failure[0], "message": failure[1]}
        known = [dataset.key for dataset in self.plan.datasets if dataset.source_id == source_id]
        known += [
            view.key
            for view in self.plan.views
            if view.source_id == source_id and view.materialised
        ]
        for key in known:
            planned = self.plan.cursors.get(key)
            cursor = cursors.get(key)
            rows = 0
            if key in self.reads:
                rows = self.reads[key].table.num_rows
                found.rows_read += rows
            elif self.prepared is not None and source_id not in self.left_out:
                rows = self.prepared.relation_rows.get(key, 0)
            found.datasets[key[1]] = DatasetRun(
                rows_read=rows,
                cursor_column=planned.column if planned is not None else None,
                cursor=cursor_text(cursor) if cursor is not None else None,
            )
        # As the record of the source holds it once the run is written: a
        # failing source keeps the watermark it had.
        before = self.state.watermark(source_id) if self.state is not None else None
        added = None if source_id in self.failing else self._source_watermark(source_id)
        found.watermark = max((w for w in (before, added) if w is not None), default=None)
        if found.watermark is not None:
            found.freshness_s = (self.now - found.watermark).total_seconds()
        found.rows_late = self._rows_late(source_id)
        return found

    def _run_report(self) -> RunReport:
        assert self.now is not None
        status, error = self._outcome()
        versions = self._versions()
        report = RunReport(
            run=self.uid,
            kind=self._kind(),
            now=self.now,
            status=status,
            error=error,
            versions_written=len(versions),
            first_seq=versions[0].seq if versions else None,
            last_seq=versions[-1].seq if versions else None,
            head=self._head(),
            rows_read=self._rows_read(),
            rows_written=self.loader.rows_written if self.loader is not None else 0,
            rows_rejected=self._rows_rejected(),
            rows_late=self._rows_late(),
            rejects_location=self.rejects.location
            if self.rejects is not None and self.rejects.written
            else None,
            watermark=self._watermark(),
            duration_ms=int((time.perf_counter() - self.clock) * 1000.0),
            unmapped_columns=list(self.unmapped),
            resumed_run=self.state.interrupted.uid
            if self.state is not None and self.state.interrupted is not None
            else None,
        )
        if self.plan is None:
            return report
        cursors = self._settled_cursors() if self.state is not None else {}
        reported = list(self.run_sources)
        reported += [source_id for source_id in self.failing if source_id not in reported]
        for source_id in reported:
            report.sources[source_id] = self._source_run(source_id, cursors)
        report.mappings = self._mapping_counts()
        for mapping in self.plan.mappings:
            source = report.sources.get(mapping.source_id)
            if source is not None:
                source.rows_written += report.mappings[mapping.label].rows_written
                source.rows_rejected += report.mappings[mapping.label].rows_rejected
        return report

    # -- the run ------------------------------------------------------------------

    def _sync(self) -> None:
        assert self.state is not None and self.plan is not None
        prepared = self._prepare()
        first = prepared.keys[0] if prepared.keys else prepared.last_key
        self._name(version_time(prepared, self.state.head.t, first))
        self._record_set_aside()
        rejected = self._rejected_mappings()
        if rejected and not self.recurring:
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
            # A run that repeats always leaves a record of itself. A load does
            # when a cursor moved or an interrupted run is waiting to be recorded.
            must_record=self.recurring
            or bool(prepared.cursors)
            or self.state.interrupted is not None,
            # Only a load of existing history marks its versions as such.
            extra={"backfill": True} if self.plan.interval_us and not self.recurring else {},
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
        if rejected and not self.recurring:
            error = self._note_rejected(rejected)
            if self.loader.versions:
                # The last version already holds the run, recorded as failed.
                error.detail["recorded"] = True
            raise error

    def _attempt(self) -> SyncError | None:
        """Read, map and write. Returns what failed the run, having recorded it."""
        try:
            self._extract()
            self._sync()
        except SyncError as exc:
            if not exc.detail.pop("recorded", False):
                self._fail(exc)
            return exc
        except Exception as exc:
            # The text of an unexpected error may quote a row; only its type is kept.
            log.error(
                "run failed unexpectedly",
                error_type=type(exc).__name__,
                frames="".join(traceback.format_tb(exc.__traceback__)),
            )
            log.debug("unexpected error in full", error=str(exc))
            error = SyncError(f"the run failed unexpectedly: {type(exc).__name__}", code=UNEXPECTED)
            self._fail(error)
            return error
        return None

    def execute(self) -> LoadReport | RunReport:
        log.info(
            "sync run started",
            trace_id=self.trace_id,
            now=iso_utc(self.now) if self.now is not None else None,
            sources=sorted(self.selected) if self.selected is not None else None,
            replay=f"{self.replay.source}.{self.replay.dataset}" if self.replay else None,
        )
        plan = self._plan()
        if not plan.mappings and not self.recurring:
            log.warn("the manifest maps nothing; there is nothing to load")
            return self._report(SUCCEEDED)
        self._connector()
        self.state = read_state(self.store)
        self._window()
        try:
            failure = self._attempt()
        finally:
            if self.prepared is not None:
                self.prepared.close()
            if self.rejects is not None:
                self.rejects.close()
        report: LoadReport | RunReport
        if self.recurring:
            report = self._run_report()
        else:
            report = self._report(FAILED if failure is not None else SUCCEEDED)
        counts = {
            "run": report.run,
            "kind": report.kind,
            "status": report.status,
            "versions_written": report.versions_written,
            "first_seq": report.first_seq,
            "last_seq": report.last_seq,
            "rows_read": report.rows_read,
            "rows_written": report.rows_written,
            "rows_rejected": report.rows_rejected,
            "rows_late": report.rows_late,
            "set_aside_by_rule": dict(self.rejects.rules) if self.rejects is not None else {},
            "rejects_location": report.rejects_location,
            "failing_sources": {name: code for name, (code, _) in sorted(self.failing.items())},
            "unmapped_columns": len(report.unmapped_columns),
            "duration_ms": report.duration_ms,
        }
        if failure is not None:
            log.error("sync run failed", code=failure.code, reason=failure.message, reached=counts)
            if not self.recurring:
                failure.detail["report"] = report.as_dict()
                raise failure from None
        elif report.status == FAILED:
            log.error("sync run failed: every source of the run failed", **counts)
        elif report.status == PARTLY:
            log.warn("sync run finished; some sources failed and the others were synced", **counts)
        else:
            log.info("sync run finished", **counts)
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
        report = _Run(twin, settings, env, connect, trace_id).execute()
        assert isinstance(report, LoadReport)
        return report


def run(
    twin: Twin,
    settings: RuntimeSettings,
    env: Mapping[str, str],
    clock: Clock,
    *,
    sources: Sequence[str] | None = None,
    replay: Replay | None = None,
    connect: Connect | None = None,
) -> RunReport:
    """One run of a twin that keeps in step with sources that change.

    `now` is read from the clock once. Only intervals of `sync.interval` that
    have ended by then are taken; each is written as one version, at its end,
    and the run always ends with a version that records it. A source that
    cannot be read or mapped is left as it was, marked failing, and tried
    again by the next run; the others are synced.

    `sources` keeps the run to the mappings of those sources. `replay` keeps
    it to the mappings that read one dataset, and reads that dataset whole.

    Raises `ClockUnavailable` when the clock cannot be read, and a `SyncError`
    or `ConfigError` when the manifest or what was asked cannot be used; in
    both cases nothing was read or written. Anything that goes wrong later is
    in the report: its `status` is `succeeded`, `partial` or `failed`.
    """
    # The clock's module opens connectors; it is loaded here, where a clock is
    # in hand anyway, so that a load does not need them.
    from ..clock import ClockUnavailable

    with trace(current_trace_id()) as trace_id:
        try:
            now = clock.now()
        except ClockUnavailable as exc:
            log.warn(
                "the clock cannot be read; nothing was read or written",
                clock=getattr(clock, "kind", None),
                code=exc.code,
            )
            raise
        if now.tzinfo is None or now.utcoffset() is None:
            log.error(
                "the clock gave a time without an offset; nothing was read or written",
                clock=getattr(clock, "kind", None),
            )
            raise ClockUnavailable(
                "the clock gave a time without an offset",
                detail={"clock": getattr(clock, "kind", None), "cause": "clock.no_offset"},
            )
        runner = _Run(
            twin,
            settings,
            env,
            connect,
            trace_id,
            now=now.astimezone(UTC),
            selected=sources,
            replay=replay,
        )
        report = runner.execute()
        assert isinstance(report, RunReport)
        return report


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
        runner = _Run(twin, settings, env, connect, trace_id)
        runner._plan()
        runner._connector()
        runner.state = read_state(runner.store)
        runner._window()
        runner._extract()
        return runner._prepare()
