"""The launcher builds its two images the first time it needs them, then runs the command.

Every test runs a copy of the plugin whose VERSION is a version of its own, so
the images built are `dta-toolkit:<that version>` and `dta-runtime:<that
version>`; they are removed afterwards by repository and tag. The builds use
this machine's layer cache, and the launcher's own JSON-free lines and docker's
progress go to stderr while stdout carries only the command's document.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import launcher_kit as kit
import pytest

pytestmark = kit.needs_buildx

# A first build with an empty cache takes minutes; with this machine's cache, seconds.
BUILD_TIMEOUT_S = 1800.0
STATUS = ["--json", "status"]
_TAGGED = re.compile(r"naming to docker\.io/library/(\S+)")


@dataclass
class Plugin:
    launcher: Path
    version: str

    @property
    def toolkit(self) -> str:
        return f"dta-toolkit:{self.version}"

    @property
    def runtime(self) -> str:
        return f"dta-runtime:{self.version}"

    def present(self) -> tuple[bool, bool]:
        return kit.image_present(self.toolkit), kit.image_present(self.runtime)


def _version() -> str:
    return f"0.0.0-m9ftest-{secrets.token_hex(4)}"


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = kit.environment(home, **extra)
    del env["DTA_TOOLKIT_IMAGE"]
    return env


def _launch(
    plugin: Plugin, cwd: Path, env: dict[str, str], **options
) -> subprocess.CompletedProcess:
    return kit.launch(
        STATUS, cwd=cwd, env=env, launcher=plugin.launcher, timeout=BUILD_TIMEOUT_S, **options
    )


def _document(done: subprocess.CompletedProcess[str]) -> dict:
    """The one JSON document on stdout: nothing else may be there."""
    assert done.stdout.count("\n") == 1 and done.stdout.endswith("\n"), done.stdout[:2000]
    document = json.loads(done.stdout)
    assert document["error"]["code"] == "workspace.missing", document
    return document


def _notes(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in done.stderr.splitlines() if line.startswith("dta: ")]


def _log(home: Path) -> Path:
    return home / ".local" / "state" / "dta" / "build.log"


def _built(home: Path) -> list[str]:
    """The images the last build that wrote the log tagged."""
    return _TAGGED.findall(_log(home).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plugin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Plugin]:
    version = _version()
    launcher = kit.plugin_copy(tmp_path_factory.mktemp("plugin"), version)
    made = Plugin(launcher, version)
    assert made.present() == (False, False)
    try:
        yield made
    finally:
        kit.remove_tags(made.toolkit, made.runtime)


@pytest.fixture(scope="module")
def first(
    plugin: Plugin, tmp_path_factory: pytest.TempPathFactory
) -> tuple[subprocess.CompletedProcess[str], Path, float]:
    """The first call of the plugin's launcher, with neither image present."""
    home = tmp_path_factory.mktemp("home")
    work = tmp_path_factory.mktemp("work")
    started = time.perf_counter()
    done = _launch(plugin, work, _env(home))
    took = time.perf_counter() - started
    kit.log.info(
        "first call built the images", duration_s=round(took, 1), exit_code=done.returncode
    )
    return done, home, took


def test_absent_images_are_built_then_the_command_runs(
    plugin: Plugin, first: tuple[subprocess.CompletedProcess[str], Path, float]
) -> None:
    done, home, took = first
    assert done.returncode == 2, done.stderr[-3000:]
    _document(done)
    notes = _notes(done)
    assert notes[0].startswith(f"dta: building {plugin.toolkit} and {plugin.runtime} from ")
    assert "the first time takes several minutes" in notes[0] and str(_log(home)) in notes[0]
    assert re.fullmatch(rf"dta: built {plugin.toolkit} and {plugin.runtime} in \d+ s", notes[-1])
    # docker's own progress, in plain form, between the two lines.
    lines = done.stderr.splitlines()
    progress = lines[lines.index(notes[0]) + 1 : lines.index(notes[-1])]
    assert any(line.startswith("#1 [internal] load build definition") for line in progress)
    # The toolkit's own log lines come after the build.
    assert all(line.startswith("{") for line in lines[lines.index(notes[-1]) + 1 :])
    assert plugin.present() == (True, True)
    assert _built(home) == [plugin.toolkit, plugin.runtime]
    print(f"\nfirst call, both images built with this machine's cache: {took:.1f} s")


def test_a_second_call_builds_nothing(
    plugin: Plugin, first: tuple[subprocess.CompletedProcess[str], Path, float], tmp_path: Path
) -> None:
    _, home, _ = first
    log_before = _log(home).read_bytes()
    done = _launch(plugin, tmp_path, _env(home))
    assert done.returncode == 2
    _document(done)
    assert _notes(done) == []
    assert _log(home).read_bytes() == log_before


def test_the_runtime_image_alone_is_built_when_it_is_absent(
    plugin: Plugin, first: tuple[subprocess.CompletedProcess[str], Path, float], tmp_path: Path
) -> None:
    kit.remove_tags(plugin.runtime)
    assert plugin.present() == (True, False)
    home = tmp_path / "home"
    done = _launch(plugin, tmp_path, _env(home))
    assert done.returncode == 2, done.stderr[-3000:]
    _document(done)
    notes = _notes(done)
    assert notes[0].startswith(f"dta: building {plugin.runtime} from "), notes
    assert plugin.present() == (True, True)
    assert _built(home) == [plugin.runtime]


