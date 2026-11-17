"""Where a run executes, and the limits enforced on it from outside.

Shipped code runs in a process of a pool that is kept: `batch` (one process) for the
ops that may take minutes, `interactive` for the others. Code the twin brought runs
in a fresh process of its own, which counts against the same pool's limit. A pool
admits as many runs as it has processes; as many more may wait; beyond that it
answers busy at once.

Every run has a deadline on the monotonic clock. When it passes, the event loop
kills the process (its whole process group) whatever the thread reading its answer
is doing, and a pool process killed that way is replaced in the background.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any

import anyio.to_thread
from dta_core.log import Log

from ..protocol import BATCH_OPS, runs_own_code
from ..settings import WorkerSettings
from .channel import ERROR, READY, ChannelBroken, Job, Reply, Setup, read_reply
from .errors import BUSY, FAILED, MEMORY, TIMEOUT, WorkerError
from .limits import (
    BATCH_PROCESSES,
    MIB,
    cpu_seconds,
    interactive_size,
    oom_kills,
    process_memory,
)

log = Log("worker.pools")

POOL_MODULE = "dta_runtime.worker.process"
CHILD_MODULE = "dta_runtime.worker.child"
BATCH = "batch"
INTERACTIVE = "interactive"
START_TIMEOUT_S = 60.0
# How long a run that holds a slot waits for a process of its pool, which is only
# missing while one is being replaced.
TAKE_TIMEOUT_S = 30.0
STOP_GRACE_S = 2.0
RETRY_MIN_S = 0.5
RETRY_MAX_S = 30.0
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
LANG = "C.UTF-8"
# Variables of the worker a pool process is given besides PATH, LANG and TMPDIR.
POOL_PASSED = ("HOME", "TZ", "NUMBA_CACHE_DIR")


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


class Proc:
    """A process that runs code and its two pipes. Its session is its own, so killing
    its group kills what it started too.

    Its end is seen as the end of its answer pipe: the descriptor is private to it, so
    the pipe closes when it dies. A process it forked may hold the pipe open after it;
    the deadline's kill takes that one down with the group.
    """

    def __init__(self, popen: subprocess.Popen[bytes], sender: Connection, receiver: Connection):
        self.popen = popen
        self.pid = popen.pid
        self.sender = sender
        self.receiver = receiver
        self._lock = threading.Lock()
        self._answered = False
        self.expired = False
        self.closed = False

    @classmethod
    def spawn(cls, module: str, env: Mapping[str, str], cwd: Path) -> Proc:
        job_read, job_write = os.pipe()
        answer_read, answer_write = os.pipe()
        try:
            popen = subprocess.Popen(
                [sys.executable, "-I", "-m", module],
                stdin=job_read,
                stdout=answer_write,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(job_write)
            os.close(answer_read)
            raise
        finally:
            os.close(job_read)
            os.close(answer_write)
        return cls(
            popen, Connection(job_write, readable=False), Connection(answer_read, writable=False)
        )

    def alive(self) -> bool:
        return not self.closed and self.popen.poll() is None

    def begin(self) -> None:
        with self._lock:
            self._answered = False
            self.expired = False

    def expire(self) -> None:
        """The deadline passed: kill the process unless its answer is already in."""
        with self._lock:
            if self._answered:
                return
            self.expired = True
        log.warn("a run passed its budget; its process is killed", pid=self.pid)
        self.kill()

    def kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            # The group is gone and its id taken by someone else's: kill the process alone.
            log.warn(
                "the process group cannot be killed; killing the process",
                pid=self.pid,
                error=repr(exc),
            )
            self.popen.kill()

    def read(self, oom_before: int | None) -> Reply:
        """Block until the process answers or ends; a death becomes an error reply."""
        try:
            reply = read_reply(self.receiver)
        except EOFError:
            return self._death(oom_before)
        except ChannelBroken as exc:
            if self.expired:
                return self._death(oom_before)
            log.error("a run process sent an unreadable answer", pid=self.pid, error=str(exc))
            self.kill()
            self.popen.wait()
            return Reply(kind=ERROR, code=FAILED, message=f"the run sent {exc}", retire=True)
        with self._lock:
            self._answered = not self.expired
        if self.expired:
            return self._death(oom_before)
        return reply

    def run(self, message: Any, oom_before: int | None) -> Reply:
        try:
            self.sender.send(message)
        except OSError as exc:
            log.warn("a run process could not be sent its job", pid=self.pid, error=repr(exc))
            return self._death(oom_before)
        return self.read(oom_before)

    def _death(self, oom_before: int | None) -> Reply:
        try:
            code = self.popen.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            log.error("a run process closed its answer pipe and lives on; killed", pid=self.pid)
            self.kill()
            self.popen.wait()
            message = "the run's process stopped answering before it gave a result"
            return Reply(kind=ERROR, code=FAILED, message=message, retire=True)
        if self.expired:
            message = "the run passed its budget and was stopped"
            return Reply(kind=ERROR, code=TIMEOUT, message=message, retire=True)
        if code < 0:
            number = -code
            if number == signal.SIGXCPU:
                message = "the run used up its CPU time and was stopped"
                return Reply(kind=ERROR, code=TIMEOUT, message=message, retire=True)
            if number == signal.SIGKILL:
                after = oom_kills()
                if oom_before is None or after is None or after > oom_before:
                    message = "the run's process was killed for memory"
                    return Reply(kind=ERROR, code=MEMORY, message=message, retire=True)
            message = f"the run's process was killed by {_signal_name(number)}"
            return Reply(kind=ERROR, code=FAILED, message=message, retire=True)
        message = f"the run's process exited with status {code} before it answered"
        return Reply(kind=ERROR, code=FAILED, message=message, retire=True)

    def close(self) -> None:
        """Tell the process to stop, then kill its group; safe to call more than once."""
        if self.closed:
            return
        self.closed = True
        try:
            self.sender.close()
        except OSError:
            pass
        if self.popen.poll() is None:
            try:
                self.popen.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                log.warn("a run process did not stop when told; killed", pid=self.pid)
        # Whatever it left running in its group goes with it.
        self.kill()
        self.popen.wait()
        self.receiver.close()


def start_pool_process(setup: Setup, env: Mapping[str, str], cwd: Path) -> Proc:
    started = time.monotonic()
    proc = Proc.spawn(POOL_MODULE, env, cwd)
    try:
        proc.sender.send(setup)
        if proc.receiver not in wait([proc.receiver], timeout=START_TIMEOUT_S):
            raise WorkerError(f"a pool process did not start within {START_TIMEOUT_S:g} s")
        reply = read_reply(proc.receiver)
        if reply.kind != READY:
            raise WorkerError(f"a pool process answered {reply.kind} instead of ready")
    except (WorkerError, EOFError, ChannelBroken, OSError) as exc:
        log.error("a pool process failed to start", exc=exc, pid=proc.pid, code=proc.popen.poll())
        proc.close()
        raise WorkerError(f"a pool process failed to start: {type(exc).__name__}: {exc}") from exc
    log.info("pool process started", pid=proc.pid, elapsed_ms=_elapsed_ms(started))
    return proc


@dataclass
class Slot:
    """A run admitted to a pool; its deadline once it has one."""

    deadline: float | None = None
    waited_ms: float = 0.0


@dataclass
class Pool:
    name: str
    size: int
    start: Callable[[], Proc]
    running: int = 0
    waiting: int = 0
    closed: bool = False
    _slots: asyncio.Semaphore = field(init=False)
    _idle: asyncio.Queue[Proc] = field(init=False)
    _procs: set[Proc] = field(init=False, default_factory=set)
    _admitted: list[Slot] = field(init=False, default_factory=list)
    _tasks: set[asyncio.Task[None]] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.size)
        self._idle = asyncio.Queue()

    async def open(self) -> None:
        started = time.monotonic()
        procs = await asyncio.gather(
            *(anyio.to_thread.run_sync(self.start) for _ in range(self.size)),
            return_exceptions=True,
        )
        failed = [found for found in procs if isinstance(found, BaseException)]
        for found in procs:
            if isinstance(found, Proc):
                self._procs.add(found)
                self._idle.put_nowait(found)
        if failed:
            await self.close()
            raise failed[0]
        log.info("pool open", pool=self.name, processes=self.size, elapsed_ms=_elapsed_ms(started))

    def retry_after_s(self) -> int:
        """The soonest a run of the pool must end, in whole seconds."""
        now = time.monotonic()
        ends = [slot.deadline - now for slot in self._admitted if slot.deadline is not None]
        return max(1, math.ceil(min(ends))) if ends else 1

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[Slot]:
        if self.closed:
            raise WorkerError("the worker is stopping", code=BUSY, headers={"Retry-After": "5"})
        if self.running >= self.size and self.waiting >= self.size:
            retry = self.retry_after_s()
            log.warn(
                "a pool is full; a run is refused",
                pool=self.name,
                running=self.running,
                waiting=self.waiting,
                retry_after_s=retry,
            )
            raise WorkerError(
                f"the {self.name} pool is full: {self.running} runs and {self.waiting} waiting",
                code=BUSY,
                headers={"Retry-After": str(retry)},
            )
        started = time.monotonic()
        self.waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self.waiting -= 1
        admitted = Slot(waited_ms=_elapsed_ms(started))
        self.running += 1
        self._admitted.append(admitted)
        try:
            yield admitted
        finally:
            self._admitted.remove(admitted)
            self.running -= 1
            self._slots.release()

    async def take(self) -> Proc:
        while True:
            try:
                proc = await asyncio.wait_for(self._idle.get(), TAKE_TIMEOUT_S)
            except TimeoutError as exc:
                log.error(
                    "no process of a pool became free", pool=self.name, waited_s=TAKE_TIMEOUT_S
                )
                raise WorkerError(
                    f"no process of the {self.name} pool is available",
                    code=BUSY,
                    headers={"Retry-After": str(int(TAKE_TIMEOUT_S))},
                ) from exc
            if proc.alive():
                return proc
            log.warn("an idle pool process had ended; replacing it", pool=self.name, pid=proc.pid)
            self.replace(proc)

    def give_back(self, proc: Proc) -> None:
        self._idle.put_nowait(proc)

    def replace(self, proc: Proc) -> None:
        self._procs.discard(proc)
        task = asyncio.get_running_loop().create_task(self._replace(proc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _replace(self, dead: Proc) -> None:
        started = time.monotonic()
        await anyio.to_thread.run_sync(dead.close)
        delay = RETRY_MIN_S
        while not self.closed:
            try:
                proc = await anyio.to_thread.run_sync(self.start)
            except Exception as exc:
                log.error(
                    "a pool process cannot be started; retrying",
                    exc=exc,
                    pool=self.name,
                    retry_in_s=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_S)
                continue
            if self.closed:
                await anyio.to_thread.run_sync(proc.close)
                return
            self._procs.add(proc)
            self._idle.put_nowait(proc)
            log.info(
                "pool process replaced",
                pool=self.name,
                old_pid=dead.pid,
                pid=proc.pid,
                elapsed_ms=_elapsed_ms(started),
            )
            return

    async def close(self) -> None:
        self.closed = True
        for task in list(self._tasks):
            task.cancel()
        procs = list(self._procs)
        self._procs.clear()
        await asyncio.gather(*(anyio.to_thread.run_sync(proc.close) for proc in procs))
        log.info("pool closed", pool=self.name, processes=len(procs))


@dataclass(frozen=True)
class Ran:
    """A run's reply and where it ran."""

    reply: Reply
    pool: str
    own_code: bool
    waited_ms: float


