"""A process of a pool: started once, it runs shipped code job after job.

Started by the worker as `python -I -m dta_runtime.worker.process`. It opens its
channel and caps the libraries' threads before importing anything heavy, takes its
setup, limits its address space, says it is ready and then runs every job it is
sent until its job pipe closes or a run leaves it unfit to run another.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from .channel import take_channels
    from .limits import cap_threads, limit_memory

    receiver, sender = take_channels()
    cap_threads()

    from dta_core.log import Log

    from .channel import Setup, send_ready
    from .run import Runner, configure_logging, limit_arrow

    log = Log("worker.process")
    setup = receiver.recv()
    if not isinstance(setup, Setup):
        raise TypeError(f"a pool process takes a Setup first, not {type(setup).__name__}")
    configure_logging(setup.log_level)
    limit_arrow()
    limit_memory(setup.memory_bytes)
    runner = Runner(setup, sender)
    send_ready(sender, pid=os.getpid())
    log.debug("pool process ready", pid=os.getpid())
    runs = 0
    try:
        while True:
            try:
                job = receiver.recv()
            except EOFError:
                log.debug("pool process told to stop", pid=os.getpid(), runs=runs)
                return 0
            runs += 1
            if runner.run(job):
                log.warn("pool process retires after a run", pid=os.getpid(), runs=runs)
                return 0
    finally:
        runner.close()
        sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main())
