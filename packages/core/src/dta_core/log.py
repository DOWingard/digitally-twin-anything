"""Structured JSON logging shared by every package.

One line per event with the fields `timestamp`, `level`, `context`, `message`,
`trace_id`, `error`, `stack` and, when given, `data`. Values under keys that
look like credentials are masked before they are written.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = "dta"
_LEVEL_ENV = "DTA_LOG_LEVEL"
_LEVEL_NAMES = {"WARNING": "warn", "CRITICAL": "error"}
_SECRET_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|credential", re.I)
_MASK = "***"

# Correlates every line written while one request or command is being served.
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("dta_trace_id", default=None)


def new_trace_id() -> str:
    """A fresh id in the W3C trace-id shape (32 lowercase hex characters)."""
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    return _trace_id.get()


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace id to everything logged inside the block."""
    value = trace_id or new_trace_id()
    token = _trace_id.set(value)
    try:
        yield value
    finally:
        _trace_id.reset(token)


def bind_trace(trace_id: str) -> str:
    """Bind a trace id for the rest of the process or task; returns it."""
    _trace_id.set(trace_id)
    return trace_id


def redact(value: Any) -> Any:
    """Mask values stored under credential-like keys, at any depth."""
    if isinstance(value, dict):
        return {
            key: _MASK if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": _LEVEL_NAMES.get(record.levelname, record.levelname.lower()),
            "context": getattr(record, "context", record.name),
            "message": record.getMessage(),
            "trace_id": _trace_id.get(),
            "error": None,
            "stack": None,
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["stack"] = "".join(traceback.format_exception(*record.exc_info))
        data = getattr(record, "data", None)
        if data:
            entry["data"] = redact(data)
        return json.dumps(entry, default=str)


def configure(log_file: Path | None = None, level: str | None = None) -> None:
    """Send logs to stderr and, when a path is given, to that file as well.

    The level comes from the argument, then `DTA_LOG_LEVEL`, then `info`.
    """
    root = logging.getLogger(_ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = False
    formatter = _JsonFormatter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    requested = (level or os.environ.get(_LEVEL_ENV) or "info").upper()
    requested = {"WARN": "WARNING"}.get(requested, requested)
    numeric = logging.getLevelName(requested)
    if isinstance(numeric, int):
        root.setLevel(numeric)
    else:
        root.setLevel(logging.INFO)
        Log("log").warn("unknown log level, using info", given=requested)


class Log:
    """Logger bound to one context name; keyword arguments become the `data` field."""

    def __init__(self, context: str) -> None:
        self._logger = logging.getLogger(_ROOT)
        self._context = context

    def _emit(self, level: int, message: str, exc: BaseException | None, data: dict) -> None:
        self._logger.log(
            level, message, exc_info=exc, extra={"context": self._context, "data": data}
        )

    def debug(self, message: str, **data: Any) -> None:
        self._emit(logging.DEBUG, message, None, data)

    def info(self, message: str, **data: Any) -> None:
        self._emit(logging.INFO, message, None, data)

    def warn(self, message: str, **data: Any) -> None:
        self._emit(logging.WARNING, message, None, data)

    def error(self, message: str, exc: BaseException | None = None, **data: Any) -> None:
        self._emit(logging.ERROR, message, exc, data)
