"""How big the pools are, and the limits the worker puts on every process that runs code.

The standard library and the twin's log only: a run process applies these before it
imports anything heavy, because some of them only take effect if they come first.

An address-space limit counts every byte a process maps, not what it uses. The
numeric libraries map memory per thread when they load: on a 12-core machine a
process that has imported pyarrow maps about 860 MB, nearly all of it buffers of
the BLAS library's threads and reservations of the allocators, with 86 MB resident.
So a run process starts with one thread per library pool, few malloc arenas and
Arrow's system allocator; it then maps about 250 MB, of which 150 MB are the
libraries' own files.
"""

from __future__ import annotations

import ctypes
import math
import os
import resource
from pathlib import Path

from dta_core.log import Log

log = Log("worker.limits")

MIB = 1024 * 1024
MIN_PROCESS_MEMORY = 256 * MIB
MAX_INTERACTIVE = 4
BATCH_PROCESSES = 1
# Seconds of CPU time own code gets beyond its budget on the wall; only a run with
# several busy threads reaches it before the wall does.
CPU_GRACE_S = 5
# Between the soft limit (SIGXCPU) and the hard one (SIGKILL): a run that ignores
# SIGXCPU is still stopped, and one that does not is told apart from one killed
# for memory.
CPU_HARD_EXTRA_S = 2

THREADS_PER_PROCESS = 1
# Read by the libraries when they load: BLAS, OpenMP (also Arrow's own pool), MKL,
# numexpr, numba; and which allocator Arrow uses.
THREAD_CAPS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
ARROW_POOL_ENV = "ARROW_DEFAULT_MEMORY_POOL"
_M_ARENA_MAX = -8
MALLOC_ARENAS = 2


def interactive_size(cpus: float) -> int:
    return min(MAX_INTERACTIVE, max(1, math.floor(cpus)))


def processes_at_once(cpus: float) -> int:
    """Processes that can run code at the same time: every process of both pools; a
    run of own code takes the place of one of them."""
    return interactive_size(cpus) + BATCH_PROCESSES


def process_memory(memory_mb: int, cpus: float) -> int:
    """Bytes of address space each process may map."""
    share = memory_mb * MIB // processes_at_once(cpus)
    return max(MIN_PROCESS_MEMORY, share)


def cpu_seconds(budget_s: float) -> int:
    return math.ceil(budget_s) + CPU_GRACE_S


def cap_threads(threads: int = THREADS_PER_PROCESS) -> dict[str, str]:
    """Few threads for the libraries this process loads from now on, and Arrow's system
    allocator; the variables set, so that a process of own code can take them away."""
    caps = {name: str(threads) for name in THREAD_CAPS}
    caps[ARROW_POOL_ENV] = "system"
    os.environ.update(caps)
    return caps


def cap_arenas() -> bool:
    """At most `MALLOC_ARENAS` malloc arenas, each of which reserves 64 MiB of address
    space; whether the allocator took the cap (only glibc's has arenas)."""
    try:
        return bool(ctypes.CDLL(None).mallopt(_M_ARENA_MAX, MALLOC_ARENAS))
    except (AttributeError, OSError):
        return False


def release_thread_caps(caps: dict[str, str]) -> None:
    for name in caps:
        os.environ.pop(name, None)


def limit_memory(memory_bytes: int) -> None:
    """Soft and hard alike, so the code that runs cannot raise it again."""
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def limit_cpu(seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + CPU_HARD_EXTRA_S))


def _cgroup_events() -> Path | None:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        log.debug("the process's cgroup cannot be read", error=repr(exc))
        return None
    for line in lines:
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup" + line[3:].rstrip("/")) / "memory.events"
    log.debug("the process is in no cgroup v2")
    return None


def oom_kills() -> int | None:
    """How many processes the kernel has killed for memory in this process's cgroup
    (in a container: the container), or None where that cannot be read."""
    events = _cgroup_events()
    if events is None:
        return None
    try:
        for line in events.read_text(encoding="ascii").splitlines():
            name, _, value = line.partition(" ")
            if name == "oom_kill":
                return int(value)
    except (OSError, ValueError) as exc:
        log.debug("the cgroup's memory events cannot be read", path=str(events), error=repr(exc))
        return None
    return None
