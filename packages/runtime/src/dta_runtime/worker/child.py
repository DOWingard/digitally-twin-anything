"""The process of one run of code the twin brought.

Started by the worker as `python -I -m dta_runtime.worker.child` with an environment
holding only `PATH`, `LANG`, `TMPDIR` and the variables the request declared; the
API's address and token come on the job pipe, never in the environment. It caps the
libraries' threads while it loads what it needs and takes the caps away again before
the code runs, so the code sees nothing it did not declare. Its address space and CPU
time are limited before the code is fetched.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    from .channel import take_channels
    from .limits import cap_threads, limit_cpu, limit_memory

    receiver, sender = take_channels()
    caps = cap_threads()

    from .channel import Job, Setup
    from .run import Runner, configure_logging, limit_arrow

    setup, job = receiver.recv()
    receiver.close()
    if not isinstance(setup, Setup) or not isinstance(job, Job) or job.cpu_s is None:
        raise TypeError("a process of own code takes a Setup and a Job with its CPU time")
    configure_logging(setup.log_level)
    limit_arrow()
    limit_memory(setup.memory_bytes)
    limit_cpu(job.cpu_s)
    runner = Runner(setup, sender, caps=caps)
    try:
        runner.run(job)
    finally:
        runner.close()
        sender.close()
        sys.stderr.flush()
    # Threads the code left running do not hold the process up.
    os._exit(0)


if __name__ == "__main__":
    main()
