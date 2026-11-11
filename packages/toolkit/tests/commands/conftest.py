"""Fixtures for driving `dta` through its real command line in a temporary workspace."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import Owner, Status, parse_manifest, required_fields
from dta_toolkit.cli import app
from typer.testing import CliRunner, Result

_SAMPLE = Path(__file__).with_name("complete_manifest.yaml")


def _confirm_user_fields(data: dict[str, Any]) -> dict[str, Any]:
    """The sample with every user-owned field confirmed, as the user would after discussion."""
    manifest = parse_manifest(data)
    data["field_status"] = {
        state.path: Status.confirmed.value
        for state in required_fields(manifest)
        if state.owner == Owner.user and state.status == Status.proposed
    }
    return data


class Dta:
    """Runs `dta` against one workspace and reads its files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runner = CliRunner()

    def run(self, *args: str, input: str | None = None) -> Result:
        return self.runner.invoke(app, ["--workspace", str(self.root), *args], input=input)

    def json(self, *args: str, input: str | None = None) -> tuple[int, dict[str, Any]]:
        """Run with `--json`; stdout must be exactly one JSON document."""
        result = self.run("--json", *args, input=input)
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            result.exception
        )
        return result.exit_code, json.loads(result.stdout)

    def write_manifest(self, data: dict[str, Any]) -> None:
        self.root.joinpath("manifest.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )

    def read_manifest(self) -> dict[str, Any]:
        return yaml.safe_load(self.root.joinpath("manifest.yaml").read_text(encoding="utf-8"))

    def state(self) -> dict[str, Any]:
        return json.loads(self.root.joinpath(".dta", "state.json").read_text(encoding="utf-8"))

    def gate(self, name: str) -> dict[str, Any]:
        code, document = self.json("status")
        assert code == 0, document
        return next(gate for gate in document["result"]["gates"] if gate["name"] == name)


@pytest.fixture(scope="session")
def _complete_template() -> dict[str, Any]:
    return _confirm_user_fields(yaml.safe_load(_SAMPLE.read_text(encoding="utf-8")))


@pytest.fixture
def complete(_complete_template: dict[str, Any]) -> dict[str, Any]:
    """A fresh copy of the complete sample manifest, safe to mutate."""
    return copy.deepcopy(_complete_template)


@pytest.fixture
def dta(tmp_path: Path) -> Dta:
    """A `dta` runner bound to a not-yet-initialised workspace directory."""
    return Dta(tmp_path / "pump-station")


@pytest.fixture
def workspace(dta: Dta) -> Dta:
    """An initialised workspace, manifest still the skeleton."""
    result = dta.run("init")
    assert result.exit_code == 0, result.output
    return dta
