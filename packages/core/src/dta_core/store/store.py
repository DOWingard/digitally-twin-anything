"""The store: a typed graph with its full version history, kept in Dolt.

Guarantees made here and relied on everywhere else:

- observed facts reach the live branch only from the sync pipeline;
- every stored row carries the provenance of its values;
- every version satisfies the constraints of the ontology;
- a committed version never changes.

How a version is written. One committer runs at a time, holding a lock in
the process and a named lock in the database. It opens a transaction, writes
the rows and calls `DOLT_COMMIT` inside that transaction; the call ends the
transaction and creates the commit as one step, so other connections see
either nothing or the complete version, and a process that dies at any point
before the call leaves no trace. Statements that change table definitions
cannot be part of a transaction, so installing and migrating build their
commit on a scratch branch and then move the live branch onto it with a
fast-forward, which is again a single step.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pymysql

from ..log import Log, current_trace_id
from ..schema import Ontology, RowValidator, Violation
from . import ddl
from .boundary import public
from .db import Conn, Database, error_text
from .errors import (
    BranchExists,
    HeadMoved,
    InvalidOntology,
    LockTimeout,
    MergeConflict,
    MigrationRequired,
    NothingToMerge,
    NotInstalled,
    OntologyMismatch,
    ProtectedBranch,
    RecoveryFailed,
    StoreClosed,
    StoreError,
    TimeRegression,
    UnknownBranch,
    UnknownVersion,
    ViolationError,
    invalid,
)
from .index import Chain, VersionIndex, commit_message
from .merge import check_merged
from .migrate import plan_migration
from .model import (
    EPOCH,
    LIVE_BRANCH,
    BranchInfo,
    Cause,
    ChangeSet,
    CommitMeta,
    Version,
    VersionRef,
    iso_utc,
)
from .reads import ALL_VERSIONS, At, Reads
from .selector import COMMIT_ID, Selector
from .settings import StoreSettings
from .validate import DescriptorValidator
from .write import apply_changes

log = Log("store")

BRANCH_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
# Where a change of table definitions is prepared. Not a valid caller-chosen name.
SCRATCH_BRANCH = "_dta_schema"
# Causes written only by the operation of the same name.
_OWN_OPERATION = {Cause.install: "install", Cause.migration: "migrate", Cause.merge: "merge"}
_STARTUP_LOCK_WAIT_S = 5


class Store(Reads):
    def __init__(
        self,
        settings: StoreSettings,
        ontology: Ontology,
        validator: RowValidator | None = None,
    ) -> None:
        """`ontology` is the one the caller works with: writes are accepted only
        while it is the ontology in force, and reads are shaped by it."""
        ddl.check_ontology(ontology)
        self._settings = settings
        self._db = Database(settings)
        self._ontology = ontology
        self._validator: RowValidator = validator or DescriptorValidator(ontology)
        self._index = VersionIndex()
        self._commit_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Descriptors by hash, and the hash stored at a commit. Neither can go stale.
        self._ontologies: dict[str, Ontology] = {ontology.hash: ontology}
        self._hash_at: dict[str, str] = {}
        self._opened = False
        self._closed = False
        name = f"dta_store:{settings.database}"
        if len(name) > 64:
            name = "dta_store:" + hashlib.sha256(settings.database.encode()).hexdigest()[:40]
        self._lock_name = name
        # Called with the name of each step of a commit; lets a test stop the process there.
        self._fault: Callable[[str], None] | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def ontology(self) -> Ontology:
        return self._ontology

    @property
    def settings(self) -> StoreSettings:
        return self._settings

    def close(self) -> None:
        """Release every connection. The store cannot be used afterwards."""
        self._closed = True
        try:
            self._db.dispose()
        except Exception as exc:
            log.warn("closing the connection pool failed", error=repr(exc))
        log.info("store closed", database=self._settings.database)

    def _ready(self) -> None:
        if self._closed:
            raise StoreClosed("the store has been closed")
        if self._opened:
            return
        with self._state_lock:
            if self._opened:
                return
            self._open()
            self._opened = True

    def _open(self) -> None:
        started = time.perf_counter()
        with self._db.connect() as conn:
            chain = self._index.refresh(conn, LIVE_BRANCH)
            row = conn.query(
                f"SELECT `format`, `twin_id` FROM `{ddl.META_TABLE}` AS OF %s", (chain.head.id,)
            )
            if not row or row[0][0] != ddl.FORMAT_VERSION:
                found = row[0][0] if row else None
                log.error("unsupported storage format", found=found, wanted=ddl.FORMAT_VERSION)
                raise StoreError(
                    f"the database uses storage format {found}; this layer reads "
                    f"{ddl.FORMAT_VERSION}",
                    code="store.unsupported_format",
                    detail={"found": found, "supported": ddl.FORMAT_VERSION},
                )
            self._recover_at_startup(conn)
        log.info(
            "store opened",
            database=self._settings.database,
            twin_id=row[0][1],
            head_seq=chain.head.seq,
            head=chain.head.id,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    def _recover_at_startup(self, conn: Conn) -> None:
        """Discard what a committer that died left behind, unless one is running now."""
        if not self._commit_lock.acquire(timeout=_STARTUP_LOCK_WAIT_S):
            log.warn("a commit is running in this process; working set not checked at startup")
            return
        try:
            if conn.scalar("SELECT GET_LOCK(%s, %s)", (self._lock_name, _STARTUP_LOCK_WAIT_S)) != 1:
                log.warn(
                    "another committer holds the database lock; the working set is "
                    "checked by the next commit instead",
                    waited_s=_STARTUP_LOCK_WAIT_S,
                )
                return
            try:
                dirty = conn.query("SELECT `table_name`, `staged`, `status` FROM `dolt_status`")
                if dirty:
                    self._discard(conn, LIVE_BRANCH, dirty)
                self._drop_scratch(conn, expected=False)
            finally:
                self._unlock(conn)
        finally:
            self._commit_lock.release()

    def _checkpoint(self, step: str) -> None:
        if self._fault is not None:
            self._fault(step)

    # -- the single committer ---------------------------------------------------

    @contextmanager
    def _committer(self, branch: str) -> Iterator[Conn]:
        """A connection on `branch`, held by the only committer of the database."""
        started = time.perf_counter()
        wait = self._settings.lock_timeout_s
        if not self._commit_lock.acquire(timeout=wait):
            log.warn("timed out waiting for a commit in this process", waited_s=wait)
            raise LockTimeout(
                f"another commit in this process did not finish within {wait} s",
                detail={"waited_s": wait},
            )
        try:
            with self._db.connect(branch) as conn:
                got = conn.scalar("SELECT GET_LOCK(%s, %s)", (self._lock_name, int(wait)))
                if got != 1:
                    log.warn("timed out waiting for the database commit lock", waited_s=wait)
                    raise LockTimeout(
                        f"another committer held the database for more than {wait} s",
                        detail={"waited_s": wait},
                    )
                waited_ms = (time.perf_counter() - started) * 1000.0
                if waited_ms > 100.0:
                    log.info("waited for another committer", waited_ms=round(waited_ms, 1))
                try:
                    yield conn
                finally:
                    self._unlock(conn)
        finally:
            self._commit_lock.release()

    def _unlock(self, conn: Conn) -> None:
        """Give up the database lock, after undoing anything left open."""
        if conn.broken:
            return  # the server frees the lock when the connection closes
        try:
            if conn.transacted:
                conn.rollback()
            conn.scalar("SELECT RELEASE_LOCK(%s)", (self._lock_name,))
        except Exception as exc:
            log.warn("could not release the commit lock; closing the connection", error=repr(exc))
            conn.broken = True

    def _discard(self, conn: Conn, branch: str, dirty: Any) -> None:
        """Throw away uncommitted changes found in the working set of `branch`.

        Nothing this layer does leaves any, so they come from a writer that
        bypassed it or died outside a transaction. They are never committed.
        """
        tables = sorted({row[0] for row in dirty})
        log.warn(
            "uncommitted changes found in the working set; discarding them",
            branch=branch,
            tables=tables,
        )
        conn.call("CALL DOLT_RESET('--hard')")
        conn.call("CALL DOLT_CLEAN()")
        left = conn.query("SELECT `table_name` FROM `dolt_status`")
        if left:
            log.error(
                "working set is still dirty after recovery", branch=branch, tables=sorted(left)
            )
            raise RecoveryFailed(
                f"uncommitted changes on {branch} could not be discarded",
                detail={"branch": branch, "tables": sorted(row[0] for row in left)},
            )
        log.info("working set recovered", branch=branch, discarded_tables=tables)

    def _drop_scratch(self, conn: Conn, expected: bool = True) -> None:
        exists = conn.scalar(
            "SELECT COUNT(*) FROM `dolt_branches` WHERE `name` = %s", (SCRATCH_BRANCH,)
        )
        if not exists:
            return
        if not expected:
            log.warn("removing a scratch branch left by an interrupted schema change")
        try:
            conn.call("CALL DOLT_BRANCH('-D', %s)", (SCRATCH_BRANCH,))
        except pymysql.MySQLError as exc:
            log.warn("could not remove the scratch branch", error=error_text(exc))

    def _begin(self, conn: Conn, branch: str) -> Version:
        """Open the committer's transaction on a clean working set; return the branch head."""
        for attempt in (1, 2):
            conn.begin()
            dirty = conn.query("SELECT `table_name`, `staged`, `status` FROM `dolt_status`")
            if not dirty:
                break
            conn.rollback()
            if attempt == 2:
                raise RecoveryFailed(
                    f"the working set of {branch} is being changed by another writer",
                    detail={"branch": branch, "tables": sorted({row[0] for row in dirty})},
                )
            self._discard(conn, branch, dirty)
        head = conn.scalar("SELECT HASHOF('HEAD')")
        return self._index.refresh(conn, branch, head).head

    def _seal(self, conn: Conn, branch: str, head: Version, meta: CommitMeta) -> Version:
        """Turn the open transaction into the next version of `branch`."""
        seen = conn.query("SELECT HASHOF('HEAD'), HASHOF(%s)", (branch,))[0]
        if seen[0] != head.id or seen[1] != head.id:
            conn.rollback()
            log.error(
                "branch head moved during a commit; nothing was written",
                branch=branch,
                read=head.id,
                found=seen[1],
            )
            raise HeadMoved(
                f"the head of {branch} changed while a version was being written; "
                "another writer is bypassing the store",
                detail={"branch": branch, "read": head.id, "found": seen[1], "written": False},
            )
        seq = head.seq + 1
        written_at = datetime.now(UTC)
        trace = meta.trace or current_trace_id()
        message = commit_message(meta, seq, written_at, trace)
        self._checkpoint("seal.before")
        commit = conn.call(
            "CALL DOLT_COMMIT('-A', '--allow-empty', '-m', %s, '--date', %s)",
            (message, iso_utc(meta.t)),
        )[0]
        self._checkpoint("seal.after")
        parents = tuple(
            row[0]
            for row in conn.query(
                "SELECT `parent_hash` FROM `dolt_commit_ancestors` "
                "WHERE `commit_hash` = %s ORDER BY `parent_index`",
                (commit,),
            )
        )
        if not parents or parents[0] != head.id:
            log.error(
                "version was written on top of a commit the store did not make",
                branch=branch,
                commit=commit,
                read=head.id,
                parents=list(parents),
            )
            raise HeadMoved(
                f"version {commit} of {branch} follows a commit made behind the store's back",
                detail={"branch": branch, "read": head.id, "commit": commit, "written": True},
            )
        version = Version(
            id=commit,
            seq=seq,
            t=meta.t,
            branch=branch,
            extra=dict(meta.extra),
            cause=meta.cause,
            actor=meta.actor,
            run=meta.run,
            trace=trace,
            valid_from=meta.valid_from,
            valid_to=meta.valid_to,
            written_at=written_at,
            note=meta.note,
            parents=parents,
        )
        self._index.append(version)
        return version

    # -- ontology in force --------------------------------------------------

    def _hash_of(self, conn: Conn, version: Version) -> str:
        chain = self._chain(conn, version.branch)
        boundary = chain.boundaries[bisect_right(chain.boundaries, version.seq) - 1]
        commit = chain.entries[boundary].id
        found = self._hash_at.get(commit)
        if found is None:
            found = conn.scalar(f"SELECT `hash` FROM `{ddl.ONTOLOGY_TABLE}` AS OF %s", (commit,))
            if found is None:
                raise StoreError(
                    f"version {commit} holds no ontology", code="store.corrupt_ontology"
                )
            self._hash_at[commit] = found
        return found

    def _ontology_of(self, conn: Conn, version: Version) -> Ontology:
        """The ontology in force at a version."""
        wanted = self._hash_of(conn, version)
        found = self._ontologies.get(wanted)
        if found is None:
            text = conn.scalar(
                f"SELECT `descriptor` FROM `{ddl.ONTOLOGY_TABLE}` AS OF %s", (version.id,)
            )
            found = Ontology.from_json(text)
            if found.hash != wanted:
                log.error("stored ontology does not match its hash", version=version.id)
                raise StoreError(
                    f"the ontology stored at {version.id} does not match its recorded hash",
                    code="store.corrupt_ontology",
                )
            self._ontologies[wanted] = found
        return found

    def _require_ontology(self, conn: Conn, head: Version) -> None:
        stored = self._hash_of(conn, head)
        if stored != self._ontology.hash:
            log.warn(
                "write refused: the store was given another ontology than the one in force",
                branch=head.branch,
                in_force=stored,
                given=self._ontology.hash,
            )
            raise OntologyMismatch(
                f"the ontology in force at the head of {head.branch} is not the one this "
                "store was given; migrate, or merge the live branch into this one",
                detail={"branch": head.branch, "in_force": stored, "given": self._ontology.hash},
            )

    @public
    def ontology_at(self, at: At = "head", branch: str = LIVE_BRANCH) -> Ontology:
        """The ontology that was in force at a version."""
        self._ready()
        with self._db.connect() as conn:
            return self._ontology_of(conn, self._one(conn, at, branch))

    # -- versions -----------------------------------------------------------

    @staticmethod
    def _check_branch(branch: str) -> None:
        if not isinstance(branch, str) or not BRANCH_NAME.match(branch) or COMMIT_ID.match(branch):
            raise invalid(
                "bad_branch_name",
                f"a branch name matches {BRANCH_NAME.pattern} and is not a commit id: {branch!r}",
            )

    def _chain(self, conn: Conn, branch: str) -> Chain:
        return self._index.known(branch) or self._index.refresh(conn, branch)

    def _single(self, conn: Conn, selector: Selector, branch: str, clamp: bool = False) -> Version:
        chain = self._index.known(branch)
        if selector.kind == "id":
            # A commit never changes, so one that is already known needs no look at the database.
            if chain is None or selector.id not in chain.by_id:
                chain = self._index.refresh(conn, branch)
            seq = chain.by_id.get(selector.id or "")
            if seq is None:
                raise UnknownVersion(
                    f"{selector.id} is not a version of {branch}",
                    detail={"branch": branch, "selector": str(selector)},
                )
            return chain.entries[seq]
        if selector.kind == "seq":
            wanted = selector.seq or 0
            if chain is None or wanted >= len(chain.entries):
                chain = self._index.refresh(conn, branch)
            if wanted >= len(chain.entries):
                raise UnknownVersion(
                    f"{branch} has no version #{wanted}; its head is #{chain.head.seq}",
                    detail={"branch": branch, "selector": str(selector)},
                )
            return chain.entries[wanted]
        chain = self._index.refresh(conn, branch)
        if selector.kind == "head":
            return chain.head
        position = bisect_right(chain.times, selector.time) - 1
        if position < 0:
            if clamp:
                return chain.entries[0]
            raise UnknownVersion(
                f"{branch} has no version at or before {selector}",
                detail={"branch": branch, "selector": str(selector)},
            )
        return chain.entries[position]

    def _resolve(self, conn: Conn, selector: At, branch: str) -> list[Version]:
        if isinstance(selector, VersionRef):
            self._check_branch(selector.branch)
            return [self._single(conn, Selector("id", id=selector.id), selector.branch)]
        self._check_branch(branch)
        parsed = selector if isinstance(selector, Selector) else Selector.parse(selector)
        if not parsed.is_range:
            return [self._single(conn, parsed, branch)]
        assert parsed.start is not None and parsed.end is not None
        # A range starting before the first version starts at the first.
        first = self._single(conn, parsed.start, branch, clamp=True)
        last = self._single(conn, parsed.end, branch)
        if first.seq > last.seq:
            raise invalid(
                "bad_selector", f"range {parsed} runs backwards: #{first.seq} to #{last.seq}"
            )
        chain = self._chain(conn, branch)
        return chain.entries[first.seq : last.seq + 1 : parsed.step]

    def _one(self, conn: Conn, selector: At, branch: str) -> Version:
        found = self._resolve(conn, selector, branch)
        if isinstance(selector, VersionRef) or len(found) == 1 and not self._is_range(selector):
            return found[0]
        raise invalid("bad_selector", f"one version is needed here, not a range: {selector}")

    @staticmethod
    def _is_range(selector: At) -> bool:
        if isinstance(selector, Selector):
            return selector.is_range
        return isinstance(selector, str) and ".." in selector

    @public
    def resolve(self, selector: At, branch: str = LIVE_BRANCH) -> list[VersionRef]:
        """The versions a selector names on a branch, oldest first."""
        self._ready()
        with self._db.connect() as conn:
            return [version.ref for version in self._resolve(conn, selector, branch)]

    @public
    def head(self, branch: str = LIVE_BRANCH) -> VersionRef:
        return self.resolve("head", branch)[0]

    @public
    def versions(
        self,
        selector: At = ALL_VERSIONS,
        branch: str = LIVE_BRANCH,
        *,
        cause: Cause | str | None = None,
        actor: str | None = None,
        run: str | None = None,
        extra: Mapping[str, Any] | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Version]:
        """The commit log of a branch with everything recorded about each version.

        `extra` keeps the versions whose caller-defined fields hold each given
        top-level key with an equal value.
        """
        self._ready()
        try:
            wanted_cause = None if cause is None else Cause(cause)
        except ValueError as exc:
            raise invalid("bad_filter", f"unknown cause {cause!r}") from exc
        if extra is not None and not isinstance(extra, Mapping):
            raise invalid("bad_filter", "the extra filter is a mapping of key to value")
        with self._db.connect() as conn:
            found = self._resolve(conn, selector, branch)
        missing = object()
        kept = [
            version
            for version in found
            if (wanted_cause is None or version.cause == wanted_cause)
            and (actor is None or version.actor == actor)
            and (run is None or version.run == run)
            and (
                extra is None
                or all(version.extra.get(key, missing) == value for key, value in extra.items())
            )
        ]
        if newest_first:
            kept.reverse()
        return kept if limit is None else kept[: max(0, limit)]

    # -- writing ------------------------------------------------------------

    def _check_write(self, changes: ChangeSet, meta: CommitMeta, branch: str) -> None:
        if not isinstance(changes, ChangeSet):
            raise invalid("invalid_change", "changes must be a ChangeSet")
        if not isinstance(meta, CommitMeta):
            raise invalid("invalid_meta", "meta must be a CommitMeta")
        self._check_branch(branch)
        if meta.cause in _OWN_OPERATION:
            raise invalid(
                "invalid_meta",
                f"cause {meta.cause.value} is written only by {_OWN_OPERATION[meta.cause]}()",
            )

    @staticmethod
    def _check_time(meta: CommitMeta, head: Version) -> None:
        if meta.t < head.t:
            log.warn(
                "write refused: version time is earlier than the branch head",
                branch=head.branch,
                given=iso_utc(meta.t),
                head=iso_utc(head.t),
            )
            raise TimeRegression(
                f"version time {iso_utc(meta.t)} is earlier than {iso_utc(head.t)}, the time of "
                f"the head of {head.branch}; record a late fact at the current time with an "
                "earlier t_valid",
                detail={"branch": head.branch, "given": iso_utc(meta.t), "head": iso_utc(head.t)},
            )

    @public
    def validate(
        self, changes: ChangeSet, meta: CommitMeta, branch: str = LIVE_BRANCH
    ) -> list[Violation]:
        """Every reason `commit` would refuse this change set; writes nothing.

        The change set is applied inside a transaction that is always rolled
        back, so the constraints held by the database are checked as well.
        """
        self._ready()
        self._check_write(changes, meta, branch)
        with self._db.connect(branch) as conn:
            conn.begin()
            try:
                head = self._index.refresh(conn, branch, conn.scalar("SELECT HASHOF('HEAD')")).head
                self._require_ontology(conn, head)
                applied = apply_changes(
                    conn, self._ontology, self._validator, changes, meta.cause, branch
                )
            finally:
                if not conn.broken:
                    conn.rollback()
        log.debug("change set validated", branch=branch, violations=len(applied.violations))
        return applied.violations

    @public
    def commit(self, changes: ChangeSet, meta: CommitMeta, branch: str = LIVE_BRANCH) -> VersionRef:
        """Write the change set as the next version of `branch`, or change nothing.

        Raises `store.violation` carrying every violation found.
        """
        self._ready()
        self._check_write(changes, meta, branch)
        started = time.perf_counter()
        with self._committer(branch) as conn:
            head = self._begin(conn, branch)
            self._require_ontology(conn, head)
            self._check_time(meta, head)
            self._checkpoint("commit.begun")
            applied = apply_changes(
                conn, self._ontology, self._validator, changes, meta.cause, branch
            )
            if applied.violations:
                conn.rollback()
                log.warn(
                    "change set refused",
                    branch=branch,
                    cause=meta.cause.value,
                    violations=len(applied.violations),
                    rules=sorted({violation.rule for violation in applied.violations}),
                    first=[
                        (violation.type, violation.uid, violation.property, violation.rule)
                        for violation in applied.violations[:5]
                    ],
                )
                raise ViolationError(applied.violations, branch=branch)
            self._checkpoint("commit.rows_written")
            version = self._seal(conn, branch, head, meta)
        log.info(
            "version committed",
            branch=branch,
            seq=version.seq,
            version=version.id,
            cause=meta.cause.value,
            actor=meta.actor,
            created=applied.created,
            replaced=applied.replaced,
            updated=applied.updated,
            deleted=applied.deleted,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return version.ref

    # -- branches -----------------------------------------------------------

    @public
    def branches(self) -> list[BranchInfo]:
        self._ready()
        with self._db.connect() as conn:
            rows = conn.query("SELECT `name`, `hash` FROM `dolt_branches` ORDER BY `name`")
        return [BranchInfo(name, head) for name, head in rows if name != SCRATCH_BRANCH]

    @public
    def branch_create(self, name: str, at: At = "head", source: str = LIVE_BRANCH) -> VersionRef:
        """Create a branch starting at a version of `source`; returns that version."""
        self._ready()
        self._check_branch(name)
        with self._db.connect() as conn:
            start = self._one(conn, at, source)
            try:
                conn.call("CALL DOLT_BRANCH(%s, %s)", (name, start.id))
            except pymysql.MySQLError as exc:
                if "already exists" in error_text(exc):
                    raise BranchExists(
                        f"a branch named {name} already exists", detail={"branch": name}
                    ) from exc
                raise
        self._index.forget(name)
        log.info("branch created", branch=name, source=start.branch, at_seq=start.seq, at=start.id)
        return VersionRef(start.id, start.seq, start.t, name, start.extra)

    @public
    def branch_delete(self, name: str) -> None:
        self._ready()
        self._check_branch(name)
        if name == LIVE_BRANCH:
            raise ProtectedBranch(f"{LIVE_BRANCH} is the live branch and cannot be deleted")
        with self._committer(LIVE_BRANCH) as conn:
            try:
                conn.call("CALL DOLT_BRANCH('-D', %s)", (name,))
            except pymysql.MySQLError as exc:
                if "not found" in error_text(exc):
                    raise UnknownBranch(f"no branch named {name}", detail={"branch": name}) from exc
                raise
        self._index.forget(name)
        log.info("branch deleted", branch=name)

    @public
    def merge(self, source: str, into: str, meta: CommitMeta) -> VersionRef:
        """Merge `source` into `into` as one new version of `into`, or change nothing.

        A three-way merge. Changes to the same cell on both sides refuse the
        whole merge. A merge into the live branch is refused when it would
        bring in a simulated fact, or an observed one.
        """
        self._ready()
        if not isinstance(meta, CommitMeta) or meta.cause != Cause.merge:
            raise invalid("invalid_meta", "a merge is recorded with cause merge")
        self._check_branch(source)
        self._check_branch(into)
        if source == into:
            raise invalid("bad_branch_name", "a branch cannot be merged into itself")
        started = time.perf_counter()
        with self._committer(into) as conn:
            head = self._begin(conn, into)
            if into == LIVE_BRANCH:
                self._require_ontology(conn, head)
            self._check_time(meta, head)
            merged = self._index.refresh(conn, source).head
            try:
                conn.call("CALL DOLT_MERGE('--no-commit', '--no-ff', %s)", (source,))
            except pymysql.MySQLError as exc:
                conn.rollback()
                log.warn("merge refused by the database", source=source, into=into, exc=str(exc))
                raise MergeConflict(
                    f"{source} cannot be merged into {into}: {error_text(exc)}",
                    detail={"source": source, "into": into, "tables": []},
                ) from exc
            conflicts = conn.query("SELECT `table`, `num_conflicts` FROM `dolt_conflicts`")
            broken = conn.query(
                "SELECT `table`, `num_violations` FROM `dolt_constraint_violations`"
            )
            merging = conn.scalar("SELECT `is_merging` FROM `dolt_merge_status`")
            if conflicts or broken:
                conn.rollback()
                tables = sorted({row[0] for row in conflicts} | {row[0] for row in broken})
                log.warn(
                    "merge refused: the branches conflict",
                    source=source,
                    into=into,
                    conflicts=dict(conflicts),
                    constraint_violations=dict(broken),
                )
                raise MergeConflict(
                    f"{source} conflicts with {into} in {', '.join(tables)}; nothing was merged",
                    detail={
                        "source": source,
                        "into": into,
                        "tables": tables,
                        "conflicts": dict(conflicts),
                        "constraint_violations": dict(broken),
                    },
                )
            if not merging:
                conn.rollback()
                raise NothingToMerge(
                    f"{into} already holds everything on {source}",
                    detail={"source": source, "into": into},
                )
            stored_hash = conn.scalar(f"SELECT `hash` FROM `{ddl.ONTOLOGY_TABLE}`")
            ontology = self._ontologies.get(stored_hash)
            if ontology is None:
                ontology = Ontology.from_json(
                    conn.scalar(f"SELECT `descriptor` FROM `{ddl.ONTOLOGY_TABLE}`")
                )
                self._ontologies[stored_hash] = ontology
            violations = check_merged(conn, ontology, into)
            if violations:
                conn.rollback()
                log.warn(
                    "merge refused: the merged rows break a rule",
                    source=source,
                    into=into,
                    violations=len(violations),
                    rules=sorted({violation.rule for violation in violations}),
                )
                raise ViolationError(violations, branch=into)
            self._checkpoint("merge.rows_merged")
            version = self._seal(conn, into, head, meta)
        if len(version.parents) < 2 or version.parents[1] != merged.id:
            log.warn(
                "merge commit does not name the source head that was read",
                version=version.id,
                parents=list(version.parents),
                source_head=merged.id,
            )
        log.info(
            "branches merged",
            source=source,
            into=into,
            seq=version.seq,
            version=version.id,
            source_head=merged.id,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return version.ref

    # -- table definitions --------------------------------------------------

    def _schema_version(
        self,
        conn: Conn,
        base: str,
        seq: int,
        meta: CommitMeta,
        apply: Callable[[Conn], None],
    ) -> str:
        """Build a commit that changes table definitions and move the live branch onto it.

        The work happens on a scratch branch, so the live branch and its
        working set are untouched until the single step that publishes it.
        """
        self._drop_scratch(conn, expected=False)
        conn.call("CALL DOLT_BRANCH(%s, %s)", (SCRATCH_BRANCH, base))
        self._checkpoint("schema.branch_created")
        try:
            with self._db.connect(SCRATCH_BRANCH) as scratch:
                apply(scratch)
                self._checkpoint("schema.applied")
                written_at = datetime.now(UTC)
                message = commit_message(meta, seq, written_at, meta.trace or current_trace_id())
                commit = scratch.call(
                    "CALL DOLT_COMMIT('-A', '--allow-empty', '-m', %s, '--date', %s)",
                    (message, iso_utc(meta.t)),
                )[0]
                recorded = scratch.scalar(
                    "SELECT `date` FROM `dolt_log` WHERE `commit_hash` = %s", (commit,)
                )
            if recorded is None or recorded.replace(tzinfo=UTC) != meta.t:
                log.error(
                    "the database returns commit times in another zone than UTC",
                    written=iso_utc(meta.t),
                    read_back=str(recorded),
                )
                raise StoreError(
                    "the database server does not keep commit times in UTC",
                    code="store.server_timezone",
                    detail={"written": iso_utc(meta.t), "read_back": str(recorded)},
                )
            self._checkpoint("schema.committed")
            try:
                conn.call("CALL DOLT_MERGE('--ff-only', %s)", (SCRATCH_BRANCH,))
            except pymysql.MySQLError as exc:
                log.error("live branch could not be moved onto the new schema", exc=exc)
                raise HeadMoved(
                    f"{LIVE_BRANCH} changed while its schema was being changed: {error_text(exc)}",
                    detail={"branch": LIVE_BRANCH, "read": base, "written": False},
                ) from exc
            self._checkpoint("schema.published")
        finally:
            if not conn.broken:
                self._drop_scratch(conn)
        return commit

    @public
    def install(
        self,
        twin_id: str,
        t: datetime | str = EPOCH,
        actor: str = "system",
        note: str = "",
    ) -> VersionRef:
        """Create the database and every table as version 0.

        `t` is the version time everything later must not precede. Installing
        again with the same ontology does nothing and returns the head.
        """
        if self._closed:
            raise StoreClosed("the store has been closed")
        if not isinstance(twin_id, str) or not twin_id or len(twin_id) > 190:
            raise invalid("invalid_meta", "twin_id must be text of at most 190 characters")
        meta = CommitMeta(cause=Cause.install, actor=actor, t=t, note=note)
        statements = ddl.install_statements(self._ontology)
        started = time.perf_counter()
        with self._db.server() as server:
            server.execute(f"CREATE DATABASE IF NOT EXISTS `{self._settings.database}`")
        with self._committer(LIVE_BRANCH) as conn:
            base = self._index.head_id(conn, LIVE_BRANCH)
            try:
                head = self._index.refresh(conn, LIVE_BRANCH, base).head
            except NotInstalled:
                head = None
            if head is not None:
                self._require_ontology(conn, head)
                log.info("twin is already installed", head_seq=head.seq, head=head.id)
                return head.ref
            dirty = conn.query("SELECT `table_name`, `staged`, `status` FROM `dolt_status`")
            if dirty:
                self._discard(conn, LIVE_BRANCH, dirty)

            def apply(scratch: Conn) -> None:
                for statement in statements:
                    try:
                        scratch.execute(statement)
                    except pymysql.MySQLError as exc:
                        subject = statement.split("(", 1)[0].strip()
                        log.error("table definition refused", exc=exc, statement=subject)
                        raise InvalidOntology(
                            f"the database refused {subject}: {error_text(exc)}",
                            detail={"statement": subject},
                        ) from exc
                scratch.execute(
                    f"INSERT INTO `{ddl.ONTOLOGY_TABLE}` (`id`, `hash`, `descriptor`, `source`) "
                    "VALUES (1, %s, %s, %s)",
                    (self._ontology.hash, self._ontology.to_json(), self._ontology.source),
                )
                scratch.execute(
                    f"INSERT INTO `{ddl.META_TABLE}` (`id`, `format`, `twin_id`) "
                    "VALUES (1, %s, %s)",
                    (ddl.FORMAT_VERSION, twin_id),
                )
                scratch.execute(
                    "INSERT INTO `dolt_ignore` VALUES (%s, %s)",
                    (ddl.SCRATCH_TABLE_PREFIX + "%", 1),
                )

            commit = self._schema_version(conn, base, 0, meta, apply)
            self._index.forget(LIVE_BRANCH)
            version = self._index.refresh(conn, LIVE_BRANCH).head
        if version.id != commit:
            raise HeadMoved(
                f"{LIVE_BRANCH} is not at the installed schema",
                detail={"branch": LIVE_BRANCH, "read": commit, "found": version.id},
            )
        log.info(
            "twin installed",
            database=self._settings.database,
            twin_id=twin_id,
            version=version.id,
            types=len(self._ontology.types),
            ontology=self._ontology.hash,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return version.ref

    @public
    def migrate(
        self,
        ontology: Ontology,
        meta: CommitMeta,
        validator: RowValidator | None = None,
    ) -> VersionRef:
        """Move the live branch to a new ontology that only adds to the current one.

        One new version with cause `migration`. On success the store works
        with the new ontology, and with `validator` when one is given.
        Raises `store.migration_required` listing every change that is not
        additive; nothing is changed in that case.
        """
        self._ready()
        if not isinstance(meta, CommitMeta) or meta.cause != Cause.migration:
            raise invalid("invalid_meta", "a migration is recorded with cause migration")
        ddl.check_ontology(ontology)
        started = time.perf_counter()
        with self._committer(LIVE_BRANCH) as conn:
            head = self._begin(conn, LIVE_BRANCH)
            conn.rollback()
            current = self._ontology_of(conn, head)
            if current.hash == ontology.hash:
                self._adopt(ontology, validator)
                log.info("migration skipped: this ontology is already in force", head=head.id)
                return head.ref
            self._check_time(meta, head)
            plan = plan_migration(current, ontology)
            if plan.reasons:
                log.warn("migration refused: not additive", reasons=plan.reasons)
                raise MigrationRequired(plan.reasons)

            def apply(scratch: Conn) -> None:
                for statement in plan.statements:
                    scratch.execute(statement)
                broken = []
                for description, statement in plan.checks:
                    try:
                        scratch.execute(statement)
                    except pymysql.MySQLError as exc:
                        broken.append(f"{description} cannot be added: {error_text(exc)}")
                if broken:
                    log.warn("migration refused: stored rows break a new check", reasons=broken)
                    raise MigrationRequired(broken)
                scratch.execute(
                    f"UPDATE `{ddl.ONTOLOGY_TABLE}` SET `hash` = %s, `descriptor` = %s, "
                    "`source` = %s WHERE `id` = 1",
                    (ontology.hash, ontology.to_json(), ontology.source),
                )

            commit = self._schema_version(conn, head.id, head.seq + 1, meta, apply)
            version = self._index.refresh(conn, LIVE_BRANCH).head
            self._adopt(ontology, validator)
        if version.id != commit:
            raise HeadMoved(
                f"{LIVE_BRANCH} is not at the migrated schema",
                detail={"branch": LIVE_BRANCH, "read": commit, "found": version.id},
            )
        log.info(
            "ontology migrated",
            seq=version.seq,
            version=version.id,
            ontology=ontology.hash,
            new_types=plan.new_types,
            new_properties=plan.new_properties,
            widened_enums=plan.widened_enums,
            new_checks=[description for description, _ in plan.checks],
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return version.ref

    def _adopt(self, ontology: Ontology, validator: RowValidator | None) -> None:
        self._ontologies[ontology.hash] = ontology
        self._ontology = ontology
        self._validator = validator or DescriptorValidator(ontology)


__all__ = ["BRANCH_NAME", "SCRATCH_BRANCH", "Store"]
