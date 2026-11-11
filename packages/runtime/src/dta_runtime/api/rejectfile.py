"""The rows a sync run set aside, read back from the file its record names.

The file holds one JSON object per line and may hold millions of lines, so it
is only ever read as a stream. A page needs the number of lines that pass the
filters, which takes one pass over the file; that count is kept for the file
as it is (its size and modification time), and a cursor carries the byte at
which its page starts, so the pages after the first read only what they return.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dta_core.log import Log

from .problems import ApiError

log = Log("api.rejects")

COUNTS_KEPT = 32


@dataclass(frozen=True)
class RejectsPage:
    lines: list[dict[str, Any]]
    next_position: dict[str, Any] | None
    total: int
    skipped: int


def locate(artifacts_dir: Path, location: str) -> Path | None:
    """The file a run's record names, or None when the name leads out of the
    artifacts directory: absolute, climbing out, or through a link pointing out."""
    if not location or Path(location).is_absolute():
        return None
    root = artifacts_dir.resolve()
    # Resolving follows every link, so a link inside that points out is caught too.
    resolved = (root / location).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        found = json.loads(line)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _bad_cursor() -> ApiError:
    return ApiError("the cursor is not one this API issued", code="api.bad_cursor")


@dataclass(frozen=True)
class _Filter:
    rule: str | None
    mapping: str | None

    def passes(self, record: Mapping[str, Any]) -> bool:
        return (self.rule is None or record.get("rule") == self.rule) and (
            self.mapping is None or record.get("mapping") == self.mapping
        )


class RejectsReader:
    def __init__(self, kept: int = COUNTS_KEPT) -> None:
        self._kept = kept
        self._lock = threading.Lock()
        # (path, file identity, rule, mapping) -> (lines that pass, lines that are not objects)
        self._counts: OrderedDict[tuple[str, str, str | None, str | None], tuple[int, int]] = (
            OrderedDict()
        )

    def page(
        self,
        path: Path,
        *,
        rule: str | None,
        mapping: str | None,
        limit: int,
        position: Any,
    ) -> RejectsPage:
        """The lines from `position` on that pass the filters.

        Raises OSError when the file cannot be read.
        """
        status = path.stat()
        identity = f"{status.st_size}:{status.st_mtime_ns}"
        index, offset = self._start(position, identity, status.st_size)
        wanted = _Filter(rule, mapping)
        key = (str(path), identity, rule, mapping)
        with self._lock:
            counts = self._counts.get(key)
            if counts is not None:
                self._counts.move_to_end(key)
        with path.open("rb") as handle:
            if offset and not self._at_line_start(handle, offset):
                raise _bad_cursor()
            if counts is None:
                lines, end, total, skipped = self._whole(handle, wanted, index, limit)
                with self._lock:
                    self._counts[key] = (total, skipped)
                    while len(self._counts) > self._kept:
                        self._counts.popitem(last=False)
            else:
                total, skipped = counts
                handle.seek(offset)
                lines, end = self._from(handle, wanted, offset, limit)
        following = None
        if index + len(lines) < total and lines:
            following = {"p": index + len(lines), "o": end, "f": identity}
        return RejectsPage(lines, following, total, skipped)

    @staticmethod
    def _start(position: Any, identity: str, size: int) -> tuple[int, int]:
        """(index among the lines that pass, byte offset) a page starts at."""
        if position is None:
            return 0, 0
        if not isinstance(position, Mapping) or position.get("f") != identity:
            # A cursor of another file, or of this file before it changed.
            raise _bad_cursor()
        index, offset = position.get("p"), position.get("o")
        for number in (index, offset):
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise _bad_cursor()
        assert isinstance(index, int) and isinstance(offset, int)
        if offset > size:
            raise _bad_cursor()
        return index, offset

    @staticmethod
    def _at_line_start(handle: Any, offset: int) -> bool:
        handle.seek(offset - 1)
        return bool(handle.read(1) == b"\n")

    @staticmethod
    def _whole(
        handle: Any, wanted: _Filter, index: int, limit: int
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """One pass over the file: the page from `index`, where it ends, and the counts."""
        handle.seek(0)
        lines: list[dict[str, Any]] = []
        position = end = total = skipped = 0
        for line in handle:
            position += len(line)
            if not line.strip():
                continue
            record = _record(line)
            if record is None:
                skipped += 1
                continue
            if not wanted.passes(record):
                continue
            if index <= total < index + limit:
                lines.append(record)
                end = position
            total += 1
        return lines, end, total, skipped

    @staticmethod
    def _from(
        handle: Any, wanted: _Filter, offset: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Up to `limit` lines that pass, from a byte offset, and the byte after the last."""
        lines: list[dict[str, Any]] = []
        position = end = offset
        for line in handle:
            position += len(line)
            record = _record(line) if line.strip() else None
            if record is None or not wanted.passes(record):
                continue
            lines.append(record)
            end = position
            if len(lines) >= limit:
                break
        return lines, end
