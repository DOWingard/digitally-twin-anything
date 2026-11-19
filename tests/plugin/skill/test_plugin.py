"""The plugin's manifest and its marketplace entry: they name the plugin, carry the version
of the tool it drives, and declare every host its parts reach, the builds of its images
included."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from skilldocs import PLUGIN

PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = PLUGIN / ".claude-plugin" / "marketplace.json"
DOCKERFILES = sorted((PLUGIN / "docker").glob("*.Dockerfile"))
# `claude plugin validate` reads and writes its own configuration, so it runs only when
# asked for, and then in a home directory of its own.
VALIDATE_FLAG = "DTA_SKILL_PLUGIN_VALIDATE"
# Where docker pulls an image of Docker Hub from: the registry, its token service and
# the content delivery network the registry sends each layer's download to.
DOCKER_HUB = {"registry-1.docker.io", "auth.docker.io", "production.cloudfront.docker.com"}
_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_json_names_the_plugin_and_the_tool_version() -> None:
    data = _json(PLUGIN_JSON)
    assert data["name"] == "digitally-twin-anything"
    assert data["version"] == importlib.metadata.version("dta-toolkit")
    assert isinstance(data.get("description"), str) and data["description"].strip()


def test_the_marketplace_lists_this_plugin_at_its_root() -> None:
    market, plugin = _json(MARKETPLACE_JSON), _json(PLUGIN_JSON)
    [entry] = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
    # One place for the version and the owner's details: the plugin's own manifest.
    assert "version" not in entry
    assert market["owner"] == {"name": plugin["author"]["name"]}


def test_every_base_image_is_pinned_by_digest() -> None:
    images = [image for path in DOCKERFILES for image in _FROM.findall(path.read_text())]
    assert len(DOCKERFILES) == 2 and len(images) == 3, images
    unpinned = [image for image in images if not re.search(r":[^/@]+@sha256:[0-9a-f]{64}$", image)]
    assert not unpinned, unpinned


def test_network_hosts_cover_what_the_image_builds_pull_from() -> None:
    hosts = set(_json(PLUGIN_JSON)["network"]["hosts"])
    registries = set()
    for path in DOCKERFILES:
        for image in _FROM.findall(path.read_text(encoding="utf-8")):
            first = image.split("/", 1)[0]
            has_registry = "/" in image and ("." in first or ":" in first)
            registries.add(first if has_registry else "docker.io")
    assert registries == {"docker.io"}, registries
    assert DOCKER_HUB <= hosts
    # The installer and the build backend come from the index the lock file names, the
    # locked packages from the addresses it records.
    lock = (PLUGIN / "uv.lock").read_text(encoding="utf-8")
    pulled = set(re.findall(r'(?:url|registry) = "https://([^/"]+)', lock))
    assert pulled == {"pypi.org", "files.pythonhosted.org"}, pulled
    assert pulled <= hosts


def _validate(claude: str, home: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}
    return subprocess.run(
        [claude, "plugin", "validate", *flags, str(PLUGIN)],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )


def test_claude_accepts_the_plugin(tmp_path: Path) -> None:
    if os.environ.get(VALIDATE_FLAG) != "1":
        pytest.skip(f"set {VALIDATE_FLAG}=1 to run `claude plugin validate` in a separate home")
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("the claude command is not on PATH")
    done = _validate(claude, tmp_path, "--strict")
    said = done.stdout + done.stderr
    if done.returncode != 0 and "strict" in said and "unknown" in said.lower():
        done = _validate(claude, tmp_path)
        said = done.stdout + done.stderr
    assert done.returncode == 0, said