class Pools:
    """Both pools and the processes of own code, for one worker."""

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        handlers: Mapping[str, str],
        handler_paths: Sequence[str] = (),
        work_dir: Path | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        root = work_dir or Path(tempfile.gettempdir()) / "dta-worker"
        self.code_root = root / "code"
        self.runs_root = root / "runs"
        for directory in (root, self.code_root, self.runs_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.memory_bytes = process_memory(settings.memory_mb, settings.cpus)
        self.setup = Setup(
            api_url=settings.api_url,
            api_token=settings.api_token,
            memory_bytes=self.memory_bytes,
            code_root=str(self.code_root),
            handler_paths=tuple(str(path) for path in handler_paths),
            log_level=os.environ.get("DTA_LOG_LEVEL"),
        )
        self._root = root
        pool_env = {
            "PATH": os.environ.get("PATH", DEFAULT_PATH),
            "LANG": LANG,
            "TMPDIR": tempfile.gettempdir(),
            **{name: os.environ[name] for name in POOL_PASSED if name in os.environ},
        }

        def start() -> Proc:
            return start_pool_process(self.setup, pool_env, root)

        self.batch = Pool(BATCH, BATCH_PROCESSES, start)
        self.interactive = Pool(INTERACTIVE, interactive_size(settings.cpus), start)
        self.started = False

    def pool_for(self, op: str) -> Pool:
        return self.batch if op in BATCH_OPS else self.interactive

    async def open(self) -> None:
        started = time.monotonic()
        await self.interactive.open()
        try:
            await self.batch.open()
        except BaseException:
            await self.interactive.close()
            raise
        self.started = True
        log.info(
            "pools open",
            interactive=self.interactive.size,
            batch=self.batch.size,
            memory_mb_per_process=self.memory_bytes // MIB,
            elapsed_ms=_elapsed_ms(started),
        )

    async def close(self) -> None:
        self.started = False
        await asyncio.gather(self.interactive.close(), self.batch.close())

    async def run(self, request: Any) -> Ran:
        pool = self.pool_for(request.op)
        own = runs_own_code(request)
        handler = self._handlers[request.op]
        async with pool.slot() as slot:
            if own:
                reply = await self._own(request, handler, slot)
            else:
                reply = await self._shipped(pool, request, handler, slot)
        return Ran(reply=reply, pool=pool.name, own_code=own, waited_ms=slot.waited_ms)

    async def _watch(self, proc: Proc, message: Any, deadline: float) -> Reply:
        loop = asyncio.get_running_loop()
        oom_before = oom_kills()
        proc.begin()
        timer = loop.call_later(max(0.0, deadline - time.monotonic()), proc.expire)
        try:
            return await anyio.to_thread.run_sync(proc.run, message, oom_before)
        finally:
            timer.cancel()

    async def _shipped(self, pool: Pool, request: Any, handler: str, slot: Slot) -> Reply:
        proc = await pool.take()
        deadline = slot.deadline = time.monotonic() + request.budget_s
        reply = await self._watch(proc, Job(request, handler, deadline), deadline)
        if reply.retire or not proc.alive():
            log.info(
                "a pool process is replaced after a run",
                pool=pool.name,
                pid=proc.pid,
                code=reply.code,
            )
            pool.replace(proc)
        else:
            pool.give_back(proc)
        return reply

    async def _own(self, request: Any, handler: str, slot: Slot) -> Reply:
        rundir = Path(tempfile.mkdtemp(prefix="run-", dir=self.runs_root))
        env = {"PATH": os.environ.get("PATH", DEFAULT_PATH), "LANG": LANG, "TMPDIR": str(rundir)}
        env.update({name: os.environ[name] for name in getattr(request, "env", ())})
        deadline = slot.deadline = time.monotonic() + request.budget_s
        job = Job(request, handler, deadline, own_code=True, cpu_s=cpu_seconds(request.budget_s))
        proc: Proc | None = None
        try:
            proc = await anyio.to_thread.run_sync(Proc.spawn, CHILD_MODULE, env, rundir)
            log.debug("process of own code started", pid=proc.pid, op=request.op)
            return await self._watch(proc, (self.setup, job), deadline)
        finally:
            if proc is not None:
                await anyio.to_thread.run_sync(proc.close)
            await anyio.to_thread.run_sync(self._remove, rundir)

    @staticmethod
    def _remove(rundir: Path) -> None:
        def failed(function: Any, path: str, exc: BaseException) -> None:
            log.warn("a run's temporary file cannot be removed", path=path, error=repr(exc))

        shutil.rmtree(rundir, onexc=failed)
