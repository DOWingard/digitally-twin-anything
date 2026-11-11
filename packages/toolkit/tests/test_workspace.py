import subprocess
import sys

import pytest
from dta_core.errors import LockTimeout, WorkspaceError
from dta_core.manifest import parse_manifest
from dta_toolkit.workspace import State, Workspace, now


def _make(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    with workspace.exclusive():
        workspace.atomic_write(workspace.state_path, State(created_at=now()).model_dump_json())
        workspace.save_manifest(parse_manifest({"twin": {"id": "demo", "name": "Demo"}}))
    return workspace


_HOLDER = """
import sys, time
from pathlib import Path
from dta_toolkit.workspace import Workspace
with Workspace(Path(sys.argv[1])).exclusive():
    print("ready", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_writes_need_the_lock_and_stay_inside(tmp_path):
    workspace = _make(tmp_path)
    with pytest.raises(WorkspaceError) as caught:
        workspace.atomic_write(workspace.manifest_path, "x")
    assert caught.value.code == "workspace.unlocked_write"
    with workspace.exclusive(), pytest.raises(WorkspaceError) as caught:
        workspace.atomic_write(tmp_path / "elsewhere.txt", "x")
    assert caught.value.code == "workspace.outside"
    with workspace.exclusive(), pytest.raises(WorkspaceError):
        workspace.atomic_write(workspace.root / ".." / "escape.txt", "x")
    assert not (tmp_path / "elsewhere.txt").exists() and not (tmp_path / "escape.txt").exists()


def test_atomic_write_leaves_no_temporary_files(tmp_path):
    workspace = _make(tmp_path)
    with workspace.exclusive():
        workspace.atomic_write(workspace.root / "stack" / "compose.yaml", "services: {}\n")
    assert [p.name for p in (workspace.root / "stack").iterdir()] == ["compose.yaml"]


def test_lock_excludes_another_process(tmp_path):
    workspace = _make(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(workspace.root), "1.5"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(LockTimeout), workspace.exclusive(timeout=0.2):
            pass
        with workspace.exclusive(timeout=10):
            assert workspace.locked
    finally:
        holder.wait(timeout=10)
    assert not workspace.locked


def test_missing_workspace_is_reported(tmp_path):
    with pytest.raises(WorkspaceError) as caught:
        Workspace(tmp_path / "nothing").load_state()
    assert caught.value.code == "workspace.missing"


def test_gate_recording_and_fingerprint(tmp_path):
    workspace = _make(tmp_path)
    state = workspace.record_gate(0, True, subject_hash="abc", detail={"sources": 1})
    assert state.gates["intake"].passed and workspace.load_state().gates["intake"].stage == 0

    before = workspace.fingerprint()
    assert set(before) == {"manifest.yaml", ".dta/state.json"}
    (workspace.root / "tasks" / "t1" / "output").mkdir(parents=True)
    (workspace.root / "tasks" / "t1" / "output" / "result.json").write_text("{}")
    assert workspace.fingerprint() == before
    workspace.manifest_path.write_text(workspace.manifest_path.read_text() + "# edited\n")
    assert workspace.fingerprint()["manifest.yaml"] != before["manifest.yaml"]
