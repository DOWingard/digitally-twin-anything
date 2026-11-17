"""The one client of the worker: everything that fits, tests or applies a model, and every
run of code the twin brought, leaves the API process through here.

A request names what to read, never the data; the worker reads it back through the API.
What the worker refuses is passed on as it was given: its code, its detail, and for a
function that raised, the frames of its own code. What is not an answer of the worker at
all (no connection, something that is not a result or a problem) is `worker.unreachable`.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import pyarrow as pa
from dta_core.log import Log, trace

from ..connections import client_limits
from ..protocol import RESULT_KEY
from .formats import ARROW_MEDIA, JSON_MEDIA
from .problems import PROBLEM_MEDIA, STATUS_BY_CODE, ApiError

log = Log("api.compute")
# What a run logged, written again here under the run's trace.
run_log = Log("worker.run")

UNAVAILABLE = "worker.unavailable"
UNREACHABLE = "worker.unreachable"
BUSY = "worker.busy"
# Seconds allowed for the answer to travel, beyond the run's own budget.
ANSWER_MARGIN_S = 5.0
# The longest a job waits before asking a busy worker again.
BUSY_WAIT_MAX_S = 30.0
BUSY_WAIT_DEFAULT_S = 5.0
HEALTH_TIMEOUT_S = 1.0
HEALTH_KEPT_S = 5.0
_LEVELS = {"debug": run_log.debug, "info": run_log.info, "warn": run_log.warn}


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _subject(request: Any) -> str | None:
    """What a run is of, for the log: the family, or the function's entry."""
    family = getattr(request, "family", None)
    if family is not None:
        return family.name or family.entry
    return getattr(request, "entry", None)


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return BUSY_WAIT_DEFAULT_S


