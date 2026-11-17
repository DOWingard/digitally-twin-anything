"""What the live-twin tests share: limits, expected steps and checks, and reading the API.

The limits are fixed here, before anything is measured.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from typing import Any

import m3tk_kit as kit
from dta_core.log import configure
from dta_core.manifest import parse_manifest
from dta_toolkit.acceptance import TwinApi
from dta_toolkit.envfile import read_env
from dta_toolkit.stack import Stack
from dta_toolkit.workspace import Workspace

LIVE_BUILD_LIMIT_S = 180.0
SYNC_RUN_LIMIT_S = 30.0
LIVE_ACCEPT_LIMIT_S = 120.0
SCHEDULED_PICKUP_LIMIT_S = 150.0
BUSY_RETRIES = 10
BUSY_PAUSE_S = 3.0

STEPS = (
    "scaffold",
    "image",
    "store",
    "install",
    "load",
    "api",
    "contract",
    "sync",
    "code",
    "functions",
    "models",
    "questions",
    "ui",
    "skill",
)
# What a twin with questions and no code or model of its own does in each build.
COMPUTE_STEPS = {
    "image": ("ran", "skipped"),
    "code": ("ran", "skipped"),
    "functions": ("ran", "ok"),
    "models": ("ran", "skipped"),
    "questions": ("ran", "ok"),
}
SYNC_CHECKS = ("sync.scheduler", "sync.runs", "sync.sources", "sync.freshness")
COMPUTE_CHECKS = ("questions.answered", "questions.error", "models.accepted", "compute.worker")
CHECKS = {
    "api.contract",
    "readiness.R1",
    "readiness.R2",
    "readiness.R3",
    "readiness.R4",
    "readiness.R5",
    "data.constraints",
    "data.coverage",
    "ui.reachable",
    "skill.current",
    "skill.endpoints",
    *SYNC_CHECKS,
    *COMPUTE_CHECKS,
}
LIVE_LOADED = "the twin already holds data; its runs keep it in step"


def steps(result: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {entry["step"]: (entry["action"], entry["status"]) for entry in result["steps"]}


def step_detail(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in result["steps"] if entry["step"] == name)["detail"]


def check(result: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(entry for entry in result["checks"] if entry["id"] == ident)


def _stack(twin: kit.KitTwin) -> Stack:
    """The twin's stack as the toolkit runs it, from this test's own process."""
    # A command run through CliRunner leaves the log handler on a stream it has closed.
    configure()
    workspace = Workspace(twin.root)
    return Stack(workspace, workspace.load_manifest())


def services(twin: kit.KitTwin) -> dict[str, str]:
    """State of each container of the twin's stack."""
    return {row["service"]: row["state"] for row in _stack(twin).services()}


def started_at(twin: kit.KitTwin, service: str) -> str:
    """When the service's container last started, as docker records it."""
    rows = _stack(twin).services()
    container = next(row["container"] for row in rows if row["service"] == service)
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def stop_service(twin: kit.KitTwin, service: str) -> None:
    _stack(twin).stop(service)


def api(twin: kit.KitTwin) -> TwinApi:
    """A client of the twin's API, as the toolkit makes one."""
    configure()
    manifest = parse_manifest(twin.dta.read_manifest())
    return TwinApi(manifest, read_env(twin.env_file))


def sync_run(twin: kit.KitTwin, *args: str) -> tuple[int, dict[str, Any]]:
    """`dta sync run`, asked again while the scheduler's own run holds the twin.

    A run with other parameters than the scheduler's is refused while that one
    is going; the scheduler starts one a minute, so the test waits it out.
    """
    for _ in range(BUSY_RETRIES):
        code, document = twin.run("sync", "run", *args)
        if code != 0 and document["error"]["code"] == "sync.busy":
            time.sleep(BUSY_PAUSE_S)
            continue
        return code, document
    raise AssertionError("the twin stayed busy with other sync runs")


def now() -> datetime:
    return datetime.now(UTC)
