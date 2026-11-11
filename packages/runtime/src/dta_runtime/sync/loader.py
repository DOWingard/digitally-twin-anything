"""Writes a prepared run into the store, one version per interval.

A version is committed without being validated first: the store checks it
anyway, and checking twice would double the cost of every version. When the
store refuses one, the rows it names are set aside and the rest is tried
again, until it is accepted or nothing is left.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from dta_core.log import Log
from dta_core.schema import UID_FIELD, Kind, Violation
from dta_core.store import (
    EPOCH,
    ChangeSet,
    CommitMeta,
    LockTimeout,
    Provenance,
    Store,
    StoreError,
    StoreUnavailable,
    VersionRef,
    ViolationError,
)

from .errors import StoreFailed
from .mapper import Prepared, bucket_end_us
from .plan import MappingPlan
from .records import ACTOR, provenance
from .rejects import Rejects

log = Log("sync.load")

_COMMIT_ATTEMPTS = 4
_BACKOFF_S = 0.5
_HEAD_PROBES = 4
_PROGRESS_EVERY_S = 5.0
_DELETE_RULES = ("referenced", "database")

SlotKey = tuple[str, str, int]  # (type, uid, slot index)
# Extra rows of a version: (its time, its interval key or None, whether it is the last).
Records = Callable[[datetime, int | None, bool], dict[str, list[dict[str, Any]]]]


@dataclass
class Draft:
    """One object's row in a version being put together."""

    uid: str
    values: dict[str, Any] = field(default_factory=dict)
    # property -> (mapping it came from, valid time; None stands for the version's time)
    origin: dict[str, tuple[int, datetime | None]] = field(default_factory=dict)
    mappings: list[int] = field(default_factory=list)
    # property -> slot, for the properties that come from a time series
    series: dict[str, int] = field(default_factory=dict)


@dataclass
class Bucket:
    key: int | None
    rows: dict[str, dict[str, Draft]] = field(default_factory=dict)
    deletes: dict[str, list[str]] = field(default_factory=dict)

    def size(self) -> int:
        return sum(len(drafts) for drafts in self.rows.values()) + sum(
            len(uids) for uids in self.deletes.values()
        )


def _whole_ms(moment: datetime) -> datetime:
    """`moment`, or the next whole millisecond after it.

    The store keeps version times to the millisecond and cuts off the rest;
    rounding up instead keeps a version from being earlier than what it holds.
    """
    rest = moment.microsecond % 1000
    return moment + timedelta(microseconds=1000 - rest) if rest else moment


def version_time(prepared: Prepared, head_t: datetime, key: int | None) -> datetime:
    """The time of the version that takes an interval, given the head it is written on.

    With a backfill interval it is the end of the interval; otherwise the
    latest valid time read. Data older than the head is recorded at the
    head's time and keeps its own valid time.
    """
    interval_us = prepared.plan.interval_us
    latest = max(prepared.cursors.values(), default=None)
    if key is not None and prepared.timed:
        moment = EPOCH + timedelta(microseconds=key)
    elif latest is None:
        moment = head_t
    elif interval_us is not None:
        micros = (latest - EPOCH) // timedelta(microseconds=1)
        moment = EPOCH + timedelta(microseconds=bucket_end_us(micros, interval_us))
    else:
        moment = _whole_ms(latest)
    return max(head_t, moment)


