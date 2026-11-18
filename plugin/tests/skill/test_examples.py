"""The two example workspaces pass the tool's own checks as they stand at the end of
design, with nothing open but what the user confirms and what the tool measures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_toolkit.cli import app
from skilldocs import REFERENCE
from typer.testing import CliRunner

EXAMPLES = sorted(path for path in (REFERENCE / "examples").iterdir() if path.is_dir())
# Open fields the tool writes itself once it can reach the sources.
MEASURED = ("sources.{source}.profile", "sources.{source}.refresh_period")
DERIVED = ("sync.interval",)


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runner = CliRunner()

    def json(self, *args: str) -> tuple[int, dict[str, Any]]:
        result = self.runner.invoke(app, ["--workspace", str(self.root), "--json", *args])
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            result.exception
        )
        return result.exit_code, json.loads(result.stdout)


@pytest.fixture(params=EXAMPLES, ids=[path.name for path in EXAMPLES])
def example(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Workspace, dict[str, Any]]:
    source: Path = request.param
    data = yaml.safe_load((source / "manifest.yaml").read_text(encoding="utf-8"))
    workspace = Workspace(tmp_path / data["twin"]["id"])
    code, document = workspace.json(
        "init", "--id", data["twin"]["id"], "--name", data["twin"]["name"]
    )
    assert code == 0, document
    shutil.copytree(source, workspace.root, dirs_exist_ok=True)
    return workspace, data


def test_examples_exist() -> None:
    assert [path.name for path in EXAMPLES] == ["live-coldstore", "static-greenhouse"]


def test_example_ontology_matches_its_signature(example: tuple[Workspace, dict]) -> None:
    workspace, _ = example
    code, document = workspace.json("ontology", "check")
    assert code == 0, document
    assert document["result"]["agree"] is True


def test_example_validates_without_findings(example: tuple[Workspace, dict]) -> None:
    workspace, _ = example
    code, document = workspace.json("manifest", "validate")
    assert code == 0, document
    assert document["result"]["findings"] == []


def test_example_leaves_open_only_confirmations_and_measurements(
    example: tuple[Workspace, dict],
) -> None:
    workspace, data = example
    code, document = workspace.json("manifest", "gaps")
    assert code == 0, document
    by_owner = document["result"]["by_owner"]
    assert by_owner["user"], "the example should still wait for the user's confirmation"
    assert {gap["status"] for gap in by_owner["user"]} == {"proposed"}
    allowed = {
        pattern.format(source=source) for source in data["sources"] for pattern in MEASURED
    } | set(DERIVED)
    assert {gap["path"] for gap in by_owner["agent"]} <= allowed


def test_example_passes_intake(example: tuple[Workspace, dict]) -> None:
    workspace, _ = example
    code, document = workspace.json("status")
    assert code == 0, document
    gates = {gate["name"]: gate["result"] for gate in document["result"]["gates"]}
    assert gates["intake"] == "passed"
