"""Commands run against a twin's stack: real docker, the twin's own image, a real source."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_toolkit.envfile import MASK, EnvFileError, missing, read_env, scrub
from dta_toolkit.stack import Stack, StackError, reported_error
from dta_toolkit.workspace import State, Workspace, now

RUNTIME_IMAGE = "dta-runtime:0.1.0"


def _image_present(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "image", "inspect", name], capture_output=True, check=False)
    return done.returncode == 0


needs_stack = pytest.mark.skipif(
    not _image_present(RUNTIME_IMAGE),
    reason=f"needs docker and the image {RUNTIME_IMAGE}, built from docker/runtime.Dockerfile",
)


def _document(twin_id: str) -> dict[str, Any]:
    return {
        "twin": {"id": twin_id, "name": "Runner test"},
        "sources": {
            "plant": {
                "connector": "file",
                "location": "file://${PLANT_DIR}",
                "env": ["PLANT_DIR"],
                "options": {"datasets": {"readings": {"glob": "*.csv", "format": "csv"}}},
            }
        },
        "interfaces": {"ui": False},
    }


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "plant-data"
    folder.mkdir()
    (folder / "readings.csv").write_text("sensor,value\na,1.5\nb,2.5\n", encoding="utf-8")
    return folder


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    current = Workspace(tmp_path / "ws")
    with current.exclusive():
        current.atomic_write(current.state_path, State(created_at=now()).model_dump_json())
    return current


@pytest.fixture
def manifest() -> Manifest:
    return parse_manifest(_document(f"runner-{secrets.token_hex(4)}"), origin="test")


def _write_env(workspace: Workspace, values: dict[str, str]) -> None:
    text = "".join(f"{name}={value}\n" for name, value in values.items())
    workspace.env_path.write_text(text, encoding="utf-8")
    workspace.env_path.chmod(0o600)


def _complete_env(source_dir: Path) -> dict[str, str]:
    return {
        "DTA_STORE_PASSWORD": secrets.token_urlsafe(24),
        "DTA_API_TOKEN": secrets.token_urlsafe(32),
        "DTA_API_PORT": "18790",
        "PLANT_DIR": str(source_dir),
    }


# -- the environment file ------------------------------------------------------


def test_env_file_names_are_read_and_empty_ones_count_as_missing(tmp_path: Path):
    path = tmp_path / ".env"
    assert read_env(path) == {}
    path.write_text('A=1\nB="two words"\n# note\nC=\nexport D=4\n', encoding="utf-8")
    values = read_env(path)
    assert values == {"A": "1", "B": "two words", "C": "", "D": "4"}
    assert missing(["A", "C", "E", "A"], values) == ["C", "E"]


def test_an_unreadable_env_file_is_an_error_without_its_content(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(EnvFileError) as caught:
        read_env(path)
    assert caught.value.code == "env.unreadable"


def test_scrub_masks_values_long_enough_to_be_secrets():
    values = {"TOKEN": "s3cr3t-token-value", "PORT": "8000", "EMPTY": ""}
    text = scrub("auth s3cr3t-token-value failed on port 8000", values)
    assert text == f"auth {MASK} failed on port 8000"


# -- what a runtime command printed --------------------------------------------


def test_only_a_filled_error_entry_makes_a_document_an_error():
    assert reported_error({"status": "succeeded", "error": None, "rows_written": 3}) is None
    assert reported_error({"ok": True}) is None
    failure = {"code": "sync.source_failed", "message": "cannot be read", "detail": {}}
    assert reported_error({"error": failure}) == failure


# -- files and environment, without docker ------------------------------------


def test_write_is_idempotent_and_follows_the_manifest(workspace: Workspace, manifest: Manifest):
    stack = Stack(workspace, manifest)
    assert stack.write() is True
    first = stack.compose_path.read_text("utf-8")
    assert stack.write() is False
    assert yaml.safe_load(first)["name"] == stack.plan.project

    changed = manifest.model_copy(deep=True)
    changed.interfaces.ui = True
    assert Stack(workspace, changed).write() is True
    assert "ui" in yaml.safe_load(stack.compose_path.read_text("utf-8"))["services"]


def test_missing_variables_are_named_before_docker_is_called(
    workspace: Workspace, manifest: Manifest, source_dir: Path
):
    stack = Stack(workspace, manifest)
    assert stack.missing_env() == [
        "DTA_STORE_PASSWORD",
        "DTA_API_TOKEN",
        "DTA_API_PORT",
        "PLANT_DIR",
    ]
    env = _complete_env(source_dir)
    env["DTA_API_TOKEN"] = ""
    _write_env(workspace, env)
    with pytest.raises(StackError) as caught:
        stack.compose("ps")
    assert caught.value.code == "stack.env_missing"
    assert caught.value.detail["missing"] == ["DTA_API_TOKEN"]
    assert env["DTA_STORE_PASSWORD"] not in str(caught.value.as_dict())


# -- against docker ------------------------------------------------------------


@pytest.fixture
def stack(workspace: Workspace, manifest: Manifest, source_dir: Path) -> Iterator[Stack]:
    _write_env(workspace, _complete_env(source_dir))
    current = Stack(workspace, manifest)
    try:
        yield current
    finally:
        current.down(volumes=True)


@needs_stack
def test_a_runtime_command_reads_the_source_from_inside_the_stack(stack: Stack):
    text = yaml.safe_dump(_document(stack.plan.twin_id))
    document = stack.runtime("probe", input=text, store=False)
    assert document["ok"] is True
    (source,) = document["sources"]
    assert [d["dataset"] for d in source["datasets"]] == ["readings"]
    # Nothing but the one-off container was started for it.
    assert stack.services() == []


@needs_stack
def test_a_runtime_error_keeps_its_own_code(stack: Stack):
    text = yaml.safe_dump(_document(stack.plan.twin_id))
    with pytest.raises(StackError) as caught:
        stack.runtime("probe", "--source", "nope", input=text, store=False)
    assert caught.value.code == "runtime.unknown_source"
    assert caught.value.detail["command"] == "probe"
    assert caught.value.detail["sources"] == ["plant"]


@needs_stack
def test_values_come_from_the_env_file_not_from_the_callers_shell(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DTA_API_PORT", "1")
    monkeypatch.setenv("PLANT_DIR", "/nonexistent")
    done = stack.compose("config", "--format", "json")
    assert '"published": "18790"' in done.stdout
    assert "/nonexistent" not in done.stdout


@needs_stack
def test_store_comes_up_healthy_and_goes_down_with_its_data(stack: Stack):
    stack.up("store")
    (store,) = stack.services()
    assert (store["service"], store["state"], store["health"]) == ("store", "running", "healthy")
    assert "store" in stack.logs("store", tail=5) or stack.logs("store", tail=5) == ""
    stack.down(volumes=True)
    assert stack.services() == []
    volumes = subprocess.run(
        ["docker", "volume", "ls", "--quiet", "--filter", f"name={stack.plan.project}_"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert volumes.stdout.strip() == ""


@needs_stack
def test_a_failing_compose_command_reports_its_exit_code_without_secrets(stack: Stack):
    with pytest.raises(StackError) as caught:
        stack.compose("up", "--detach", "no-such-service")
    assert caught.value.code == "stack.command_failed"
    assert caught.value.detail["exit_code"] != 0
    secret = read_env(stack.env_file)["DTA_API_TOKEN"]
    assert secret not in str(caught.value.as_dict())


def test_docker_absent_is_its_own_error(
    workspace: Workspace, manifest: Manifest, source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_env(workspace, _complete_env(source_dir))
    monkeypatch.setenv("PATH", os.devnull)
    with pytest.raises(StackError) as caught:
        Stack(workspace, manifest).compose("ps")
    assert caught.value.code == "stack.docker_unavailable"
