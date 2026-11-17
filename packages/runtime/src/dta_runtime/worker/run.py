"""One run inside the process that executes it: the handler called, its answer sent back.

The handler is imported here, in the process that runs it, and kept for the next run
of the same op. What the run logs through `dta_core.log` at info and above is kept
as its JSON records and returned with the result; the process's own log goes on to
its standard error as well.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import time
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
from dta_core import log as core_log
from dta_core.errors import DtaError
from dta_core.log import Log, trace

from ..connections import client_limits
from ..protocol import (
    RESULT_KEY,
    BacktestResult,
    FitResult,
    FunctionResult,
    PredictResult,
)
from .channel import Job, Setup, send_error, send_result
from .client import ApiArtifacts, ApiTwin, Calls
from .code import code_dir
from .errors import ARROW_MEDIA, FAILED, HANDLER_MISSING, JSON_MEDIA, MEMORY
from .limits import MIB, release_thread_caps

log = Log("worker.run")

RESULTS = (FunctionResult, FitResult, BacktestResult, PredictResult)
MAX_LOG_LINES = 200
MAX_FRAMES = 50
MAX_MESSAGE_CHARS = 2_000
_LOGGER = "dta"


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


class Capture(logging.Handler):
    """The run's log lines at info and above, as the JSON records the twin's log holds."""

    def __init__(self, limit: int = MAX_LOG_LINES) -> None:
        super().__init__(level=logging.INFO)
        # The twin's log format; the same lines go to the process's standard error.
        self.setFormatter(core_log._JsonFormatter())  # noqa: SLF001
        self._limit = limit
        self._lines: list[dict[str, Any]] = []
        self._seen = 0

    def emit(self, record: logging.LogRecord) -> None:
        self._seen += 1
        if len(self._lines) < self._limit:
            self._lines.append(json.loads(self.format(record)))

    def lines(self) -> list[dict[str, Any]]:
        if self._seen <= self._limit:
            return list(self._lines)
        kept = self._lines[: self._limit - 1]
        dropped = self._seen - len(kept)
        record = logging.LogRecord(
            _LOGGER, logging.WARNING, __file__, 0, "log lines dropped", None, None
        )
        record.context = "worker.run"
        record.data = {"dropped": dropped, "kept": len(kept)}
        return [*kept, json.loads(self.format(record))]


class HandlerMissing(DtaError):
    code = HANDLER_MISSING


def _import(spec: str) -> Callable[..., Any]:
    module_name, _, name = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, name)
    except Exception as exc:
        log.error("a handler cannot be imported", exc=exc, handler=spec, module=module_name)
        raise HandlerMissing(
            f"the handler {spec} cannot be imported from module {module_name}: "
            f"{type(exc).__name__}: {exc}",
            detail={"module": module_name},
        ) from exc


def _code_artifact(request: Any) -> str:
    family = getattr(request, "family", None)
    if family is not None and family.artifact is not None:
        return str(family.artifact)
    return str(request.artifact)


def _frames(exc: BaseException) -> list[str]:
    """The traceback below the worker's call of the handler, as text."""
    found = traceback.extract_tb(exc.__traceback__)
    while found and found[0].filename == __file__:
        found.pop(0)
    return [frame.rstrip("\n") for frame in traceback.format_list(found)][-MAX_FRAMES:]


def _message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE_CHARS]


def encode(returned: Any, duration_ms: float, logs: list[dict[str, Any]]) -> tuple[str, Any]:
    """The answer's media type and body: the result as JSON, or the table as an Arrow
    stream with the result under `RESULT_KEY` in its schema's metadata."""
    table, result = (
        returned if isinstance(returned, tuple) and len(returned) == 2 else (None, returned)
    )
    if not isinstance(result, RESULTS):
        raise TypeError(f"the handler returned {type(result).__name__}, not a result")
    if table is not None and not isinstance(table, pa.Table):
        raise TypeError(f"the handler returned a {type(table).__name__} as its table")
    text = result.model_copy(update={"duration_ms": duration_ms, "logs": logs}).model_dump_json()
    if table is None:
        return JSON_MEDIA, text.encode("utf-8")
    metadata = {**(table.schema.metadata or {}), RESULT_KEY.encode(): text.encode("utf-8")}
    table = table.replace_schema_metadata(metadata)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return ARROW_MEDIA, sink.getvalue()


