"""Structured JSON logging shared by every module of the benchmark."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# One id per process so every line of a benchmark run can be correlated.
RUN_ID = uuid.uuid4().hex[:12]

_LEVEL_NAMES = {"WARNING": "warn", "CRITICAL": "error"}
_ROOT = "spike"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": _LEVEL_NAMES.get(record.levelname, record.levelname.lower()),
            "context": getattr(record, "context", record.name),
            "message": record.getMessage(),
            "run_id": RUN_ID,
            "error": None,
            "stack": None,
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["stack"] = "".join(traceback.format_exception(*record.exc_info))
        data = getattr(record, "data", None)
        if data:
            entry["data"] = data
        return json.dumps(entry, default=str)


def configure(log_dir: Path) -> Path:
    """Send logs to stderr and to a per-run file; return the file path."""
    level_name = os.environ.get("SPIKE_LOG_LEVEL", "info").upper()
    level = getattr(logging, level_name, None)
    root = logging.getLogger(_ROOT)
    root.handlers.clear()
    root.propagate = False
    formatter = _JsonFormatter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run-{RUN_ID}.log"
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not isinstance(level, int):
        root.setLevel(logging.INFO)
        Log("log").warn("unknown SPIKE_LOG_LEVEL, falling back to info", given=level_name)
    else:
        root.setLevel(level)
    return path


class Log:
    """Logger bound to one context name; keyword arguments become the `data` field."""

    def __init__(self, context: str) -> None:
        self._logger = logging.getLogger(_ROOT)
        self._context = context

    def _emit(self, level: int, message: str, exc_info: Any, data: dict[str, Any]) -> None:
        self._logger.log(
            level, message, exc_info=exc_info, extra={"context": self._context, "data": data}
        )

    def debug(self, message: str, **data: Any) -> None:
        self._emit(logging.DEBUG, message, None, data)

    def info(self, message: str, **data: Any) -> None:
        self._emit(logging.INFO, message, None, data)

    def warn(self, message: str, **data: Any) -> None:
        self._emit(logging.WARNING, message, None, data)

    def error(self, message: str, exc: BaseException | None = None, **data: Any) -> None:
        self._emit(logging.ERROR, message, exc if exc is not None else None, data)
