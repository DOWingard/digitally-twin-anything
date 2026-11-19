"""Running the plugin's launcher, and the toolkit as `uv run dta` runs it, from the tests.

Every test runs the launcher with a small environment of its own: HOME is a
temporary directory, so nothing is ever written to the real `~/.claude`, and
the image is the one under test. The twins these tests build have ids that
start with `m7img-`, compose projects that start with `dta-m7img-` and host
ports in 13341-13349 on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import pty
import secrets
import select
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from dta_core.log import Log

log = Log("tests.launcher")

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "bin" / "dta"
VERSION_FILE = LAUNCHER.with_name("VERSION")
SAMPLE = Path(__file__).with_name("sample")
# What `uv run dta` runs: the entry point installed in this environment.
NATIVE = Path(sys.executable).with_name("dta")

IMAGE = os.environ.get("DTA_TEST_TOOLKIT_IMAGE") or "dta-toolkit:main"
RUNTIME_IMAGE = os.environ.get("DTA_TEST_RUNTIME_IMAGE") or "dta-runtime:main"

EX_USAGE = 64
EX_UNAVAILABLE = 69
TWIN_PREFIX = "m7img-"
PROJECT_PREFIX = "dta-m7img-"
PORTS = range(13341, 13350)
SECRET_MIN = 6  # shorter values (ports) are not masked by the toolkit either
COMMAND_TIMEOUT_S = 600.0

# The developer's own docker client configuration, read before any test moves HOME, so
# that the launcher's docker calls reach the same daemon as the developer's.
_DOCKER_CONFIG = os.environ.get("DOCKER_CONFIG") or str(Path.home() / ".docker")


def _image_present(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", name],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


needs_image = pytest.mark.skipif(
    not _image_present(IMAGE),
    reason=f"needs docker and the image {IMAGE} (DTA_TEST_TOOLKIT_IMAGE), built from "
    "docker/toolkit.Dockerfile",
)
needs_runtime = pytest.mark.skipif(
    not _image_present(RUNTIME_IMAGE),
    reason=f"needs the image {RUNTIME_IMAGE} (DTA_TEST_RUNTIME_IMAGE), built from "
    "docker/runtime.Dockerfile",
)


def _buildx_present() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "buildx", "version"], capture_output=True).returncode == 0


needs_buildx = pytest.mark.skipif(
    not _buildx_present(), reason="needs docker with its buildx plugin, which builds the images"
)

# What the launcher runs besides docker and the shell's own builtins; flock is
# used when present and is left out here.
LAUNCHER_TOOLS = (
    *("env", "sed", "ls", "id", "readlink", "mkdir"),
    *("tee", "tail", "date", "sleep", "rm", "rmdir"),
)

# The tags the rest of this machine's work depends on: no test builds or removes them.
PROTECTED_TAGS = frozenset(
    f"{repository}:{tag}"
    for repository in ("dta-toolkit", "dta-runtime")
    for tag in ("0.1.0", "main")
)
# What the launcher's build reads from the plugin's root; the tests are left out.
_SOURCES = ("pyproject.toml", "uv.lock", "docker")
_NOT_SOURCES = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".venv", ".*_cache")


def image_present(name: str) -> bool:
    return _image_present(name)


def plugin_copy(root: Path, version: str, *, sources: bool = True) -> Path:
    """A copy of the plugin whose VERSION is `version`, with the sources of its images
    beside the launcher unless told otherwise; returns the copy's launcher."""
    assert version not in {tag.split(":")[1] for tag in PROTECTED_TAGS}, version
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "dta"
    shutil.copy2(LAUNCHER, launcher)
    (root / "bin" / "VERSION").write_text(version + "\n", encoding="utf-8")
    if sources:
        for name in _SOURCES:
            source = REPO / name
            if source.is_dir():
                shutil.copytree(source, root / name)
            else:
                shutil.copy2(source, root / name)
        shutil.copytree(REPO / "packages", root / "packages", ignore=_NOT_SOURCES)
    return launcher


def remove_tags(*names: str) -> None:
    """Remove these images by repository:tag, never by id and never forced: an image
    that holds other tags only loses this one."""
    for name in names:
        assert name not in PROTECTED_TAGS, name
        done = _docker("image", "rm", name)
        if done.returncode != 0 and "No such image" not in done.stderr:
            log.warn("a test image could not be removed", image=name, stderr=done.stderr[-500:])