def configure_logging(level: str | None) -> None:
    """The process's log to standard error at the stack's level; the logger itself lets
    info through whatever that level is, for the run's own lines."""
    core_log.configure(level=level)
    logger = logging.getLogger(_LOGGER)
    for handler in logger.handlers:
        handler.setLevel(logger.level)
    logger.setLevel(min(logger.level, logging.INFO))


def limit_arrow() -> None:
    """Arrow's pools to the process's one thread and its system allocator: Arrow's own
    allocator reserves address space far beyond what it uses."""
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    pa.set_memory_pool(pa.system_memory_pool())


class Runner:
    """Runs jobs in this process and answers on the channel."""

    def __init__(
        self, setup: Setup, sender: Connection, *, caps: dict[str, str] | None = None
    ) -> None:
        self._setup = setup
        self._sender = sender
        # Thread caps a process of own code takes away before the code runs.
        self._caps = caps
        self._handlers: dict[str, Callable[..., Any]] = {}
        for path in setup.handler_paths:
            if path not in sys.path:
                sys.path.append(path)
        self._http = httpx.Client(
            base_url=setup.api_url,
            headers={"Authorization": f"Bearer {setup.api_token}"},
            limits=client_limits(),
        )

    def close(self) -> None:
        self._http.close()

    def _handler(self, spec: str) -> Callable[..., Any]:
        found = self._handlers.get(spec)
        if found is None:
            started = time.monotonic()
            found = self._handlers[spec] = _import(spec)
            log.info("handler imported", handler=spec, elapsed_ms=_elapsed_ms(started))
        return found

    def run(self, job: Job) -> bool:
        """Run one job and send its answer; whether this process must not run another."""
        started = time.monotonic()
        request = job.request
        capture = Capture()
        logger = logging.getLogger(_LOGGER)
        logger.addHandler(capture)
        with trace(request.trace_id):
            log.info("run started", op=request.op, own_code=job.own_code)
            try:
                retire = self._answer(job, started, capture)
            except OSError as exc:
                # The worker gave up on the run (its budget passed) or is stopping.
                log.error("a run's answer cannot be sent", exc=exc, op=request.op)
                retire = True
            finally:
                logger.removeHandler(capture)
        return retire

    def _answer(self, job: Job, started: float, capture: Capture) -> bool:
        """Call the handler and send what came of it; whether the process must retire."""
        op = job.request.op
        try:
            returned = self._call(job)
            media_type, body = encode(returned, _elapsed_ms(started), capture.lines())
        except MemoryError:
            limit_mb = self._setup.memory_bytes // MIB
            log.error("the run went past its memory limit", op=op, limit_mb=limit_mb)
            message = f"the run went past its memory limit of {limit_mb} MiB"
            send_error(self._sender, MEMORY, message, retire=True)
            return True
        except DtaError as exc:
            log.warn("the run ended with an error", op=op, code=exc.code)
            send_error(self._sender, exc.code, exc.message[:MAX_MESSAGE_CHARS])
            return False
        except (Exception, SystemExit) as exc:
            # Code that asked for its process to end is not given that process again.
            retire = isinstance(exc, SystemExit)
            log.error("the run's code raised", exc=exc, op=op)
            send_error(self._sender, FAILED, _message(exc), frames=_frames(exc), retire=retire)
            return retire
        send_result(self._sender, media_type, body)
        log.info("run finished", op=op, media_type=media_type, elapsed_ms=_elapsed_ms(started))
        return False

    def _call(self, job: Job) -> Any:
        request = job.request
        handler = self._handler(job.handler)
        calls = Calls(self._http, deadline=job.deadline, trace_id=request.trace_id)
        twin = ApiTwin(calls, request)
        artifacts = ApiArtifacts(calls)
        if job.own_code:
            directory = code_dir(artifacts, _code_artifact(request), Path(self._setup.code_root))
            sys.path.insert(0, str(directory))
            if self._caps:
                release_thread_caps(self._caps)
        return handler(request, twin, artifacts)