class Loader:
    def __init__(
        self,
        *,
        store: Store,
        prepared: Prepared,
        head: VersionRef,
        run_uid: str,
        rejects: Rejects,
        records: Records,
        must_record: bool,
    ) -> None:
        self.store = store
        self.prepared = prepared
        self.plan = prepared.plan
        self.head = head
        self.run_uid = run_uid
        self.rejects = rejects
        self.records = records
        # Whether a version is due even when no data is left to write: a cursor
        # moved, or an interrupted run is waiting to be recorded.
        self.must_record = must_record
        self.extra = {"backfill": True} if self.plan.interval_us else {}
        self.versions: list[VersionRef] = []
        self.rows_written = 0
        # Rows of the version being attempted, for the run's own record of itself.
        self.pending = 0
        count = len(self.plan.mappings)
        self.written = [0] * count
        self.deleted = [0] * count
        # Rows of a refused version set aside, and deletions refused, per mapping.
        self.dropped = [0] * count
        self.deletes_refused = [0] * count
        # What the store holds for a time-series property whose latest value was
        # set aside, and the next row of that series, due at a later interval.
        self._baselines: dict[SlotKey, Any] = {}
        self._due: dict[int, dict[SlotKey, dict[str, Any]]] = {}
        self._forced: set[SlotKey] = set()
        self._provenances: dict[tuple[int, datetime], Provenance] = {}

    # -- times ------------------------------------------------------------------

    def interval_end(self, key: int | None) -> datetime | None:
        """The end of the interval a key stands for, when keys are times."""
        if key is None or not self.prepared.timed:
            return None
        return EPOCH + timedelta(microseconds=key)

    # -- putting a version together -----------------------------------------------

    def _mapping(self, index: int) -> MappingPlan:
        return self.plan.mappings[index]

    def _assemble(self, key: int) -> Bucket:
        bucket = Bucket(key)
        for type_name, batch in self.prepared.batches.items():
            span = batch.spans.get(key)
            if span is not None:
                columns = batch.rows.slice(*span).to_pydict()
                slots = [
                    (
                        slot,
                        columns[f"v_{slot.index}"],
                        columns[f"w_{slot.index}"],
                        columns[f"tv_{slot.index}"],
                        columns[f"m_{slot.index}"],
                    )
                    for slot in batch.slots
                ]
                drafts: dict[str, Draft] = {}
                for row, uid in enumerate(columns[UID_FIELD]):
                    draft = Draft(uid, mappings=list(columns["ms"][row]))
                    for slot, values, written, valid, origin in slots:
                        if not written[row]:
                            continue
                        name = slot.field.name
                        value = values[row]
                        if slot.field.kind == Kind.json and value is not None:
                            value = json.loads(value)
                        draft.values[name] = value
                        draft.origin[name] = (origin[row], valid[row])
                        if self._mapping(origin[row]).timeseries:
                            draft.series[name] = slot.index
                    drafts[uid] = draft
                bucket.rows[type_name] = drafts
            if batch.deletes and key == self.prepared.static_key:
                bucket.deletes[type_name] = list(batch.deletes)
        self._apply_due(bucket)
        return bucket

    def _provenance(self, mapping: int, valid: datetime | None, t: datetime) -> Provenance:
        at = valid or t
        found = self._provenances.get((mapping, at))
        if found is None:
            plan = self._mapping(mapping)
            found = Provenance(source=plan.source_id, kind=plan.kind, t_valid=at, confidence=1.0)
            if len(self._provenances) > 100_000:
                self._provenances.clear()
            self._provenances[(mapping, at)] = found
        return found

    def _changes(self, bucket: Bucket, t: datetime) -> ChangeSet:
        changes = ChangeSet()
        for type_name, drafts in bucket.rows.items():
            if not drafts:
                continue
            rows = []
            for draft in drafts.values():
                rows.append({UID_FIELD: draft.uid, **draft.values})
                given = {
                    name: self._provenance(mapping, valid, t)
                    for name, (mapping, valid) in draft.origin.items()
                }
                if given:
                    # The row carries what most of its properties share; the others their own.
                    whole = Counter(given.values()).most_common(1)[0][0]
                else:
                    whole = self._provenance(draft.mappings[0], None, t)
                changes.row_provenance[draft.uid] = whole
                own = {name: found for name, found in given.items() if found != whole}
                if own:
                    changes.property_provenance[draft.uid] = own
            changes.upserts[type_name] = rows
        for type_name, uids in bucket.deletes.items():
            if uids:
                changes.deletes[type_name] = list(uids)
        return changes

    # -- committing -------------------------------------------------------------

    def _landed(self, meta: CommitMeta) -> VersionRef | None:
        """The version a failed commit call wrote all the same, or None when it wrote none."""
        head: VersionRef | None = None
        for probe in range(_HEAD_PROBES):
            try:
                head = self.store.head()
                break
            except StoreError as exc:
                log.warn(
                    "cannot read the head to see whether the version was written; trying again",
                    probe=probe + 1,
                    of=_HEAD_PROBES,
                    code=exc.code,
                )
                time.sleep(_BACKOFF_S * 2**probe)
        if head is None:
            raise StoreFailed(
                "a version may or may not have been written and the store cannot be asked; "
                "the next run continues after whatever is there",
                detail={"run": self.run_uid, "t": meta.t.isoformat()},
            )
        if head.id == self.head.id:
            return None
        mine = self.store.versions(f"#{self.head.seq + 1}..head", run=self.run_uid)
        if len(mine) == 1 and mine[0].seq == self.head.seq + 1 and mine[0].t == meta.t:
            return mine[0].ref
        log.error(
            "the branch moved during the run by something else than this run",
            expected_seq=self.head.seq + 1,
            head_seq=head.seq,
            own_versions=len(mine),
        )
        raise StoreFailed(
            "another writer added versions while this run was writing; the run stops rather "
            "than write on top of a state it did not compare with",
            detail={"run": self.run_uid, "head_seq": head.seq, "expected": self.head.seq + 1},
        )

    def _commit(self, changes: ChangeSet, meta: CommitMeta) -> VersionRef:
        """Commit once. A call that fails is never repeated before it is known not to
        have written the version."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.store.commit(changes, meta)
            except ViolationError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, StoreError) else type(exc).__name__
                log.warn(
                    "commit call failed; finding out whether the version was written",
                    attempt=attempt,
                    code=code,
                    t=meta.t.isoformat(),
                )
                landed = self._landed(meta)
                if landed is not None:
                    log.warn(
                        "the version is in place although the commit call failed; carrying on",
                        seq=landed.seq,
                        version=landed.id,
                    )
                    return landed
                retryable = isinstance(exc, StoreUnavailable | LockTimeout)
                if not retryable or attempt >= _COMMIT_ATTEMPTS:
                    log.error(
                        "version could not be written; the run stops",
                        exc=exc,
                        attempts=attempt,
                        code=code,
                    )
                    raise StoreFailed(
                        f"a version could not be written after {attempt} attempt(s): {code}",
                        detail={"run": self.run_uid, "t": meta.t.isoformat(), "cause": code},
                    ) from exc
                wait = _BACKOFF_S * 2 ** (attempt - 1)
                log.warn(
                    "nothing was written; committing again after a pause",
                    attempt=attempt,
                    of=_COMMIT_ATTEMPTS,
                    wait_s=wait,
                )
                time.sleep(wait)

    def _write(self, bucket: Bucket, last: bool) -> None:
        while True:
            if bucket.size() == 0 and not (last and (self.versions or self.must_record)):
                log.debug("nothing left to write for the interval", key=bucket.key)
                return
            meta = CommitMeta(
                cause="sync",
                actor=ACTOR,
                t=version_time(self.prepared, self.head.t, bucket.key),
                run=self.run_uid,
                extra=self.extra,
            )
            self.pending = bucket.size()
            changes = self._changes(bucket, meta.t)
            for type_name, rows in self.records(meta.t, bucket.key, last).items():
                changes.upserts[type_name] = rows
            changes.provenance = provenance(meta.t)
            try:
                version = self._commit(changes, meta)
            except ViolationError as exc:
                self._set_aside(bucket, exc.violations, meta.t)
                continue
            self._committed(bucket, version)
            return

    def _committed(self, bucket: Bucket, version: VersionRef) -> None:
        self.head = version
        self.versions.append(version)
        for drafts in bucket.rows.values():
            self.rows_written += len(drafts)
            for draft in drafts.values():
                for mapping in draft.mappings:
                    self.written[mapping] += 1
        for type_name, uids in bucket.deletes.items():
            self.rows_written += len(uids)
            mapping = self.prepared.batches[type_name].delete_mapping
            if mapping is not None:
                self.deleted[mapping] += len(uids)
        # A series whose value was forced into this version is level with the store again.
        for slot_key in self._forced:
            type_name, uid, _ = slot_key
            if uid in bucket.rows.get(type_name, {}):
                self._baselines.pop(slot_key, None)
        self._forced.clear()

    # -- setting rows aside -------------------------------------------------------

    def _set_aside(self, bucket: Bucket, violations: list[Violation], t: datetime) -> None:
        by_row: dict[tuple[str, str], list[Violation]] = {}
        by_delete: dict[tuple[str, str], list[Violation]] = {}
        loose: list[Violation] = []
        for violation in violations:
            uid = violation.uid
            home = next(
                (name for name, drafts in bucket.rows.items() if uid is not None and uid in drafts),
                None,
            )
            if home is not None and uid is not None:
                by_row.setdefault((home, uid), []).append(violation)
                continue
            gone = next(
                (name for name, uids in bucket.deletes.items() if uid is not None and uid in uids),
                None,
            )
            if gone is not None and uid is not None:
                by_delete.setdefault((gone, uid), []).append(violation)
            else:
                loose.append(violation)
        for (type_name, uid), found in by_row.items():
            draft = bucket.rows[type_name][uid]
            at_fault = self._at_fault(draft, found)
            for index in sorted(at_fault):
                mapping = self._mapping(index)
                self.dropped[index] += 1
                own = [
                    violation
                    for violation in found
                    if draft.origin.get(violation.property or "", (index, None))[0] == index
                ]
                self.rejects.add_violations(
                    mapping=mapping.label,
                    source=mapping.source_id,
                    type_name=type_name,
                    uid=uid,
                    violations=own or found,
                    t=t,
                )
            self._withdraw(bucket, type_name, draft, at_fault)
        for (type_name, uid), found in by_delete.items():
            bucket.deletes[type_name].remove(uid)
            self._refuse_delete(type_name, uid, found, t)
        unexplained = []
        for violation in loose:
            pending = bucket.deletes.get(violation.type) or []
            if violation.uid is None and pending and violation.rule in _DELETE_RULES:
                # The database refused the deletions of a type without naming one.
                for uid in list(pending):
                    self._refuse_delete(violation.type, uid, [violation], t)
                bucket.deletes[violation.type] = []
            else:
                unexplained.append(violation)
        log.warn(
            "version refused; violating rows set aside and the rest is committed again",
            t=t.isoformat(),
            rows_set_aside=len(by_row),
            deletes_refused=len(by_delete),
            rules=sorted({violation.rule for violation in violations}),
            left=bucket.size(),
        )
        if unexplained and not by_row and not by_delete and len(unexplained) == len(violations):
            log.error(
                "the store refused a version for reasons no row of it accounts for",
                violations=[(v.type, v.uid, v.property, v.rule) for v in unexplained[:10]],
            )
            raise StoreFailed(
                "the store refused a version for a reason no row of it accounts for: "
                f"{unexplained[0].rule}",
                detail={
                    "run": self.run_uid,
                    "violations": [
                        {"type": v.type, "uid": v.uid, "property": v.property, "rule": v.rule}
                        for v in unexplained[:20]
                    ],
                },
            )

    def _at_fault(self, draft: Draft, found: list[Violation]) -> set[int]:
        """The mappings whose part of a refused row is set aside.

        An object's row in a version can hold what several mappings say about
        it. A broken rule that names a property is laid at the door of the
        mapping that wrote it. One that names none, or a property nobody
        wrote, cannot be: then the readings go first and what describes the
        object itself is tried again, so that one bad reading does not keep
        its object, and with it every later reading, out of the twin.
        """
        everyone = set(draft.mappings)
        named = {
            draft.origin[violation.property][0]
            for violation in found
            if violation.property in draft.origin
        }
        if named and all(violation.property in draft.origin for violation in found):
            return named
        readings = {index for index in everyone if self._mapping(index).timeseries}
        if readings and readings != everyone:
            return named | readings
        return everyone

    def _withdraw(self, bucket: Bucket, type_name: str, draft: Draft, at_fault: set[int]) -> None:
        """Take what the mappings at fault wrote out of a row; the rest is committed again."""
        withdrawn = [name for name, (index, _) in draft.origin.items() if index in at_fault]
        slots = [draft.series[name] for name in withdrawn if name in draft.series]
        for name in withdrawn:
            del draft.values[name], draft.origin[name]
            draft.series.pop(name, None)
        draft.mappings = [index for index in draft.mappings if index not in at_fault]
        if not draft.mappings:
            del bucket.rows[type_name][draft.uid]
        self._unsettle(type_name, draft.uid, slots, bucket.key)

    def _refuse_delete(self, type_name: str, uid: str, found: list[Violation], t: datetime) -> None:
        index = self.prepared.batches[type_name].delete_mapping
        if index is None:
            return
        mapping = self._mapping(index)
        self.deletes_refused[index] += 1
        self.rejects.add_violations(
            mapping=mapping.label,
            source=mapping.source_id,
            type_name=type_name,
            uid=uid,
            violations=found,
            t=t,
        )

    # -- a series after one of its values was set aside ----------------------------
    #
    # Each row of a series was compared, before anything was written, with the row
    # of the interval before it. When a row is set aside, the store still holds
    # what it held before that row, so the next row of the series has to be
    # compared with that instead.

    def _unsettle(self, type_name: str, uid: str, slots: list[int], key: int | None) -> None:
        if key is None:
            return
        for slot in slots:
            slot_key = (type_name, uid, slot)
            if slot_key not in self._baselines:
                self._baselines[slot_key] = self.prepared.previous(type_name, slot, uid, key)
            following = self.prepared.following(type_name, slot, uid, key)
            if following is None:
                self._baselines.pop(slot_key, None)
            else:
                self._due.setdefault(following["tk"], {})[slot_key] = following
            self._forced.discard(slot_key)

    def _apply_due(self, bucket: Bucket) -> None:
        if bucket.key is None:
            return
        for slot_key, row in self._due.pop(bucket.key, {}).items():
            type_name, uid, index = slot_key
            slot = self.prepared.batches[type_name].slots[index]
            name = slot.field.name
            value, held = row["v"], self._baselines.get(slot_key)
            if slot.field.kind == Kind.json:
                value = json.loads(value) if value is not None else None
                held = json.loads(held) if held is not None else None
            drafts = bucket.rows.setdefault(type_name, {})
            draft = drafts.get(uid)
            if value == held:
                # The store already holds this value: nothing to write for the property.
                self._baselines.pop(slot_key, None)
                if draft is not None and name in draft.values:
                    del draft.values[name], draft.origin[name]
                    draft.series.pop(name, None)
                    contributing = {mapping for mapping, _ in draft.origin.values()}
                    draft.mappings = [
                        m
                        for m in draft.mappings
                        if m in contributing or not self._mapping(m).timeseries
                    ]
                    if not draft.mappings:
                        del drafts[uid]
                continue
            if draft is None:
                draft = drafts[uid] = Draft(uid)
            draft.values[name] = value
            draft.origin[name] = (row["m"], row["tv"])
            draft.series[name] = index
            if row["m"] not in draft.mappings:
                draft.mappings = sorted([*draft.mappings, row["m"]])
            self._forced.add(slot_key)

    # -- the run ------------------------------------------------------------------

    def _closing_key(self) -> int | None:
        """The interval of the latest reading, which the run's last version stands for.

        A run ends at the time its data reaches, whether or not the last
        readings changed anything, so the twin's time does not depend on that.
        """
        interval_us = self.plan.interval_us
        latest = max(self.prepared.cursors.values(), default=None)
        if latest is None or interval_us is None or not self.prepared.timed:
            return None
        return bucket_end_us((latest - EPOCH) // timedelta(microseconds=1), interval_us)

    def run(self) -> None:
        keys: list[int | None] = list(self.prepared.keys)
        closing = self._closing_key()
        if closing is not None and (not keys or keys[-1] < closing):
            keys.append(closing)
        elif not keys and self.must_record:
            keys = [None]
        started = reported = time.monotonic()
        position = 0
        while position < len(keys):
            key = keys[position]
            bucket = Bucket(None) if key is None else self._assemble(key)
            self._write(bucket, last=position == len(keys) - 1)
            position += 1
            if self._due:
                # A row set aside leaves the next row of its series due at an interval
                # that may have had nothing else to write. It lies before the closing
                # interval, so the last interval stays the last.
                missing = set(self._due).difference(keys)
                if missing:
                    keys[position:] = sorted({*keys[position:], *missing})
            now = time.monotonic()
            if now - reported >= _PROGRESS_EVERY_S and position < len(keys):
                reported = now
                log.info(
                    "loading",
                    intervals_done=position,
                    intervals=len(keys),
                    versions_written=len(self.versions),
                    rows_written=self.rows_written,
                    rows_set_aside=sum(self.dropped),
                    elapsed_s=round(now - started, 1),
                )
        self.rejects.flush()
