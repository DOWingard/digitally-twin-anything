"""A small static twin from the sample workspace to handover, through the launcher alone.

Every command the toolkit's own flow test uses, up to `build`, `accept` and
`install-skill`, then `down --destroy-data`: run as the plugin runs them, by
`plugin/bin/dta` on the toolkit image, never by the toolkit in this
environment. Its limits are fixed here before any measurement.
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
import pytest
import yaml

pytestmark = [kit.needs_image, kit.needs_runtime]

FLOW_LIMIT_S = 300.0
DATA_ENV = "M7IMG_DATA_DIR"
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


class Twin:
    """One twin's workspace, driven only through the launcher; every document kept."""

    def __init__(self, base: Path, home: Path) -> None:
        self.id = f"{kit.TWIN_PREFIX}{secrets.token_hex(4)}"
        self.project = f"dta-{self.id}"
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
        """The ontology and the manifest as the user and the agent wrote them."""
        (self.root / "ontology").mkdir(exist_ok=True)
        shutil.copy2(kit.SAMPLE / "ontology" / "plant.yaml", self.root / "ontology" / "plant.yaml")
        manifest = yaml.safe_load((kit.SAMPLE / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["twin"]["id"] = self.id
        manifest["runtime"]["image"] = kit.RUNTIME_IMAGE
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

    def teardown(self) -> None:
        if (self.root / "stack" / "compose.yaml").is_file():
            self.launch("down", "--destroy-data")
        kit.remove_project(self.project)


@pytest.fixture
def twin(tmp_path: Path, home: Path):
    made = Twin(tmp_path, home)
    try:
        yield made
    finally:
        made.teardown()
        assert kit.leftovers(made.project) == {"containers": [], "volumes": [], "networks": []}


def expect(**results: str) -> dict[str, str]:
    """Every gate's expected result: the named ones as given, earlier ones passed, later unset."""
    last = max(GATES.index(name) for name in results)
    return {
        name: results.get(name, "passed" if index <= last else "not_evaluated")
        for index, name in enumerate(GATES)
    }


def test_static_twin_to_handover_through_the_launcher(
    twin: Twin, tmp_path: Path, home: Path
) -> None:
    started = time.perf_counter()
    created = twin.ok("init", "--id", twin.id)
    assert created["created"] is True and created["root"] == str(twin.root)
    twin.write()
    assert twin.gates()["intake"] == "passed"

    checked = twin.ok("ontology", "check", "--write")
    assert checked["written"] is True
    template = twin.ok("env", "template")
    assert sorted(template["generated"]) == ["DTA_API_TOKEN", "DTA_STORE_PASSWORD"]
    assert set(template["to_fill"]) == {"DTA_API_PORT", "DTA_BASE_URL", DATA_ENV}
    twin.fill_env()
    assert twin.ok("env", "check")["missing"] == []

    # The sources are read from the twin's own containers, which the host's daemon
    # gives the data folder: the toolkit's container never needs it mounted.
    probe = twin.ok("source", "probe")
    assert probe["ok"] is True and probe["failing"] == []
    profiled = twin.ok("profile")
    assert profiled["written"] == ["plant"] and profiled["failed"] == {}
    twin.ok("manifest", "validate")
    twin.ok("manifest", "confirm", "--all-user")
    twin.ok("manifest", "validate")
    twin.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")
    assert twin.gates() == expect(gaps="passed")

    built, seen, container = twin.build_watched()
    steps = {entry["step"]: (entry["action"], entry["status"]) for entry in built["steps"]}
    assert steps == {
        "scaffold": ("ran", "ok"),
        "image": ("ran", "skipped"),
        "store": ("ran", "ok"),
        "install": ("ran", "ok"),
        "load": ("ran", "ok"),
        "api": ("ran", "ok"),
        "contract": ("ran", "ok"),
        "sync": ("ran", "skipped"),
        "code": ("ran", "skipped"),
        "functions": ("ran", "ok"),
        "models": ("ran", "skipped"),
        "questions": ("ran", "ok"),
        "ui": ("ran", "skipped"),
        "skill": ("ran", "ok"),
    }
    load = next(entry for entry in built["steps"] if entry["step"] == "load")["detail"]
    assert load["versions_written"] == 4 and load["rows_rejected"] == 0
    assert twin.gates() == expect(build="passed")

    # The toolkit ran docker compose from its container: none of it carried a secret.
    assert container is not None and seen
    secrets_in_env = kit.env_values(twin.root / ".env")
    assert secrets_in_env
    assert not [value for value in secrets_in_env if any(value in text for text in seen)]
    assert not [value for value in secrets_in_env if value in json.dumps(container)]
    assert any("docker compose" in text for text in seen), "the compose calls were not seen"

    accepted = twin.ok("accept")
    assert accepted["passed"] is True and accepted["failed"] == []
    assert twin.gates() == expect(acceptance="passed")

    skill = twin.ok("generate", "skill")
    assert skill["name"] == f"twin-{twin.id}" and skill["written"] == []

    target = tmp_path / "agent-skills"
    installed = twin.ok("install-skill", "--target", str(target))
    destination = target / skill["name"]
    assert installed["installed"] == str(destination)
    assert sorted(p.name for p in destination.iterdir()) == sorted(
        {Path(name).parts[0] for name in skill["files"]}
    )
    handover = (twin.root / "reports" / "handover.md").read_text(encoding="utf-8")
    assert twin.id in handover and "Nothing is provisional." in handover
    assert twin.gates() == expect(handover="passed")
    assert not (home / ".claude").exists(), "a --target install must not touch HOME"

    # The default, personal scope lands in the caller's HOME: here a temporary one.
    personal = twin.ok("install-skill")
    assert personal["installed"] == str(home / ".claude" / "skills" / skill["name"])
    assert (home / ".claude" / "skills" / skill["name"] / "SKILL.md").is_file()

    written = [
        *twin.root.rglob("*"),
        *target.rglob("*"),
        *(home / ".claude").rglob("*"),
    ]
    assert kit.owned_by_caller(written) == []

    down = twin.ok("down", "--destroy-data")
    assert down["volumes_removed"] is True
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}

    flow_s = time.perf_counter() - started
    texts = twin.outputs + [
        path.read_text(encoding="utf-8")
        for path in [
            twin.root / ".dta" / "logs" / "toolkit.log",
            *sorted((twin.root / "reports").iterdir()),
            *sorted(p for p in destination.rglob("*") if p.is_file()),
        ]
        if path.is_file()
    ]
    assert not [value for value in secrets_in_env if any(value in text for text in texts)]
    print(f"\nflow through the launcher {flow_s:.1f} s (limit {FLOW_LIMIT_S:g})")
    assert flow_s <= FLOW_LIMIT_S
