"""The structured log lines the stack's services write, newest first.

Each service appends to `<logs dir>/<service>.jsonl`. Files are read from the
end in blocks and merged by time, and reading stops as soon as enough lines
are found, so a long log costs no more than the lines returned.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dta_core.log import Log

from ..settings import LOG_FILE_SUFFIX

log = Log("api.logs")

LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40, "critical": 50}
_BLOCK = 64 * 1024


@dataclass(frozen=True)
class LogQuery:
    level: str | None = None
    since: datetime | None = None
    context: str | None = None
    trace_id: str | None = None
    services: Sequence[str] | None = None
    limit: int = 200


@dataclass
class Skipped:
    count: int = 0


def _backwards(path: Path) -> Iterator[bytes]:
    """The lines of a file, last first, read in blocks from the end."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        rest = b""
        while position > 0:
            size = min(_BLOCK, position)
            position -= size
            handle.seek(position)
            lines = (handle.read(size) + rest).split(b"\n")
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if rest.strip():
            yield rest


def _moment(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _records(
    path: Path, since: datetime | None, skipped: Skipped
) -> Iterator[tuple[datetime, dict[str, Any]]]:
    """(time, record) of each log line of one file, newest first, down to `since`."""
    service = path.name.removesuffix(LOG_FILE_SUFFIX)
    for line in _backwards(path):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            skipped.count += 1
            continue
        moment = _moment(record.get("timestamp")) if isinstance(record, dict) else None
        if moment is None:
            skipped.count += 1
            continue
        if since is not None and moment < since:
            return
        yield moment, {**record, "service": service}


def _matches(record: dict[str, Any], query: LogQuery) -> bool:
    if query.level is not None:
        level = LEVELS.get(str(record.get("level", "")).lower(), 0)
        if level < LEVELS[query.level]:
            return False
    if query.context is not None and not str(record.get("context", "")).startswith(query.context):
        return False
    return query.trace_id is None or record.get("trace_id") == query.trace_id


def read_logs(logs_dir: Path, query: LogQuery) -> tuple[list[dict[str, Any]], int]:
    """Matching log lines, newest first, and how many lines were not log records."""
    skipped = Skipped()
    if not logs_dir.is_dir():
        log.warn("the logs directory does not exist", path=str(logs_dir))
        return [], 0
    wanted = set(query.services) if query.services else None
    paths = sorted(
        path
        for path in logs_dir.glob(f"*{LOG_FILE_SUFFIX}")
        if path.is_file() and (wanted is None or path.name.removesuffix(LOG_FILE_SUFFIX) in wanted)
    )
    sources = [_records(path, query.since, skipped) for path in paths]
    found: list[dict[str, Any]] = []
    try:
        for _, record in heapq.merge(*sources, key=lambda item: item[0], reverse=True):
            if _matches(record, query):
                found.append(record)
                if len(found) >= query.limit:
                    break
    except OSError as exc:
        log.error("a log file could not be read", exc=exc, path=str(logs_dir))
        raise
    finally:
        for source in sources:
            source.close()
    return found, skipped.count