def _at_once(plugin: Plugin, cwd: Path, env: dict[str, str]) -> list[subprocess.CompletedProcess]:
    """Two calls of the launcher started together; both run to their end."""
    processes = [
        subprocess.Popen(
            [str(plugin.launcher), *STATUS],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    finished = []
    for process in processes:
        out, err = process.communicate(timeout=BUILD_TIMEOUT_S)
        finished.append(subprocess.CompletedProcess(process.args, process.returncode, out, err))
    return finished


def _built_once(plugin: Plugin, home: Path, calls: list[subprocess.CompletedProcess]) -> None:
    for done in calls:
        assert done.returncode == 2, done.stderr[-3000:]
        _document(done)
    builders = [done for done in calls if any(n.startswith("dta: building ") for n in _notes(done))]
    assert len(builders) == 1, [_notes(done) for done in calls]
    [waiter] = [done for done in calls if done is not builders[0]]
    notes = _notes(waiter)
    # Started together, the other call finds the lock taken and waits for it.
    assert any(note.startswith("dta: another dta call is building the images") for note in notes)
    assert notes[-1] == "dta: another dta call built the images meanwhile", notes
    assert _built(home) == [plugin.toolkit, plugin.runtime]
    assert plugin.present() == (True, True)


def test_two_calls_at_once_build_once(
    plugin: Plugin, first: tuple[subprocess.CompletedProcess[str], Path, float], tmp_path: Path
) -> None:
    kit.remove_tags(plugin.toolkit, plugin.runtime)
    home = tmp_path / "home"
    _built_once(plugin, home, _at_once(plugin, tmp_path, _env(home)))
    assert (home / ".local" / "state" / "dta" / "build.lock").is_file()


def test_without_flock_a_directory_is_the_lock_and_a_dead_holder_loses_it(
    plugin: Plugin, first: tuple[subprocess.CompletedProcess[str], Path, float], tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in (*kit.LAUNCHER_TOOLS, "docker"):
        found = shutil.which(name)
        assert found is not None, name
        (tools / name).symlink_to(found)
    home = tmp_path / "home"
    # The lock of a call that died while it built.
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = home / ".local" / "state" / "dta" / "build.lock.d"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{dead.pid}\n", encoding="utf-8")
    kit.remove_tags(plugin.toolkit, plugin.runtime)
    calls = _at_once(plugin, tmp_path, _env(home, PATH=str(tools)))
    taken = [n for done in calls for n in _notes(done) if n.startswith("dta: took over")]
    assert taken == [
        f"dta: took over the build's lock of process {dead.pid}, which no longer runs"
    ], [_notes(done) for done in calls]
    _built_once(plugin, home, calls)
    assert not lock.exists() and not (lock.parent / "build.lock").exists()


SHELLS = [pytest.param((), id="sh")]
if shutil.which("busybox"):
    SHELLS.append(pytest.param(("busybox", "sh"), id="busybox"))


@pytest.mark.parametrize("shell", SHELLS)
def test_dta_rebuild_builds_both_again(
    plugin: Plugin,
    first: tuple[subprocess.CompletedProcess[str], Path, float],
    tmp_path: Path,
    shell: tuple[str, ...],
) -> None:
    assert plugin.present() == (True, True)
    home = tmp_path / "home"
    done = _launch(plugin, tmp_path, _env(home, DTA_REBUILD="1"), shell=shell)
    assert done.returncode == 2, done.stderr[-3000:]
    _document(done)
    notes = _notes(done)
    assert notes[0].startswith(
        f"dta: DTA_REBUILD=1: building {plugin.toolkit} and {plugin.runtime} again from "
    ), notes
    assert _built(home) == [plugin.toolkit, plugin.runtime]


def test_a_failing_build_refuses_with_the_end_of_its_log(tmp_path: Path) -> None:
    version = _version()
    broken = Plugin(kit.plugin_copy(tmp_path / "plugin", version), version)
    # Fails at once, pulling nothing: there is no shell in an empty image.
    dockerfile = tmp_path / "plugin" / "docker" / "toolkit.Dockerfile"
    dockerfile.write_text("FROM scratch\nRUN exit 3\n", encoding="utf-8")
    home = tmp_path / "home"
    try:
        done = _launch(broken, tmp_path, _env(home))
    finally:
        kit.remove_tags(broken.toolkit, broken.runtime)
    assert done.returncode == kit.EX_UNAVAILABLE
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert re.fullmatch(
        rf"dta: building {re.escape(broken.toolkit)} failed after \d+ s \(docker's exit status "
        rf"[1-9]\d*\); its whole output is in {re.escape(str(_log(home)))}",
        lines[-1],
    ), lines[-5:]
    tail_start = lines.index(f"dta: the last lines of {_log(home)}:")
    assert 0 < len(lines) - tail_start - 2 <= 20
    assert "ERROR" in _log(home).read_text(encoding="utf-8")
    assert not os.path.exists(home / ".local" / "state" / "dta" / "build.lock.d")
