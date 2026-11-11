"""Work the API does in the background, in lanes: the jobs of a lane run one at a time, in
the order they were made, and the lanes run side by side.

A job is a kind, its parameters, a callable and the outcome of calling it;
nothing here knows what any kind of work does. Jobs live in the memory of the
API process: the last `KEEP` are kept, and a job that is queued or running is
never dropped. Where a durable record of the work is needed, the work keeps it
itself (a sync run records itself in the twin).

Sync runs have a lane of their own, so that work which takes minutes (fitting
and testing a model, running a scenario) never delays the run that keeps the
twin's data fresh. Two jobs of different lanes may write at the same moment:
each write is one commit under the store's own lock.

No wall-clock time appears in a job: jobs are ordered by `sequence`, a counter
of this process, and `duration_ms` is measured on a monotonic timer.
"""

from __future__ import annotations

import itertools
import secrets
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log, current_trace_id, new_trace_id, trace

log = Log("api.jobs")

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
ACTIVE = frozenset({QUEUED, RUNNING})

# Runs of the sync pipeline, and nothing else.
SYNC_LANE = "sync"
# Everything else that takes long: fits, backtests, scenario runs, scheduled functions.
COMPUTE_LANE = "compute"
LANES = (SYNC_LANE, COMPUTE_LANE)

KEEP = 200
SHUTDOWN_WAIT_S = 30.0

CANCELLED = "job.cancelled"
UNEXPECTED = "job.unexpected"
CLOSED = "job.closed"


class JobsClosed(DtaError):
    """No job can be made: the API is not running or is stopping."""

    code = CLOSED


@dataclass(frozen=True)
class Outcome:
    """What work that ran to its end gives back. With `error` set, the job failed."""

    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None


Work = Callable[[], Outcome]


@dataclass
class Job:
    id: str
    kind: str
    lane: str
    params: dict[str, Any]
    idempotency_key: str | None
    trace_id: str
    sequence: int
    state: str = QUEUED
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    # Set once the job reached its final state.
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "lane": self.lane,
            "state": self.state,
            "params": self.params,
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
            "sequence": self.sequence,
            "duration_ms": self.duration_ms,
            "result": self.result,
            "error": dict(self.error) if self.error else None,
        }


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000.0))


