"""Run the real `dta` command line in a process of its own, for the tests of concurrency.

Usage: launch.py [--ready FILE] [--start FILE] [--die-at GAP] <dta arguments>

--ready and --start line several processes up: each one imports the program,
creates its ready file, then waits until the start file exists, so that all of
them enter the command at the same moment instead of one interpreter start-up
apart.

--die-at kills the process, with no clean-up of any kind, when the command
reaches the named gap between two durable writes. Nothing in the program reads
a flag for this: the marker function it calls there is replaced in this
process only.
"""

import os
import sys
import time
from pathlib import Path


def main() -> None:
    arguments = sys.argv[1:]
    options = {}
    while arguments and arguments[0] in ("--ready", "--start", "--die-at"):
        options[arguments[0]] = arguments[1]
        arguments = arguments[2:]

    from dta_toolkit.cli import app
    from dta_toolkit.tasks import lifecycle

    gap = options.get("--die-at")
    if gap is not None:

        def stop(name: str) -> None:
            if name == gap:
                os._exit(70)

        lifecycle._checkpoint = stop

    if "--ready" in options:
        Path(options["--ready"]).touch()
    if "--start" in options:
        start = Path(options["--start"])
        deadline = time.monotonic() + 60
        while not start.exists():
            if time.monotonic() > deadline:
                sys.exit(99)
            time.sleep(0.0005)

    sys.argv = ["dta", *arguments]
    app()


if __name__ == "__main__":
    main()
