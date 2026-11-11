"""dlt's usage telemetry stays off: its runtime configuration says so and no
network connection is attempted while the package is imported and a table is
read through dlt. Runs in a fresh interpreter, so that dlt is imported there
for the first time, with the switch removed from its environment."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Generous: the machine running the tests may be heavily loaded.
SUBPROCESS_TIMEOUT_S = 300

_PROGRAM = r"""
import json
import sys

attempts = []

def watch(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"):
        attempts.append([event, repr(args)[:200]])

sys.addaudithook(watch)

import os
before = "dlt" in sys.modules
import dta_core.connect as connect
from dta_core.manifest.models import Source

source = Source(connector="sql", location="sqlite:///" + sys.argv[1])
with connect.open_connector("t", source, {}) as connector:
    rows = sum(table.num_rows for table in connector.read("r"))

from dlt.common.configuration import resolve_configuration
from dlt.common.configuration.specs import RuntimeConfiguration
from dlt.common.runtime import anon_tracker

print(json.dumps({
    "dlt_imported_before": before,
    "rows": rows,
    "env": os.environ.get("RUNTIME__DLTHUB_TELEMETRY"),
    "telemetry": resolve_configuration(RuntimeConfiguration()).dlthub_telemetry,
    "tracker_endpoint": anon_tracker._ANON_TRACKER_ENDPOINT,
    "tracker_pool": anon_tracker._THREAD_POOL is not None,
    "attempts": attempts,
}))
"""


def test_dlt_telemetry_is_off_and_no_connection_is_made(tmp_path: Path) -> None:
    database = tmp_path / "t.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE r (id integer primary key, v real)")
        connection.executemany("INSERT INTO r VALUES (?, ?)", [(i, i / 2) for i in range(10)])
    env = {key: value for key, value in os.environ.items() if not key.startswith("RUNTIME__")}
    finished = subprocess.run(
        [sys.executable, "-c", _PROGRAM, str(database)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    result = json.loads(finished.stdout.strip().splitlines()[-1])
    assert result["dlt_imported_before"] is False
    assert result["rows"] == 10
    assert result["env"] == "false"
    assert result["telemetry"] is False
    assert result["tracker_endpoint"] is None and result["tracker_pool"] is False
    assert result["attempts"] == []
