"""A twin workspace built with the workspace API, and ways to run `dta tasks` against it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from dta_core.manifest import Manifest, parse_manifest
from dta_toolkit.cli import app
from dta_toolkit.workspace import State, Workspace, now
from typer.testing import CliRunner

_LAUNCHER = Path(__file__).with_name("launch.py")
_DTA = Path(sys.executable).with_name("dta")
ONTOLOGY_FILE = "ontology/domain.yaml"


def sample_manifest() -> dict[str, Any]:
    """Three sources and three questions, one of them predictive and one prescriptive."""
    return {
        "twin": {"id": "plant", "name": "Plant"},
        "contract": {
            "purpose": "Keep the line running",
            "tier": "live",
            "questions": {
                "q1": {"text": "What is the throughput now?", "kind": "descriptive"},
                "q2": {"text": "What will demand be tomorrow?", "kind": "predictive"},
                "q3": {"text": "Which setpoint is best?", "kind": "prescriptive"},
            },
        },
        "ontology": {"path": ONTOLOGY_FILE},
        "sources": {
            "erp": {"connector": "sql", "location": "postgresql://${ERP_HOST}/erp"},
            "sensors": {"connector": "http", "location": "https://${SENSOR_HOST}/v1"},
            "sheets": {"connector": "file", "location": "data/*.csv"},
        },
    }


def designed_manifest() -> dict[str, Any]:
    """The sample after design: models with candidates, functions, questions pointing at them."""
    manifest = sample_manifest()
    questions = manifest["contract"]["questions"]
    questions["q2"].update(model="q2", function="q2")
    questions["q3"].update(model="q3", function="q3")
    manifest["models"] = {
        "q2": {
            "question": "q2",
            "candidates": {"arima": {"family": "arima"}, "ets": {"family": "ets"}},
        },
        "q3": {"question": "q3", "candidates": {"gbm": {"family": "gbm"}}},
    }
    manifest["functions"] = {
        "q1": {"kind": "builtin", "entry": "latest"},
        "q2": {"kind": "registered", "entry": "forecast:run"},
        "q3": {"kind": "builtin", "entry": "argmax"},
        "q3-policy": {"kind": "registered", "entry": "policy:choose"},
    }
    return manifest


def profile_patch(source: str, rows: int = 3) -> dict[str, Any]:
    """A manifest patch a profiling task could legitimately return."""
    return {
        f"sources.{source}.profile": {
            "profiled_at": "2026-01-01T00:00:00Z",
            "datasets": {
                "main": {
                    "rows": rows,
                    "columns": {"id": {"dtype": "int", "null_fraction": 0, "distinct_count": rows}},
                }
            },
        },
        f"sources.{source}.refresh_period": "PT1H",
    }


@dataclass
class Outcome:
    code: int
    payload: dict[str, Any] | None  # the single JSON document the command printed
    stderr: str = ""

    @property
    def result(self) -> dict[str, Any]:
        assert self.code == 0, f"exit {self.code}: {self.payload}"
        return self.payload["result"]

    @property
    def error(self) -> dict[str, Any]:
        assert self.code != 0 and self.payload is not None, f"exit {self.code}: {self.payload}"
        return self.payload["error"]

    def refused(self, code: str) -> dict[str, Any]:
        """Assert the command ran, said no with this code, and return the detail."""
        assert (self.code, self.error["code"]) == (1, code), self.payload
        return self.error["detail"]

    def could_not_run(self, code: str) -> dict[str, Any]:
        assert (self.code, self.error["code"]) == (2, code), self.payload
        return self.error["detail"]

    @property
    def reasons(self) -> list[str]:
        """Codes of every failed check of a refused submit."""
        return [entry["code"] for entry in self.refused("tasks.submit.refused")["reasons"]]

    def reason(self, code: str) -> dict[str, Any]:
        matches = [entry for entry in self.error["detail"]["reasons"] if entry["code"] == code]
        assert len(matches) == 1, self.payload
        return matches[0]["detail"]


def _outcome(code: int, stdout: str, stderr: str = "") -> Outcome:
    text = stdout.strip()
    return Outcome(code, json.loads(text) if text else None, stderr)


class Harness:
    profile_patch = staticmethod(profile_patch)

    def __init__(self, root: Path, manifest: dict[str, Any] | None = None) -> None:
        self.root = root
        self.workspace = Workspace(root)
        with self.workspace.exclusive():
            self.workspace.atomic_write(
                self.workspace.state_path, State(created_at=now()).model_dump_json()
            )
            self.workspace.save_manifest(parse_manifest(manifest or sample_manifest()))
            self.workspace.atomic_write(root / ONTOLOGY_FILE, "classes: {}\n")
        self.tasks = self.workspace.tasks_dir
        self._env = {k: v for k, v in os.environ.items() if k != "DTA_WORKSPACE"}

    # -- running the command line ------------------------------------------

    def _argv(self, args: tuple[str, ...]) -> list[str]:
        return ["--workspace", str(self.root), "--json", "tasks", *args]

    def run(self, *args: str) -> Outcome:
        """Run `dta tasks ...` in this process."""
        done = CliRunner().invoke(app, self._argv(args))
        return _outcome(done.exit_code, done.stdout, done.stderr)

    def spawn(self, *args: str) -> Outcome:
        """Run `dta tasks ...` through the installed entry point, in a process of its own."""
        done = subprocess.run(
            [str(_DTA), *self._argv(args)],
            capture_output=True,
            text=True,
            env=self._env,
            timeout=120,
        )
        return _outcome(done.returncode, done.stdout, done.stderr)

    def race(self, *commands: tuple[str, ...]) -> list[Outcome]:
        """Run several commands at once, each in its own process, released together."""
        line = self.root.parent / f"line-{time.monotonic_ns()}"
        line.mkdir()
        start = line / "go"
        runners = []
        for index, args in enumerate(commands):
            ready = line / f"ready-{index}"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_LAUNCHER),
                    "--ready",
                    str(ready),
                    "--start",
                    str(start),
                    *self._argv(args),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env,
            )
            runners.append((process, ready))
        deadline = time.monotonic() + 60
        while not all(ready.exists() for _, ready in runners):
            assert time.monotonic() < deadline, "processes did not reach the start line"
            assert all(process.poll() is None for process, _ in runners), "a process died early"
            time.sleep(0.005)
        start.touch()
        outcomes = []
        for process, _ in runners:
            stdout, stderr = process.communicate(timeout=120)
            outcomes.append(_outcome(process.returncode, stdout, stderr))
        return outcomes

    def crash(self, gap: str, *args: str) -> int:
        """Run a command in its own process and kill it at the named gap between two writes."""
        done = subprocess.run(
            [sys.executable, str(_LAUNCHER), "--die-at", gap, *self._argv(args)],
            capture_output=True,
            text=True,
            env=self._env,
            timeout=120,
        )
        assert done.returncode == 70, f"the command never reached {gap}: {done.stdout}"
        return done.returncode

    # -- the steps of a task's life ----------------------------------------

    def open(self, stage: str = "3", *extra: str) -> dict[str, Any]:
        return self.run("plan", "--stage", stage, "--open", *extra).result

    def claim(self, task: str, holder: str = "agent-1", ttl: int | None = None) -> str:
        extra = ("--ttl", str(ttl)) if ttl is not None else ()
        return self.run("claim", task, "--holder", holder, *extra).result["token"]

    def output(self, task: str) -> Path:
        return self.tasks / task / "output"

    def write_result(
        self,
        task: str,
        patch: dict[str, Any] | None = None,
        *,
        files: dict[str, str] | None = None,
        proposals: list[dict[str, Any]] | None = None,
        artifacts: list[str] | None = None,
        notes: str = "",
    ) -> None:
        files = files or {}
        for name, content in files.items():
            target = self.output(task) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        document = {
            "manifest_patch": patch or {},
            "proposals": proposals or [],
            "artifacts": sorted(files) if artifacts is None else artifacts,
            "notes": notes,
        }
        (self.output(task) / "result.json").write_text(json.dumps(document))

    def submit(self, task: str, token: str) -> Outcome:
        return self.run("submit", task, "--token", token)

    def finish(self, task: str, patch: dict[str, Any] | None = None, **result: Any) -> str:
        """Claim a task, write a result and submit it; returns the lease token."""
        token = self.claim(task, holder=f"agent-{task}")
        self.write_result(task, patch, **result)
        accepted = self.submit(task, token)
        assert accepted.code == 0, accepted.payload
        return token

    def merge(self, *args: str) -> dict[str, Any]:
        """Merge (everything submitted unless a task is named) and expect it to go through."""
        return self.run("merge", *(args or ("--all",))).result

    def abort(self, *args: str) -> dict[str, Any]:
        return self.run("abort", *(args or ("--batch",))).result

    # -- looking at the workspace ------------------------------------------

    def manifest(self) -> Manifest:
        return self.workspace.load_manifest()

    def manifest_bytes(self) -> bytes:
        return self.workspace.manifest_path.read_bytes()

    def state(self) -> dict[str, Any]:
        return json.loads(self.workspace.tasks_state_path.read_text())

    def state_bytes(self) -> bytes:
        return self.workspace.tasks_state_path.read_bytes()

    def task(self, task: str) -> dict[str, Any]:
        """The most recent record of a task in the state file."""
        for batch in reversed(self.state()["batches"]):
            if task in batch["tasks"]:
                return batch["tasks"][task]
        raise AssertionError(f"no record of {task}")

    def status(self) -> dict[str, Any]:
        return self.run("status").result

    def clean(self) -> bool:
        """True when a batch is open and neither shared files nor task directories are disturbed."""
        return self.run("status", "--verify").result["verified"]["clean"]

    def edit_manifest(self, change) -> None:
        """Change the manifest the way another toolkit command would: locked and recorded."""
        with self.workspace.exclusive():
            manifest = self.workspace.load_manifest()
            change(manifest)
            self.workspace.save_manifest(manifest)


@pytest.fixture
def sample() -> dict[str, Any]:
    return sample_manifest()


@pytest.fixture
def designed() -> dict[str, Any]:
    return designed_manifest()


@pytest.fixture
def ws(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "ws")


@pytest.fixture
def make_ws(tmp_path: Path):
    def make(name: str, manifest: dict[str, Any] | None = None) -> Harness:
        return Harness(tmp_path / name, manifest)

    return make
