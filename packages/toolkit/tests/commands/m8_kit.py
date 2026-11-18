"""The plant twin of the command tests with its add-ons selected.

The twin's ids start with `m8add-` and its host ports lie in 13382-13389, so its
stack never collides with another one on this machine; one runs at a time.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from m3tk_kit import KitTwin, leftovers

PROJECT_PREFIX = "dta-m8add-"
PORTS = range(13382, 13390)
ADDON_ENV = {
    "DTA_MCP_PORT": {"description": "MCP adapter port on the host", "secret": False},
    "DTA_SQL_PORT": {"description": "Read-only SQL port on the host", "secret": False},
    "DTA_SQL_READER_PASSWORD": {"description": "Password of the store's reader"},
}
# The installer's cache stays the machine's, though the tests move HOME.
UV_CACHE = os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")


def free_port(taken: set[int]) -> int:
    for port in PORTS:
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port in 13382-13389")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def remove_project(project: str) -> None:
    """Remove one of this kit's stacks with its volumes, by its compose project name."""
    assert project.startswith(PROJECT_PREFIX), project
    if shutil.which("docker") is None:
        return
    subprocess.run(
        ["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
        timeout=300,
    )


class AddonTwin(KitTwin):
    """A kit twin whose manifest selects add-ons; `revise` selects others afterwards."""

    def __init__(self, dta: Any, base: Path, addons: dict[str, bool]) -> None:
        super().__init__(dta, base)
        self.id = f"m8add-{secrets.token_hex(4)}"
        self.project = f"dta-{self.id}"
        self.addons = dict(addons)
        self.ports: dict[str, int] = {}

    def write(self) -> None:
        super().write()
        data = self.dta.read_manifest()
        data["contract"]["addons"] = dict(self.addons)
        data["env"].update(ADDON_ENV)
        self.dta.write_manifest(data)

    def fill_env(self) -> int:
        taken: set[int] = set()
        for name in ("DTA_API_PORT", "DTA_UI_PORT", "DTA_MCP_PORT", "DTA_SQL_PORT"):
            self.ports[name] = free_port(taken)
            taken.add(self.ports[name])
        values = {name: str(port) for name, port in self.ports.items()}
        values["M3TK_DATA_DIR"] = str(self.data)
        values["DTA_BASE_URL"] = f"http://127.0.0.1:{self.ports['DTA_API_PORT']}"
        lines = []
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name in values and not value:
                line = f"{name}={values[name]}"
            lines.append(line)
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.ports["DTA_API_PORT"]

    def env(self) -> dict[str, str]:
        found = {}
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                name, _, value = line.partition("=")
                found[name] = value
        return found

    def revise(self, **addons: bool) -> None:
        """Select other add-ons, and sign the manifest again as the user would."""
        data = self.dta.read_manifest()
        data["contract"]["addons"].update(addons)
        self.dta.write_manifest(data)
        self.ok("manifest", "validate")
        self.ok("manifest", "confirm", "--all-user")
        self.ok("manifest", "validate")
        self.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    def teardown(self) -> None:
        if (self.root / "compose.yaml").is_file():
            self.dta.json("down", "--destroy-data")
        if any(leftovers(self.project).values()):
            remove_project(self.project)
        left = leftovers(self.project)
        assert not any(left.values()), left


def install_into_fresh_venv(package_dir: Path, scratch: Path) -> Path:
    """A new virtual environment holding the package and its own dependencies only."""
    venv = scratch / "venv"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(scratch), "UV_CACHE_DIR": UV_CACHE}
    env["UV_NO_CONFIG"] = "1"
    subprocess.run(
        ["uv", "venv", "--quiet", "--python", sys.executable, str(venv)],
        env=env,
        check=True,
        capture_output=True,
        timeout=300,
    )
    started = time.perf_counter()
    done = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(venv / "bin" / "python"),
            str(package_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    print(f"installed in {time.perf_counter() - started:.1f} s")
    return venv / "bin" / "python"


def run_in(
    python: Path, code: str, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-I", "-c", code, *args],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def code_block(text: str, language: str, after: str) -> str:
    """The first fenced block of `language` after the heading `after` in a Markdown text."""
    start = text.index(after)
    found = re.search(rf"```{language}\n(.*?)```", text[start:], re.DOTALL)
    assert found, f"no {language} block after {after!r}"
    return found.group(1)


def manifest_of(twin: AddonTwin) -> dict[str, Any]:
    return yaml.safe_load((twin.root / "manifest.yaml").read_text(encoding="utf-8"))
