"""The plugin's manifest: it names the plugin and carries the version of the tool it drives."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from skilldocs import PLUGIN

PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"
# `claude plugin validate` reads and writes its own configuration, so it runs only when
# asked for, and then in a home directory of its own.
VALIDATE_FLAG = "DTA_SKILL_PLUGIN_VALIDATE"


def test_plugin_json_names_the_plugin_and_the_tool_version() -> None:
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "digitally-twin-anything"
    assert data["version"] == importlib.metadata.version("dta-toolkit")
    assert isinstance(data.get("description"), str) and data["description"].strip()


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
