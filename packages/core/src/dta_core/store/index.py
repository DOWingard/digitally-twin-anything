"""The versions of each branch, in order, kept in memory.

A version is a commit on the first-parent line of its branch, numbered from
the commit that installed the schema. Commits never change, so everything
learnt about one stays true; only the head of a branch moves. A refresh
therefore reads just the commits added since the last one, and checks that
they follow from the head already known. Because a commit id covers the ids
of its parents, that single check proves every earlier version is still at
its position.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from typing import Any

import pymysql

from ..log import Log
from .db import Conn, error_text
from .errors import ForeignCommit, HistoryRewritten, NotInstalled, UnknownBranch
from .model import LIVE_BRANCH, Cause, CommitMeta, Version, iso_utc

log = Log("store.index")

MESSAGE_FORMAT = 1
# Causes after which the ontology in force may differ from the version before.
_ONTOLOGY_MAY_CHANGE = (Cause.install, Cause.migration, Cause.merge)
_COMMIT_ID = re.compile(r"[0-9a-v]{32}")
_LOG = "SELECT `commit_hash`, `date`, `message`, `parents` FROM DOLT_LOG(%s, '--parents')"


def commit_message(meta: CommitMeta, seq: int, written_at: datetime, trace: str | None) -> str:
    return json.dumps(
        {
            "f": MESSAGE_FORMAT,
            "seq": seq,
            "cause": meta.cause.value,
            "actor": meta.actor,
            "run": meta.run,
            "trace": trace,
            "valid_from": iso_utc(meta.valid_from) if meta.valid_from else None,
            "valid_to": iso_utc(meta.valid_to) if meta.valid_to else None,
            "written_at": iso_utc(written_at),
            "note": meta.note,
            "x": dict(meta.extra),
        },
        separators=(",", ":"),
    )


def _optional_time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def parse_version(
    branch: str, position: int, commit: str, date: datetime, message: str, parents: tuple[str, ...]
) -> Version:
    """The version recorded by one commit; refuses a commit this layer did not write."""
    try:
        document = json.loads(message)
        if not isinstance(document, dict) or document.get("f") != MESSAGE_FORMAT:
            raise ValueError("not a version record")
        if document.get("seq") != position:
            raise ValueError(f"records position {document.get('seq')!r}, found at {position}")
        extra = document.get("x") or {}
        if not isinstance(extra, dict):
            raise ValueError("extra fields are not an object")
        return Version(
            id=commit,
            seq=position,
            t=date.replace(tzinfo=UTC),
            branch=branch,
            extra=extra,
            cause=Cause(document["cause"]),
            actor=str(document["actor"]),
            run=document.get("run"),
            trace=document.get("trace"),
            valid_from=_optional_time(document.get("valid_from")),
            valid_to=_optional_time(document.get("valid_to")),
            written_at=_optional_time(document.get("written_at")),
            note=document.get("note") or "",
            parents=parents,
        )
    except (ValueError, KeyError, TypeError) as exc:
        log.error(
            "commit on the branch was not written by the store",
            exc=exc,
            branch=branch,
            commit=commit,
            position=position,
        )
        raise ForeignCommit(
            f"commit {commit} at position {position} of {branch} was not written by the store: "
            f"{exc}",
            detail={"branch": branch, "commit": commit, "position": position},
        ) from exc


class Chain:
    """The versions of one branch, oldest first."""

    def __init__(self, branch: str, root: str) -> None:
        self.branch = branch
        self.root = root  # the empty commit the database was created with
        self.entries: list[Version] = []
        self.by_id: dict[str, int] = {}
        self.times: list[datetime] = []
        self.boundaries: list[int] = []

    @property
    def head(self) -> Version:
        return self.entries[-1]

    def parent_of(self, seq: int) -> str:
        return self.entries[seq - 1].id if seq > 0 else self.root

    def add(self, version: Version) -> None:
        if self.times and version.t < self.times[-1]:
            raise ForeignCommit(
                f"version #{version.seq} of {self.branch} is earlier in time than the one before",
                detail={"branch": self.branch, "commit": version.id, "position": version.seq},
            )
        self.entries.append(version)
        self.by_id[version.id] = version.seq
        self.times.append(version.t)
        if version.cause in _ONTOLOGY_MAY_CHANGE:
            self.boundaries.append(version.seq)


def _first_parent_line(
    rows: tuple[tuple[Any, ...], ...], head: str, stop: str | None
) -> list[tuple[str, datetime, str, tuple[str, ...]]] | None:
    """Rows from just after `stop` up to `head` along first parents, oldest first.

    With `stop` None the line runs to the commit without parents, which is
    returned as the first row. None means `stop` is not on the line.
    """
    by_id = {row[0]: row for row in rows}
    line = []
    current = head
    while current != stop:
        row = by_id.get(current)
        if row is None:
            return None
        parents = tuple(_COMMIT_ID.findall(row[3] or ""))
        line.append((row[0], row[1], row[2], parents))
        if not parents:
            if stop is None:
                break
            return None
        current = parents[0]
    line.reverse()
    return line


class VersionIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._chains: dict[str, Chain] = {}

    def head_id(self, conn: Conn, branch: str) -> str:
        head = conn.scalar("SELECT `hash` FROM `dolt_branches` WHERE `name` = %s", (branch,))
        if head is None:
            with self._lock:
                self._chains.pop(branch, None)
            raise UnknownBranch(f"no branch named {branch}", detail={"branch": branch})
        return head

    def known(self, branch: str) -> Chain | None:
        return self._chains.get(branch)

    def forget(self, branch: str) -> None:
        with self._lock:
            self._chains.pop(branch, None)

    def refresh(self, conn: Conn, branch: str, head: str | None = None) -> Chain:
        """The chain of `branch` brought up to its current head."""
        if head is None:
            head = self.head_id(conn, branch)
        with self._lock:
            chain = self._chains.get(branch)
            if chain is not None and chain.entries and chain.head.id == head:
                return chain
            if chain is None:
                chain = self._load(conn, branch, head)
            else:
                chain = self._extend(conn, chain, head)
            self._chains[branch] = chain
            return chain

    def append(self, version: Version) -> None:
        """Record a version this process has just committed."""
        with self._lock:
            chain = self._chains.get(version.branch)
            if chain is None or not version.parents or chain.head.id != version.parents[0]:
                # The next refresh reads it from the database instead.
                return
            chain.add(version)

    # -- loading ------------------------------------------------------------

    def _load(self, conn: Conn, branch: str, head: str) -> Chain:
        rows = conn.query(_LOG, (head,))
        line = _first_parent_line(rows, head, None)
        if not line:
            raise ForeignCommit(
                f"the history of {branch} cannot be followed from {head}",
                detail={"branch": branch, "commit": head},
            )
        root, versions = line[0], line[1:]
        if not versions:
            raise NotInstalled(
                f"branch {branch} holds no installed twin", detail={"branch": branch}
            )
        chain = Chain(branch, root[0])
        for position, (commit, date, message, parents) in enumerate(versions):
            chain.add(parse_version(branch, position, commit, date, message, parents))
        log.debug("version index loaded", branch=branch, versions=len(chain.entries))
        return chain

    def _extend(self, conn: Conn, chain: Chain, head: str) -> Chain:
        known = chain.head
        try:
            rows = conn.query(_LOG, (f"{known.id}..{head}",))
            line = _first_parent_line(rows, head, known.id)
        except pymysql.MySQLError as exc:
            # The head the index knew no longer exists in the database.
            log.warn("known head cannot be read back", branch=chain.branch, error=error_text(exc))
            line = None
        if line is None:
            return self._rewritten(conn, chain, head)
        for offset, (commit, date, message, parents) in enumerate(line):
            position = known.seq + 1 + offset
            chain.add(parse_version(chain.branch, position, commit, date, message, parents))
        log.debug("version index extended", branch=chain.branch, added=len(line))
        return chain

    def _rewritten(self, conn: Conn, chain: Chain, head: str) -> Chain:
        known = chain.head
        if chain.branch == LIVE_BRANCH:
            log.error(
                "history of the live branch was rewritten",
                branch=chain.branch,
                known_head=known.id,
                known_seq=known.seq,
                found_head=head,
            )
            raise HistoryRewritten(
                f"version #{known.seq} ({known.id}) is no longer on {chain.branch}; "
                f"its head is now {head}",
                detail={
                    "branch": chain.branch,
                    "known_head": known.id,
                    "known_seq": known.seq,
                    "found_head": head,
                },
            )
        # Any other branch can be deleted and created again under the same
        # name, which looks the same from here, so its versions are re-read.
        log.warn(
            "branch no longer continues from the head that was known; reading it again",
            branch=chain.branch,
            known_head=known.id,
            found_head=head,
        )
        return self._load(conn, chain.branch, head)
