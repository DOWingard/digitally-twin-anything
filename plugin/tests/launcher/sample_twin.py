"""The sample workspace's twin, driven only through the launcher.

The plain sample is a small static twin: two types, three CSV files, four
hours of readings. With `code=True` it also has code of its own (one
registered function) and a worker requirements file, so its build makes a
worker image with `docker build` from inside the toolkit's container; the one
requirement is already in the runtime image, so the build needs no network.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import launcher_kit as kit
import yaml
from dta_core.log import Log

log = Log("tests.sample_twin")

DATA_ENV = "M7IMG_DATA_DIR"
CODE_DIR = "code"
REQUIREMENTS_FILE = "worker-requirements.txt"
WORKER_PREFIX = "dta-worker-m7img-"
GATES = (
    "intake",
    "contract",
    "access",
    "profiling",
    "design",
    "gaps",
    "build",
    "acceptance",
    "handover",
)


def expect(**results: str) -> dict[str, str]:
    """Every gate's expected result: the named ones as given, earlier ones passed, later unset."""
    last = max(GATES.index(name) for name in results)
    return {
        name: results.get(name, "passed" if index <= last else "not_evaluated")
        for index, name in enumerate(GATES)
    }


class Twin:
    """One twin's workspace and stack; every document a command printed is kept."""

    def __init__(self, base: Path, home: Path, *, code: bool = False) -> None:
        self.id = f"{kit.TWIN_PREFIX}{secrets.token_hex(4)}"
        self.project = f"dta-{self.id}"
        self.code = code
        (base / "ws").mkdir()
        self.root = (base / "ws").resolve()  # the launcher mounts the physical path
        self.data = Path(shutil.copytree(kit.SAMPLE / "data", base / "data"))
        self.env = kit.environment(home)
        self.outputs: list[str] = []

    def launch(self, *args: str) -> tuple[int, dict[str, Any]]:
        done = kit.launch(["--json", *args], cwd=self.root, env=self.env)
        self.outputs.append(done.stdout)
        try:
            document = json.loads(done.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"dta {args[0]} printed no JSON document: {done.stderr}") from None
        return done.returncode, document

    def ok(self, *args: str) -> dict[str, Any]:
        code, document = self.launch(*args)
        assert code == 0, document
        return document["result"]

    def gates(self) -> dict[str, str]:
        return {gate["name"]: gate["result"] for gate in self.ok("status")["gates"]}

    def write(self) -> None:
        """The ontology, the manifest and the twin's code as the user and the agent wrote them."""
        (self.root / "ontology").mkdir(exist_ok=True)
        shutil.copy2(kit.SAMPLE / "ontology" / "plant.yaml", self.root / "ontology" / "plant.yaml")
        manifest = yaml.safe_load((kit.SAMPLE / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["twin"]["id"] = self.id
        manifest["runtime"]["image"] = kit.RUNTIME_IMAGE
        if self.code:
            shutil.copytree(kit.SAMPLE / CODE_DIR, self.root / CODE_DIR)
            shutil.copy2(kit.SAMPLE / REQUIREMENTS_FILE, self.root / REQUIREMENTS_FILE)
            manifest["runtime"]["code"] = CODE_DIR
            manifest["runtime"]["worker_requirements"] = REQUIREMENTS_FILE
            manifest["functions"]["f_label"] = {
                "description": "The upper-case label of a sensor, from the twin's own code",
                "kind": "registered",
                "entry": "plant:label",
                "complexity": "O(1)",
                "latency_budget": "PT30S",
            }
        (self.root / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )

    def fill_env(self) -> None:
        """Set the names `env template` left empty, as the user would."""
        api_port = kit.free_port()
        values = {
            "DTA_API_PORT": str(api_port),
            "DTA_UI_PORT": str(kit.free_port({api_port})),
            "DTA_BASE_URL": f"http://127.0.0.1:{api_port}",
            DATA_ENV: str(self.data),
        }
        env_file = self.root / ".env"
        lines = []
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name in values and not value:
                line = f"{name}={values[name]}"
            lines.append(line)
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")  # keeps mode 0600

    def prepare(self) -> None:
        """Every command before `build`, each expected to pass."""
        self.ok("init", "--id", self.id)
        self.write()
        self.ok("ontology", "check", "--write")
        self.ok("env", "template")
        self.fill_env()
        self.ok("source", "probe")
        self.ok("profile")
        self.ok("manifest", "validate")
        self.ok("manifest", "confirm", "--all-user")
        self.ok("manifest", "validate")
        self.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    def build_watched(self) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
        """`build`, looking at the launcher's processes and container while it runs."""
        process = subprocess.Popen(
            [str(kit.LAUNCHER), "--json", "build"],
            cwd=self.root,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        seen: list[str] = []
        container: dict[str, Any] | None = None
        try:
            while process.poll() is None:
                try:
                    seen.append(" ".join(kit.command_line(process.pid)))
                except OSError:
                    break  # it ended in between
                found = kit.toolkit_container(self.root)
                if found is not None:
                    container = found
                    seen.append(kit.container_processes(found["Id"]))
                time.sleep(0.2)
        finally:
            output, errors = process.communicate(timeout=kit.COMMAND_TIMEOUT_S)
        self.outputs.append(output)
        assert process.returncode == 0, (output, errors[-2000:])
        return json.loads(output)["result"], seen, container

    def worker_images(self) -> list[str]:
        done = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                f"dta-worker-{self.id}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return sorted(line for line in done.stdout.split() if line)

    def teardown(self) -> None:
        """The stack with its volumes, and the worker images made for this twin, by name."""
        if (self.root / "stack" / "compose.yaml").is_file():
            code, document = self.launch("down", "--destroy-data")
            if code != 0:
                log.warn("down through the launcher failed", twin=self.id, error=document)
        kit.remove_project(self.project)
        for tag in self.worker_images():
            assert tag.startswith(WORKER_PREFIX), tag
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False)
