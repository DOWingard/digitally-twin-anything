from pathlib import Path

import pytest
from dta_core.manifest.checks import STAGE_NAMES
from dta_toolkit.commands.init import default_id
from dta_toolkit.workspace import STAGES

# Files the toolkit itself touches on every run and that carry no workspace content.
_RUNTIME_FILES = {Path(".dta/lock")}
_RUNTIME_DIRS = {Path(".dta/logs")}


def _snapshot(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Content and modification time of every file; directories by presence only."""
    snapshot: dict[str, tuple[bytes, int] | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative in _RUNTIME_FILES or relative in _RUNTIME_DIRS:
            continue
        if any(directory in relative.parents for directory in _RUNTIME_DIRS):
            continue
        if path.is_file():
            snapshot[str(relative)] = (path.read_bytes(), path.stat().st_mtime_ns)
        else:
            snapshot[str(relative)] = None
    return snapshot


def test_init_creates_the_workspace_and_status_reports_intake(dta):
    code, document = dta.json("init")
    assert code == 0
    assert document["result"]["created"] is True
    assert document["result"]["twin"] == {"id": "pump-station", "name": "pump-station"}
    for directory in ("ontology", "profiles", "tasks", "stack"):
        assert (dta.root / directory).is_dir()
    assert (dta.root / ".dta" / "state.json").is_file()
    assert ".env" in (dta.root / ".gitignore").read_text().splitlines()
    assert dta.read_manifest()["twin"] == {
        "id": "pump-station",
        "name": "pump-station",
        "revision": 1,
    }

    code, document = dta.json("status")
    assert code == 0
    status = document["result"]
    assert status["stage"] == {"number": 0, "name": "intake"}
    assert status["twin"] == {"id": "pump-station", "name": "pump-station"}
    assert status["gaps"] == 4
    intake = status["gates"][0]
    assert intake["result"] == "failed"
    assert "contract.purpose has no value" in intake["reasons"]


def test_init_twice_changes_nothing(dta):
    assert dta.run("init", "--id", "first", "--name", "First").exit_code == 0
    before = _snapshot(dta.root)
    code, document = dta.json("init", "--id", "second", "--name", "Second")
    assert code == 0
    assert document["result"] == {"created": False, "root": str(dta.root.resolve())}
    assert _snapshot(dta.root) == before
    assert dta.read_manifest()["twin"]["id"] == "first"


def test_init_keeps_an_existing_manifest_and_gitignore(dta):
    dta.root.mkdir()
    (dta.root / "manifest.yaml").write_text("twin: {id: handmade, name: Handmade}\n")
    (dta.root / ".gitignore").write_text("*.log")
    code, document = dta.json("init")
    assert code == 0
    assert document["result"]["manifest_created"] is False
    assert (dta.root / "manifest.yaml").read_text() == "twin: {id: handmade, name: Handmade}\n"
    assert (dta.root / ".gitignore").read_text() == "*.log\n.env\n"


def test_init_rejects_an_invalid_id(dta):
    code, document = dta.json("init", "--id", "Not Valid")
    assert code == 2
    assert document["error"]["code"] == "init.bad_id"
    assert not dta.root.exists()


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("pump-station", "pump-station"),
        ("My Plant 2", "my-plant-2"),
        ("2024 Tank Farm", "tank-farm"),
        ("__", "twin"),
        ("x" * 80, "x" * 63),
    ],
)
def test_default_id_is_a_valid_id(directory, expected):
    assert default_id(directory) == expected


def test_status_without_a_workspace_cannot_run(dta):
    code, document = dta.json("status")
    assert code == 2
    assert document["error"]["code"] == "workspace.missing"


def test_gate_names_match_the_workspace_stage_names():
    assert STAGE_NAMES == STAGES
