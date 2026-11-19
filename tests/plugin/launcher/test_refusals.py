"""The launcher refuses, in one line on stderr, what it cannot run, and says what to do.

64 is wrong use (a working directory or workspace outside what is mounted, a
mount that does not exist, a daemon that is not on a unix socket); 69 is a
service that is not there (docker, its daemon, the image). An image is never
pulled; one named by DTA_TOOLKIT_IMAGE, or without sources beside the
launcher, is never built either (building is covered by test_first_use).
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
from dta_core.manifest.models import Runtime

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker command")


def _absent_image() -> str:
    return f"dta-toolkit:{kit.TWIN_PREFIX}absent-{secrets.token_hex(4)}"


def test_version_is_the_toolkits() -> None:
    project = tomllib.loads(
        (kit.REPO / "packages" / "toolkit" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert kit.VERSION_FILE.read_text(encoding="utf-8").splitlines() == [
        project["project"]["version"]
    ]


def test_the_runtime_image_built_is_the_one_a_manifest_names_by_default() -> None:
    # The launcher builds dta-runtime:<VERSION>; a twin runs what runtime.image names.
    version = kit.VERSION_FILE.read_text(encoding="utf-8").strip()
    assert Runtime().image == f"dta-runtime:{version}"


def test_refused_without_docker(tmp_path: Path, workdir: Path, home: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in kit.LAUNCHER_TOOLS:
        found = shutil.which(name)
        assert found is not None, name
        (tools / name).symlink_to(found)
    done = kit.launch(["--help"], cwd=workdir, env=kit.environment(home, PATH=str(tools)))
    assert done.returncode == kit.EX_UNAVAILABLE
    line = kit.refusal(done)
    assert "docker is not installed" in line and "install Docker" in line


@needs_docker
def test_an_image_dta_toolkit_image_names_is_neither_pulled_nor_built(
    workdir: Path, home: Path
) -> None:
    # This launcher has the sources of the images beside it; the variable still wins.
    assert (kit.LAUNCHER.parents[1] / "docker" / "toolkit.Dockerfile").is_file()
    image = _absent_image()
    done = kit.launch(["--help"], cwd=workdir, env=kit.environment(home, DTA_TOOLKIT_IMAGE=image))
    assert done.returncode == kit.EX_UNAVAILABLE
    line = kit.refusal(done)
    assert f"the image {image} is not present" in line
    assert "not built since DTA_TOOLKIT_IMAGE names it" in line
    assert "docker/toolkit.Dockerfile" in line and "pull" in line
    inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    assert inspected.returncode != 0, "the launcher must not pull the image"
    assert not (home / ".local" / "state" / "dta").exists(), "nothing was to be built"


@needs_docker
def test_without_sources_beside_the_launcher_an_absent_image_is_refused(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    version = f"0.0.0-{kit.TWIN_PREFIX}{secrets.token_hex(4)}"
    copy = kit.plugin_copy(tmp_path / "plugin", version, sources=False)
    # Called through a link elsewhere, as a PATH entry might be: VERSION and the
    # sources are looked for beside the launcher itself, not beside the link.
    linked = tmp_path / "elsewhere" / "dta"
    linked.parent.mkdir()
    linked.symlink_to(copy)
    env = kit.environment(home)
    del env["DTA_TOOLKIT_IMAGE"]
    for launcher in (copy, linked):
        done = kit.launch(["--help"], cwd=workdir, env=env, launcher=launcher)
        assert done.returncode == kit.EX_UNAVAILABLE
        line = kit.refusal(done)
        assert f"the image dta-toolkit:{version} is not present" in line
        assert f"no docker/toolkit.Dockerfile in {(tmp_path / 'plugin').resolve()}" in line
        assert "set DTA_TOOLKIT_IMAGE" in line
    assert not kit.image_present(f"dta-toolkit:{version}")
    assert not (home / ".local" / "state" / "dta").exists(), "nothing was to be built"


@kit.needs_image
def test_without_sources_an_absent_runtime_image_is_left_to_the_toolkit(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    version = f"0.0.0-{kit.TWIN_PREFIX}{secrets.token_hex(4)}"
    launcher = kit.plugin_copy(tmp_path / "plugin", version, sources=False)
    toolkit = f"dta-toolkit:{version}"
    tagged = subprocess.run(["docker", "image", "tag", kit.IMAGE, toolkit], capture_output=True)
    assert tagged.returncode == 0, tagged.stderr
    try:
        env = kit.environment(home)
        del env["DTA_TOOLKIT_IMAGE"]
        done = kit.launch(["--help"], cwd=workdir, env=env, launcher=launcher)
        assert done.returncode == 0, done.stderr
        assert done.stderr == ""
        assert not kit.image_present(f"dta-runtime:{version}")
    finally:
        kit.remove_tags(toolkit)


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ({"DTA_REBUILD": "yes"}, "DTA_REBUILD is 'yes'"),
        ({"DTA_REBUILD": "1"}, "unset one of the two"),
    ],
)
@needs_docker
def test_refused_for_a_rebuild_that_cannot_be(
    workdir: Path, home: Path, extra: dict[str, str], said: str
) -> None:
    done = kit.launch(["--help"], cwd=workdir, env=kit.environment(home, **extra))
    assert done.returncode == kit.EX_USAGE
    assert said in kit.refusal(done)


@needs_docker
def test_refused_without_permission_on_the_daemon_socket(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    path = tmp_path / "locked.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as locked:
        locked.bind(str(path))
        locked.listen(1)
        path.chmod(0)  # a socket of root's group that this user is not in, as docker's often is
        env = kit.environment(home, DOCKER_HOST=f"unix://{path}")
        done = kit.launch(["--help"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_UNAVAILABLE
    line = kit.refusal(done)
    assert f"no permission on its socket {path}" in line
    assert "usermod -aG docker" in line and "run the command again" in line


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
    line = kit.refusal(done)
    assert "the docker daemon is not reachable: no socket" in line and "start Docker" in line


@needs_docker
def test_refused_when_the_daemon_does_not_answer(tmp_path: Path, workdir: Path, home: Path) -> None:
    path = tmp_path / "silent.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.bind(str(path))  # bound, never listening: every connection is refused
        env = kit.environment(home, DOCKER_HOST=f"unix://{path}")
        done = kit.launch(["--help"], cwd=workdir, env=env)
    assert done.returncode == kit.EX_UNAVAILABLE
    line = kit.refusal(done)
    assert "the docker daemon is not reachable" in line and "start Docker" in line


def test_skill_directory_is_not_created_when_refused(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    env = kit.environment(home, DOCKER_HOST="tcp://127.0.0.1:2375")
    target = tmp_path / "skills"
    for args in (["install-skill"], ["install-skill", "--target", str(target)]):
        done = kit.launch(args, cwd=workdir, env=env)
        assert done.returncode in (kit.EX_USAGE, kit.EX_UNAVAILABLE)
    assert not (home / ".claude").exists() and not target.exists()