def environment(home: Path, **extra: str) -> dict[str, str]:
    """The launcher's whole environment: PATH, a temporary HOME, the image under test."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "DOCKER_CONFIG": _DOCKER_CONFIG,
        "DTA_TOOLKIT_IMAGE": IMAGE,
    }
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(extra)
    return env


def launch(
    args: Iterable[str],
    *,
    cwd: Path,
    env: dict[str, str],
    launcher: Path = LAUNCHER,
    shell: tuple[str, ...] = (),
    timeout: float = COMMAND_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run the launcher to its end, stdin closed to input; by `shell` when one is given."""
    command = [*shell, str(launcher), *args]
    started = time.perf_counter()
    done = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    log.debug(
        "launcher finished",
        args=command[1:3],
        exit_code=done.returncode,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return done


def native(
    args: Iterable[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the toolkit as `uv run dta` does, in this environment's interpreter."""
    native_env = {name: value for name, value in env.items() if name != "DTA_TOOLKIT_IMAGE"}
    return subprocess.run(
        [str(NATIVE), *args],
        cwd=cwd,
        env=native_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def on_terminal(
    args: Iterable[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_too: bool = True,
    reply: tuple[bytes, bytes] | None = None,
    timeout: float = COMMAND_TIMEOUT_S,
) -> tuple[int, bytes]:
    """Run the launcher with a terminal as stdin, and as stdout and stderr when asked.

    With `reply` (prompt, answer) the answer is typed once the prompt was shown.
    Returns the exit code and everything written to the terminal and the pipes.
    """
    main, attached = pty.openpty()
    try:
        process = subprocess.Popen(
            [str(LAUNCHER), *args],
            cwd=cwd,
            env=env,
            stdin=attached,
            stdout=attached if stdout_too else subprocess.PIPE,
            stderr=attached if stdout_too else subprocess.PIPE,
            start_new_session=True,
        )
    finally:
        os.close(attached)
    seen = b""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([main], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(main, 65536)
            except OSError:  # the terminal's other end closed: the launcher is done
                break
            if not chunk:
                break
            seen += chunk
            if reply is not None and reply[0] in seen:
                os.write(main, reply[1])
                reply = None
        else:
            process.kill()
            raise AssertionError(f"the launcher did not end within {timeout:g} s: {seen[-500:]!r}")
        piped, _ = process.communicate(timeout=timeout)
    finally:
        os.close(main)
    return process.returncode, seen + (piped or b"")


def refusal(done: subprocess.CompletedProcess[str]) -> str:
    """The one line a refusal printed; nothing may have gone to stdout."""
    lines = done.stderr.splitlines()
    assert done.stdout == "", done.stdout
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("dta: "), lines[0]
    return lines[0]


def owned_by_caller(paths: Iterable[Path]) -> list[str]:
    """Paths not owned by this process's uid and gid."""
    uid, gid = os.getuid(), os.getgid()
    return [str(p) for p in paths if (p.lstat().st_uid, p.lstat().st_gid) != (uid, gid)]


def free_port(taken: set[int] | None = None) -> int:
    """A port of the assigned range that nothing listens on now."""
    for port in secrets.SystemRandom().sample(list(PORTS), len(PORTS)):
        if taken and port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


# -- the launcher's container and processes ------------------------------------------


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def toolkit_container(workdir: Path) -> dict[str, Any] | None:
    """`docker inspect` of the running container of the image under test working in `workdir`."""
    listed = _docker("ps", "--quiet", "--no-trunc", "--filter", f"ancestor={IMAGE}")
    for container in listed.stdout.split():
        found = _docker("inspect", container)
        if found.returncode != 0:
            continue  # it ended in between
        document = json.loads(found.stdout)[0]
        if document["Config"]["WorkingDir"] == str(workdir):
            return document
    return None


def wait_for_container(workdir: Path, process: subprocess.Popen, limit_s: float = 60.0):
    """The launcher's container once it runs; fails when the launcher ends first."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        document = toolkit_container(workdir)
        if document is not None:
            return document
        assert process.poll() is None, "the launcher ended before its container was seen"
        time.sleep(0.1)
    raise AssertionError(f"no container of {IMAGE} working in {workdir} within {limit_s:g} s")


def command_line(pid: int) -> list[str]:
    """The arguments of a process of this machine, as the process list shows them."""
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def process_list() -> str:
    """Every process of this machine with its full command line."""
    return subprocess.run(
        ["ps", "-eo", "pid,args"], capture_output=True, text=True, check=True
    ).stdout


def container_processes(container: str) -> str:
    """Every process in a container with its full command line, as the host sees it."""
    return _docker("top", container, "-eo", "pid,args").stdout


def env_values(env_file: Path) -> list[str]:
    """Values of an environment file long enough that they must never be shown."""
    values = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1]
        if len(value) >= SECRET_MIN:
            values.append(value)
    return values


# -- stacks ------------------------------------------------------------------------------


def leftovers(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": _docker("ps", "-aq", "--filter", label).stdout.split(),
        "volumes": _docker("volume", "ls", "-q", "--filter", label).stdout.split(),
        "networks": _docker("network", "ls", "-q", "--filter", label).stdout.split(),
    }


def remove_project(project: str) -> None:
    """Take one of these tests' stacks down with its volumes, by its project name."""
    assert project.startswith(PROJECT_PREFIX), project
    done = _docker("compose", "--project-name", project, "down", "--volumes", "--remove-orphans")
    if done.returncode != 0:
        log.warn("compose down by project name failed", project=project, stderr=done.stderr[-500:])
