"""The toolkit run through the launcher behaves as `uv run dta` does, on real containers."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import filelock
import launcher_kit as kit
import pytest

pytestmark = kit.needs_image

# Fixed before measuring: `dta --help` through the launcher, median of ten on a warm cache.
HELP_MEDIAN_LIMIT_S = 2.0
HELP_RUNS = 10
# How long a command waits for the workspace lock before giving up (the toolkit's own value).
TOOLKIT_LOCK_WAIT_S = 30.0


def test_help_equals_the_toolkits_own(workdir: Path, env: dict[str, str]) -> None:
    through = kit.launch(["--help"], cwd=workdir, env=env)
    own = kit.native(["--help"], cwd=workdir, env=env)
    assert through.returncode == own.returncode == 0, through.stderr
    assert through.stdout == own.stdout
    assert through.stderr == own.stderr == ""


def test_version_option_equals_the_toolkits_own(workdir: Path, env: dict[str, str]) -> None:
    # The command line has no --version: both refuse it the same way.
    through = kit.launch(["--version"], cwd=workdir, env=env)
    own = kit.native(["--version"], cwd=workdir, env=env)
    assert through.returncode == own.returncode == 2
    assert (through.stdout, through.stderr) == (own.stdout, own.stderr)
    assert "--version" in own.stderr


def test_exit_code_and_output_of_a_failing_command(workdir: Path, env: dict[str, str]) -> None:
    through = kit.launch(["--json", "status"], cwd=workdir, env=env)
    own = kit.native(["--json", "status"], cwd=workdir, env=env)
    assert through.returncode == own.returncode == 2
    found, expected = json.loads(through.stdout), json.loads(own.stdout)
    assert found["ok"] is expected["ok"] is False
    assert found["error"] == expected["error"]
    assert found["error"]["code"] == "workspace.missing"


def test_files_the_toolkit_writes_belong_to_the_caller(workdir: Path, env: dict[str, str]) -> None:
    done = kit.launch(["--json", "init", "--id", f"{kit.TWIN_PREFIX}owner"], cwd=workdir, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["result"]["root"] == str(workdir)
    written = sorted(workdir.rglob("*"))
    assert workdir / ".dta" / "state.json" in written and workdir / "manifest.yaml" in written
    assert kit.owned_by_caller(written) == []


def test_dta_variables_reach_the_toolkit_and_no_other_does(
    workdir: Path, home: Path, env: dict[str, str]
) -> None:
    # Width variables change the help's layout when the toolkit sees them.
    wide = {"COLUMNS": "150", "TERMINAL_WIDTH": "150"}
    narrow = kit.native(["--help"], cwd=workdir, env=env).stdout
    assert kit.native(["--help"], cwd=workdir, env={**env, **wide}).stdout != narrow
    through = kit.launch(["--help"], cwd=workdir, env={**env, **wide})
    assert through.returncode == 0 and through.stdout == narrow

    debug = kit.launch(["--json", "status"], cwd=workdir, env={**env, "DTA_LOG_LEVEL": "debug"})
    assert "debug" in _levels(debug.stderr), debug.stderr
    quiet = kit.launch(["--json", "status"], cwd=workdir, env=env)
    assert _levels(quiet.stderr) and "debug" not in _levels(quiet.stderr)


def _levels(log_lines: str) -> set[str]:
    return {json.loads(line)["level"] for line in log_lines.splitlines() if line.startswith("{")}


def test_dta_home_and_dta_workspace(tmp_path: Path, home: Path) -> None:
    root = (tmp_path / "projects").resolve()
    workspace = root / "twin" / "ws"
    workspace.mkdir(parents=True)
    env = kit.environment(home, DTA_HOME=str(root), DTA_WORKSPACE=str(workspace))
    # From a directory above the workspace, which DTA_HOME mounts with it.
    done = kit.launch(
        ["--json", "init", "--id", f"{kit.TWIN_PREFIX}home"], cwd=root / "twin", env=env
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["result"]["root"] == str(workspace)
    assert (workspace / ".dta" / "state.json").is_file()


def test_dta_mounts_are_mounted_at_their_own_path(
    tmp_path: Path, workdir: Path, home: Path
) -> None:
    other = (tmp_path / "other").resolve()
    workspace = other / "ws"
    workspace.mkdir(parents=True)
    # Empty entries are skipped; the working directory is mounted once.
    env = kit.environment(home, DTA_MOUNTS=f"{other}::{workdir}")
    done = kit.launch(
        ["--json", "-w", str(workspace), "init", "--id", f"{kit.TWIN_PREFIX}mounts"],
        cwd=workdir,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert (workspace / ".dta" / "state.json").is_file()
    assert kit.owned_by_caller(sorted(workspace.rglob("*"))) == []


def test_runs_with_stdin_closed(workdir: Path, env: dict[str, str]) -> None:
    done = subprocess.run(
        [str(kit.LAUNCHER), "--help"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=kit.COMMAND_TIMEOUT_S,
        check=False,
        preexec_fn=lambda: os.close(0),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == kit.native(["--help"], cwd=workdir, env=env).stdout


def test_a_terminal_only_when_stdin_and_stdout_are_terminals(
    workdir: Path, env: dict[str, str]
) -> None:
    # The toolkit colours its help only when it writes to a terminal.
    code, shown = kit.on_terminal(["--help"], cwd=workdir, env=env)
    assert code == 0, shown
    assert b"\x1b[" in shown and b"Usage" in shown
    code, piped = kit.on_terminal(["--help"], cwd=workdir, env=env, stdout_too=False)
    assert code == 0, piped
    assert b"\x1b[" not in piped
    assert piped.decode() == kit.native(["--help"], cwd=workdir, env=env).stdout


_SHELLS = {
    "busybox": ("busybox", "sh"),
    "dash": ("dash",),
    "posh": ("posh",),
    "mksh": ("mksh",),
    "yash": ("yash", "--posix"),
}
POSIX_SHELLS = [
    pytest.param(command, id=name) for name, command in _SHELLS.items() if shutil.which(name)
] or [pytest.param((), marks=pytest.mark.skip(reason=f"none of {', '.join(_SHELLS)} installed"))]


@pytest.mark.parametrize("shell", POSIX_SHELLS)
def test_runs_under_another_posix_shell(
    tmp_path: Path, workdir: Path, home: Path, env: dict[str, str], shell: tuple[str, ...]
) -> None:
    done = kit.launch(["--help"], cwd=workdir, env=env, shell=shell)
    assert done.returncode == 0, done.stderr
    assert done.stdout == kit.native(["--help"], cwd=workdir, env=env).stdout

    refused = kit.launch(["status"], cwd=workdir, env={**env, "DTA_HOME": str(home)}, shell=shell)
    assert refused.returncode == kit.EX_USAGE
    assert "is not inside DTA_HOME" in kit.refusal(refused)

    other = (tmp_path / "other, with a comma").resolve()
    workspace = other / "ws"
    workspace.mkdir(parents=True)
    mounted = {**env, "DTA_MOUNTS": str(other), "DTA_LOG_LEVEL": "debug"}
    args = ["--json", "-w", str(workspace), "init", "--id", f"{kit.TWIN_PREFIX}shell"]
    done = kit.launch(args, cwd=workdir, env=mounted, shell=shell)
    assert done.returncode == 0, done.stderr
    assert (workspace / ".dta" / "state.json").is_file()
    assert "debug" in _levels(done.stderr)

    args = ["--json", "-w", str(workspace), "install-skill"]
    done = kit.launch(args, cwd=workdir, env=mounted, shell=shell)
    assert json.loads(done.stdout)["error"]["code"] == "skill.missing"
    assert (home / ".claude" / "skills").is_dir()


def test_help_is_fast(workdir: Path, env: dict[str, str]) -> None:
    assert kit.launch(["--help"], cwd=workdir, env=env).returncode == 0  # warm the cache
    durations = []
    for _ in range(HELP_RUNS):
        started = time.perf_counter()
        done = kit.launch(["--help"], cwd=workdir, env=env)
        durations.append(time.perf_counter() - started)
        assert done.returncode == 0
    median = statistics.median(durations)
    print(f"\ndta --help through the launcher: median {median:.3f} s of {HELP_RUNS}")
    assert median <= HELP_MEDIAN_LIMIT_S, [round(d, 3) for d in durations]


def test_no_secret_reaches_a_command_line_or_the_container(
    workdir: Path, home: Path, env: dict[str, str]
) -> None:
    done = kit.launch(["--json", "init", "--id", f"{kit.TWIN_PREFIX}secret"], cwd=workdir, env=env)
    assert done.returncode == 0, done.stderr
    in_file = {
        "DTA_STORE_PASSWORD": secrets.token_urlsafe(24),
        "DTA_API_TOKEN": secrets.token_urlsafe(24),
        "M7IMG_SOURCE_PASSWORD": secrets.token_urlsafe(24),
    }
    env_file = workdir / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in in_file.items()), encoding="utf-8")
    env_file.chmod(0o600)
    exported = secrets.token_urlsafe(24)
    not_passed = secrets.token_urlsafe(24)
    env = {**env, "DTA_M7IMG_PROBE": exported, "M7IMG_NOT_PASSED": not_passed}

    # While this test holds the workspace's lock, the toolkit's build waits for it: a
    # command that runs long enough to be looked at from outside, and proof that the
    # toolkit in its container and a process on the host share the lock.
    lock = filelock.FileLock(str(workdir / ".dta" / "lock"))
    process: subprocess.Popen[str] | None = None
    try:
        with lock.acquire(timeout=10):
            held_since = time.monotonic()
            process = subprocess.Popen(
                [str(kit.LAUNCHER), "--json", "build"],
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            container = kit.wait_for_container(workdir, process)
            launcher_args = kit.command_line(process.pid)  # docker run: the launcher became it
            texts = [kit.process_list(), kit.container_processes(container["Id"])]
            assert process.poll() is None, "the build did not wait for the lock"
            assert time.monotonic() - held_since < TOOLKIT_LOCK_WAIT_S - 5
    finally:
        # Released: the build goes on by itself and ends; it is waited for either way.
        if process is not None:
            output, errors = process.communicate(timeout=kit.COMMAND_TIMEOUT_S)

    # Once the lock was released the build ran, and refused a workspace not ready for it.
    assert process is not None and process.returncode == 1, errors
    assert json.loads(output)["error"]["code"] == "build.not_ready"

    document = json.dumps(container)
    for value in in_file.values():
        assert value not in document, "a value of .env reached the container's definition"
        assert value not in " ".join(launcher_args)
        assert all(value not in text for text in texts), "a value of .env is on a command line"
    arguments = json.dumps(
        [container["Path"], container["Args"], container["Config"]["Cmd"]]
        + [container["Config"]["Entrypoint"]]
    )
    assert exported not in arguments and exported not in " ".join(launcher_args)
    assert all(exported not in text for text in texts)
    assert container["Args"] == ["--json", "build"]
    # By name on the launcher's command line; by value only in the container's environment.
    assert "DTA_M7IMG_PROBE" in launcher_args
    assert f"DTA_M7IMG_PROBE={exported}" in container["Config"]["Env"]
    assert not any(e.startswith("M7IMG_NOT_PASSED=") for e in container["Config"]["Env"])
    assert not any(not_passed in text for text in texts)

    host = container["HostConfig"]
    assert host["NetworkMode"] == "host" and host["ReadonlyRootfs"] is True
    # Compiled modules of an environment made in /tmp can be loaded.
    assert "exec" in host["Tmpfs"]["/tmp"].split(",")
    assert container["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
    mounts = {m["Destination"]: m["Source"] for m in container["Mounts"]}
    assert mounts[str(workdir)] == str(workdir)
    socket_group = str(os.stat(mounts["/var/run/docker.sock"]).st_gid)
    assert host["GroupAdd"] == [socket_group]
    assert "--interactive" in launcher_args and "--tty" not in launcher_args
    assert "--pull" in launcher_args and launcher_args[launcher_args.index("--pull") + 1] == "never"


@pytest.mark.parametrize("scope", ["personal", "target"])
def test_install_skill_mounts_only_where_the_skill_goes(
    tmp_path: Path, workdir: Path, home: Path, env: dict[str, str], scope: str
) -> None:
    # A workspace with no generated skill: the toolkit refuses, after the launcher mounted.
    assert (
        kit.launch(["init", "--id", f"{kit.TWIN_PREFIX}skill"], cwd=workdir, env=env).returncode
        == 0
    )
    target = tmp_path / "agent" / "skills"
    args = ["--json", "install-skill"] + (["--target", str(target)] if scope == "target" else [])
    done = kit.launch(args, cwd=workdir, env=env)
    assert done.returncode == 1, done.stderr
    assert json.loads(done.stdout)["error"]["code"] == "skill.missing"
    expected = target if scope == "target" else home / ".claude" / "skills"
    assert expected.is_dir() and kit.owned_by_caller([expected]) == []
    other = home / ".claude" if scope == "target" else target
    assert not other.exists()