class JobRunner:
    """Jobs and the workers that run them, one worker for each lane."""

    def __init__(self, *, keep: int = KEEP, shutdown_wait_s: float = SHUTDOWN_WAIT_S) -> None:
        self._keep = keep
        self._shutdown_wait_s = shutdown_wait_s
        # Guards every job's state and the table of jobs, so documents are read whole.
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._sequence = itertools.count(1)
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._closing = False

    # -- life -----------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._executors:
                return
            self._closing = False
            self._executors = {
                lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"dta-job-{lane}")
                for lane in LANES
            }
        log.info("job workers started", lanes=list(LANES), keep=self._keep)

    def shutdown(self) -> None:
        """Cancel what is queued and wait, for a while, for the jobs in progress."""
        started = time.perf_counter()
        with self._lock:
            if self._closing or not self._executors:
                return
            self._closing = True
            executors = list(self._executors.values())
            cancelled = [job for job in self._jobs.values() if job.state == QUEUED]
            for job in cancelled:
                self._cancel(job)
            in_progress = [job for job in self._jobs.values() if job.state == RUNNING]
        if cancelled:
            log.warn(
                "queued jobs cancelled: the API is stopping",
                jobs=[job.id for job in cancelled],
                count=len(cancelled),
            )
        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)
        # One wait shared by the jobs in progress, not one each.
        deadline = time.monotonic() + self._shutdown_wait_s
        for running in in_progress:
            log.info(
                "waiting for the job in progress before stopping",
                job=running.id,
                kind=running.kind,
                lane=running.lane,
                wait_s=self._shutdown_wait_s,
            )
            if not running.done.wait(max(0.0, deadline - time.monotonic())):
                log.warn(
                    "gave up waiting for the job in progress; it goes on in its thread",
                    job=running.id,
                    kind=running.kind,
                    lane=running.lane,
                    waited_s=self._shutdown_wait_s,
                )
        log.info("job workers stopped", elapsed_ms=_elapsed_ms(started))

    # -- jobs -----------------------------------------------------------------------

    def submit(
        self,
        kind: str,
        params: Mapping[str, Any],
        work: Work,
        *,
        lane: str,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> Job:
        """A new job, queued behind those made before it in its lane."""
        if lane not in LANES:
            raise ValueError(f"no lane {lane!r}; the lanes are {', '.join(LANES)}")
        with self._lock:
            if self._closing or not self._executors:
                log.warn("a job was asked for while the API is not running", kind=kind)
                raise JobsClosed("the API is stopping; no job can be started")
            job = Job(
                id=secrets.token_hex(16),
                kind=kind,
                lane=lane,
                params=dict(params),
                idempotency_key=idempotency_key,
                trace_id=trace_id or current_trace_id() or new_trace_id(),
                sequence=next(self._sequence),
            )
            self._jobs[job.id] = job
            # Handed to the worker under the lock, so the order in a lane is the sequence.
            self._executors[lane].submit(self._execute, job, work)
            self._prune()
        log.info(
            "job created",
            job=job.id,
            kind=kind,
            lane=lane,
            sequence=job.sequence,
            params=job.params,
            job_trace_id=job.trace_id,
        )
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.document() if job is not None else None

    def document(self, job: Job) -> dict[str, Any]:
        with self._lock:
            return job.document()

    def list(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        lane: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Documents of the jobs kept, newest first."""
        found: list[dict[str, Any]] = []
        with self._lock:
            for job in reversed(self._jobs.values()):
                if (
                    (kind is None or job.kind == kind)
                    and (state is None or job.state == state)
                    and (lane is None or job.lane == lane)
                ):
                    found.append(job.document())
                    if len(found) >= limit:
                        break
        return found

    def active(self, kind: str) -> Job | None:
        """The oldest job of a kind that is queued or running."""
        with self._lock:
            return next(
                (job for job in self._jobs.values() if job.kind == kind and job.state in ACTIVE),
                None,
            )

    # -- the worker ---------------------------------------------------------------------

    def _execute(self, job: Job, work: Work) -> None:
        with trace(job.trace_id):
            with self._lock:
                if job.state != QUEUED:
                    return
                if self._closing:
                    self._cancel(job)
                    log.warn("job cancelled before it started: the API is stopping", job=job.id)
                    return
                job.state = RUNNING
            log.info("job started", job=job.id, kind=job.kind, lane=job.lane, sequence=job.sequence)
            started = time.perf_counter()
            result: dict[str, Any] | None = None
            # What the job holds if the work is interrupted by something that is not an error.
            error: dict[str, str] | None = {
                "code": UNEXPECTED,
                "message": "the job was interrupted",
            }
            try:
                outcome = work()
            except DtaError as exc:
                error = {"code": exc.code, "message": exc.message}
                log.error(
                    "job failed",
                    exc=exc,
                    job=job.id,
                    kind=job.kind,
                    code=exc.code,
                    duration_ms=_elapsed_ms(started),
                )
            except Exception as exc:
                # The text of an unexpected error may quote data; only its type is kept.
                error = {
                    "code": UNEXPECTED,
                    "message": f"the job failed unexpectedly: {type(exc).__name__}",
                }
                log.error(
                    "job failed unexpectedly",
                    job=job.id,
                    kind=job.kind,
                    code=UNEXPECTED,
                    error_type=type(exc).__name__,
                    frames="".join(traceback.format_tb(exc.__traceback__)),
                    duration_ms=_elapsed_ms(started),
                )
                log.debug("unexpected error of a job in full", job=job.id, error=str(exc))
            else:
                result, error = outcome.result, outcome.error
                if error is not None:
                    log.warn(
                        "job failed: its work reported a failure",
                        job=job.id,
                        kind=job.kind,
                        code=error.get("code"),
                        duration_ms=_elapsed_ms(started),
                    )
            finally:
                duration = _elapsed_ms(started)
                with self._lock:
                    if job.state == RUNNING:
                        job.duration_ms = duration
                        job.state = FAILED if error is not None else SUCCEEDED
                        job.result = result
                        job.error = error
                        job.done.set()
                        self._prune()
                        state = job.state
                    else:
                        state = job.state
            log.info("job finished", job=job.id, kind=job.kind, state=state, duration_ms=duration)

    def _cancel(self, job: Job) -> None:
        """A queued job that will never run. Called under the lock."""
        if job.state != QUEUED:
            return
        job.state = FAILED
        job.error = {
            "code": CANCELLED,
            "message": "the API stopped before the job started",
        }
        job.done.set()

    def _prune(self) -> None:
        """Drop the oldest finished jobs beyond what is kept. Called under the lock."""
        excess = len(self._jobs) - self._keep
        if excess <= 0:
            return
        dropped = [job_id for job_id, job in self._jobs.items() if job.state not in ACTIVE][:excess]
        for job_id in dropped:
            del self._jobs[job_id]
        if dropped:
            log.debug("finished jobs dropped from memory", count=len(dropped), keep=self._keep)
