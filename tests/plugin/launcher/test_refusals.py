"""The launcher refuses, in one line on stderr, what it cannot run.

64 is wrong use (a working directory or workspace outside what is mounted, a
mount that does not exist, a daemon that is not on a unix socket); 69 is a
service that is not there (docker, its daemon, the image). Nothing is pulled.
"""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import tomllib
from pathlib import Path

import launcher_kit as kit
import pytest

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker command")

# What the launcher runs besides docker and the shell's own builtins.
_TOOLS = ("env", "sed", "ls", "id", "readlink", "mkdir")


def _absent_image() -> str:
    return f"dta-toolkit:{kit.TWIN_PREFIX}absent-{secrets.token_hex(4)}"


def test_version_is_the_toolkits() -> None:
    project = tomllib.loads(
        (kit.REPO / "packages" / "toolkit" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert kit.VERSION_FILE.read_text(encoding="utf-8").splitlines() == [
        project["project"]["version"]
    ]


def test_refused_without_docker(tmp_path: Path, workdir: Path, home: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        assert found is not None, name
        (tools / name).symlink_to(found)
    done = kit.launch(["--help"], cwd=workdir, env=kit.environment(home, PATH=str(tools)))
    assert done.returncode == kit.EX_UNAVAILABLE
    assert "docker is not installed" in kit.refusal(done)


@needs_docker
def test_refused_without_the_image_and_nothing_is_pulled(workdir: Path, home: Path) -> None:
    image = _absent_image()
    done = kit.launch(["--help"], cwd=workdir, env=kit.environment(home, DTA_TOOLKIT_IMAGE=image))
    assert done.returncode == kit.EX_UNAVAILABLE
    line = kit.refusal(done)
    assert f"the image {image} is not present" in line
    assert "docker/toolkit.Dockerfile" in line and "pull" in line
    inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    assert inspected.returncode != 0, "the launcher must not pull the image"


@needs_docker
def test_default_image_is_named_by_the_version_file_next_to_the_launcher(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    installed = tmp_path / "plugin" / "bin"
    installed.mkdir(parents=True)
    copy = installed / "dta"
    shutil.copy2(kit.LAUNCHER, copy)
    version = f"0.0.0-{kit.TWIN_PREFIX}{secrets.token_hex(4)}"
    (installed / "VERSION").write_text(version + "\n", encoding="utf-8")
    # Called through a link elsewhere, as a PATH entry might be.
    linked = tmp_path / "elsewhere" / "dta"
    linked.parent.mkdir()
    linked.symlink_to(copy)
    env = kit.environment(home)
    del env["DTA_TOOLKIT_IMAGE"]
    for launcher in (copy, linked):
        done = kit.launch(["--help"], cwd=workdir, env=env, launcher=launcher)
        assert done.returncode == kit.EX_UNAVAILABLE
        assert f"the image dta-toolkit:{version} is not present" in kit.refusal(done)


def test_refused_outside_dta_home(tmp_path: Path, home: Path) -> None:
    mounted, elsewhere = tmp_path / "mounted", tmp_path / "elsewhere"
    mounted.mkdir()
    elsewhere.mkdir()
    done = kit.launch(["status"], cwd=elsewhere, env=kit.environment(home, DTA_HOME=str(mounted)))
    assert done.returncode == kit.EX_USAGE
    assert "is not inside DTA_HOME" in kit.refusal(done)


def test_refused_when_dta_home_is_no_directory(tmp_path: Path, workdir: Path, home: Path) -> None:
    done = kit.launch(
        ["status"], cwd=workdir, env=kit.environment(home, DTA_HOME=str(tmp_path / "none"))
    )
    assert done.returncode == kit.EX_USAGE
    assert "DTA_HOME" in kit.refusal(done)


def test_refused_at_the_root_of_the_file_system(home: Path) -> None:
    done = kit.launch(["status"], cwd=Path("/"), env=kit.environment(home))
    assert done.returncode == kit.EX_USAGE
    assert "/ cannot be mounted" in kit.refusal(done)


@pytest.mark.parametrize(
    "workspace_args",
    [
        ["-w", "{outside}"],
        ["--workspace", "{outside}"],
        ["--workspace={outside}"],
        ["-w{outside}"],
        ["--json", "-w", "../elsewhere/ws"],
    ],
)
def test_refused_for_a_workspace_outside_the_mounts(
    tmp_path: Path, workdir: Path, home: Path, workspace_args: list[str]
) -> None:
    outside = tmp_path / "elsewhere" / "ws"
    args = [arg.replace("{outside}", str(outside)) for arg in workspace_args]
    done = kit.launch([*args, "init"], cwd=workdir, env=kit.environment(home))
    assert done.returncode == kit.EX_USAGE
    assert "is not inside a mounted directory" in kit.refusal(done)
    assert not outside.exists()


def test_refused_for_dta_workspace_outside_the_mounts(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    env = kit.environment(home, DTA_WORKSPACE=str(tmp_path / "elsewhere"))
    done = kit.launch(["status"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_USAGE
    assert "DTA_WORKSPACE" in kit.refusal(done)


@pytest.mark.parametrize("entry", ["{missing}", "relative/dir", "{file}"])
def test_refused_for_a_mount_that_does_not_exist(
    tmp_path: Path, workdir: Path, home: Path, entry: str
) -> None:
    afile = tmp_path / "a-file"
    afile.write_text("", encoding="utf-8")
    value = entry.replace("{missing}", str(tmp_path / "missing")).replace("{file}", str(afile))
    done = kit.launch(
        ["status"], cwd=workdir, env=kit.environment(home, DTA_MOUNTS=f"{tmp_path}:{value}")
    )
    assert done.returncode == kit.EX_USAGE
    assert f"DTA_MOUNTS: {value}" in kit.refusal(done)


@needs_docker
def test_refused_for_a_daemon_that_is_not_on_a_unix_socket(workdir: Path, home: Path) -> None:
    env = kit.environment(home, DOCKER_HOST="tcp://127.0.0.1:2375")
    done = kit.launch(["--help"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_USAGE
    assert "not to a unix socket" in kit.refusal(done)


@needs_docker
def test_refused_without_a_daemon_socket(tmp_path: Path, workdir: Path, home: Path) -> None:
    env = kit.environment(home, DOCKER_HOST=f"unix://{tmp_path / 'none.sock'}")
    done = kit.launch(["--help"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_UNAVAILABLE
    assert "the docker daemon is not reachable: no socket" in kit.refusal(done)


@needs_docker
def test_refused_when_the_daemon_does_not_answer(tmp_path: Path, workdir: Path, home: Path) -> None:
    path = tmp_path / "silent.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.bind(str(path))  # bound, never listening: every connection is refused
        env = kit.environment(home, DOCKER_HOST=f"unix://{path}")
        done = kit.launch(["--help"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_UNAVAILABLE
    assert "the docker daemon is not reachable" in kit.refusal(done)


def test_skill_directory_is_not_created_when_refused(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    env = kit.environment(home, DOCKER_HOST="tcp://127.0.0.1:2375")
    target = tmp_path / "skills"
    for args in (["install-skill"], ["install-skill", "--target", str(target)]):
        done = kit.launch(args, cwd=workdir, env=env)
        assert done.returncode in (kit.EX_USAGE, kit.EX_UNAVAILABLE)
    assert not (home / ".claude").exists() and not target.exists()