class Compute:
    """Requests to the worker of the twin's stack; one for the whole API process."""

    def __init__(self, worker_url: str | None, token: str) -> None:
        self._url = worker_url
        self._http: httpx.Client | None = None
        if worker_url is not None:
            self._http = httpx.Client(
                base_url=worker_url,
                headers={"Authorization": f"Bearer {token}"},
                limits=client_limits(),
            )
        self._lock = threading.Lock()
        # (monotonic time it was asked, whether the worker answered).
        self._health: tuple[float, bool] | None = None

    @property
    def configured(self) -> bool:
        return self._http is not None

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    def require(self) -> httpx.Client:
        if self._http is None:
            log.warn("the worker was needed and the twin has none")
            raise ApiError(
                "this twin has no worker: it has no model and no code of its own, or its "
                "stack was started without one",
                code=UNAVAILABLE,
            )
        return self._http

    def healthy(self) -> bool | None:
        """Whether the worker answers; None without one. Asked at most once in
        `HEALTH_KEPT_S`, so that a poll of the twin's health costs one request."""
        if self._http is None:
            return None
        moment = time.monotonic()
        with self._lock:
            if self._health is not None and moment - self._health[0] < HEALTH_KEPT_S:
                return self._health[1]
        try:
            response = self._http.get("/healthz", timeout=httpx.Timeout(HEALTH_TIMEOUT_S))
            answered = response.status_code == 200
            if not answered:
                log.warn("the worker is not ready", status=response.status_code)
        except httpx.HTTPError as exc:
            log.warn("the worker does not answer", error=type(exc).__name__)
            answered = False
        with self._lock:
            self._health = (moment, answered)
        return answered

    def run(
        self, request: Any, *, wait_until: float | None = None
    ) -> tuple[dict[str, Any], pa.Table | None]:
        """Send one run; its result, and its table when the answer is one.

        A busy worker is the caller's to wait for, with the worker's `Retry-After`,
        unless `wait_until` (a `time.monotonic()` value) is given: a job waits and asks
        again until then.
        """
        http = self.require()
        subject = _subject(request)
        body = request.model_dump(mode="json")
        waited = threading.Event()
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                response = http.post(
                    "/run", json=body, timeout=httpx.Timeout(request.budget_s + ANSWER_MARGIN_S)
                )
            except httpx.HTTPError as exc:
                log.error(
                    "the worker cannot be reached",
                    exc=exc,
                    op=request.op,
                    subject=subject,
                    outcome=UNREACHABLE,
                    duration_ms=_elapsed_ms(started),
                )
                raise ApiError(
                    f"the worker did not answer ({type(exc).__name__})", code=UNREACHABLE
                ) from exc
            media = response.headers.get("content-type", "").split(";")[0].strip()
            if response.status_code == 200 and media in (ARROW_MEDIA, JSON_MEDIA):
                result, table = self._result(response, media)
                self._record(result.pop("logs", None), request.trace_id)
                log.info(
                    "worker run",
                    op=request.op,
                    subject=subject,
                    outcome="ok",
                    attempts=attempt,
                    rows=None if table is None else table.num_rows,
                    run_ms=result.get("duration_ms"),
                    duration_ms=_elapsed_ms(started),
                )
                return result, table
            refusal = self._problem(response, media, request.op, subject, started)
            if refusal.code != BUSY or wait_until is None:
                raise refusal
            pause = min(_retry_after(response), BUSY_WAIT_MAX_S, wait_until - time.monotonic())
            if pause <= 0:
                log.warn(
                    "the worker stayed busy until the job's deadline",
                    op=request.op,
                    subject=subject,
                    attempts=attempt,
                )
                raise refusal
            log.warn(
                "the worker is busy; the job waits and asks again",
                op=request.op,
                subject=subject,
                attempt=attempt,
                wait_s=round(pause, 2),
            )
            waited.wait(pause)

    @staticmethod
    def _result(response: httpx.Response, media: str) -> tuple[dict[str, Any], pa.Table | None]:
        try:
            if media == JSON_MEDIA:
                found = response.json()
                table = None
            else:
                table = pa.ipc.open_stream(response.content).read_all()
                metadata = dict(table.schema.metadata or {})
                found = json.loads(metadata.pop(RESULT_KEY.encode("utf-8")))
                table = table.replace_schema_metadata(metadata or None)
        except (ValueError, KeyError, pa.ArrowException) as exc:
            log.error("the worker's answer cannot be read", exc=exc, media_type=media)
            raise ApiError(
                "the worker answered something that is not a result", code=UNREACHABLE
            ) from exc
        if not isinstance(found, dict):
            log.error("the worker's result is not an object", media_type=media)
            raise ApiError("the worker answered something that is not a result", code=UNREACHABLE)
        return found, table

    @staticmethod
    def _problem(
        response: httpx.Response, media: str, op: str, subject: str | None, started: float
    ) -> ApiError:
        document: Any = None
        if media == PROBLEM_MEDIA:
            try:
                document = response.json()
            except ValueError:
                document = None
        code = document.get("code") if isinstance(document, dict) else None
        # A token the worker refuses is a fault of the stack, not something to pass on.
        if not isinstance(code, str) or response.status_code < 400 or code == "worker.unauthorized":
            log.error(
                "the worker answered neither a result nor a problem it may pass on",
                op=op,
                subject=subject,
                status=response.status_code,
                media_type=media,
                code=code,
                outcome=UNREACHABLE,
                duration_ms=_elapsed_ms(started),
            )
            return ApiError(
                f"the worker answered {response.status_code} without a result", code=UNREACHABLE
            )
        log.info(
            "worker run",
            op=op,
            subject=subject,
            outcome=code,
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
        )
        extra = {"frames": document["frames"]} if "frames" in document else None
        headers = None
        if response.headers.get("retry-after"):
            headers = {"Retry-After": response.headers["retry-after"]}
        return ApiError(
            str(document.get("detail") or code),
            code=code,
            # The API's own status for a code it knows; the worker's for the others.
            status=STATUS_BY_CODE.get(code, response.status_code),
            extra=extra,
            headers=headers,
        )

    @staticmethod
    def _record(lines: Any, trace_id: str) -> None:
        """What the run logged, as lines of this process under the run's trace."""
        if not isinstance(lines, list) or not lines:
            return
        with trace(trace_id):
            for line in lines:
                if not isinstance(line, dict):
                    continue
                data = line.get("data") if isinstance(line.get("data"), dict) else {}
                fields = {f"run_{name}": value for name, value in data.items()}
                emit = _LEVELS.get(str(line.get("level")))
                if emit is not None:
                    emit(str(line.get("message", "")), run_context=line.get("context"), **fields)
                else:
                    run_log.error(
                        str(line.get("message", "")),
                        run_context=line.get("context"),
                        run_error=line.get("error"),
                        **fields,
                    )
