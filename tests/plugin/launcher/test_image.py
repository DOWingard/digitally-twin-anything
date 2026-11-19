"""What the toolkit image holds, and what it does not."""

from __future__ import annotations

import json
import subprocess

import launcher_kit as kit

pytestmark = kit.needs_image

# Libraries of the store and of the source drivers: the toolkit reaches both only
# through the twin's own containers, so its image has none of them.
NOT_INSTALLED = ("pymysql", "dlt", "psycopg2", "fsspec", "aiohttp", "numpy", "pandas")


def _run(*args: str, entrypoint: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--pull", "never", "--entrypoint", entrypoint, kit.IMAGE, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_runs_as_nobody_in_particular_with_home_and_docker_config_in_tmp() -> None:
    done = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config}}", kit.IMAGE],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(done.stdout)
    assert config["User"] not in ("", "0", "root")
    assert config["Entrypoint"] == ["dta"]
    env = dict(entry.split("=", 1) for entry in config["Env"])
    assert env["HOME"] == "/tmp" and env["DOCKER_CONFIG"] == "/tmp/.docker"
    assert not [name for name in env if "PASSWORD" in name or "TOKEN" in name]


def test_holds_the_docker_client_with_compose_and_buildx() -> None:
    for args in (["--version"], ["compose", "version"], ["buildx", "version"]):
        done = _run(*args, entrypoint="docker")
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip(), args


def test_holds_this_toolkit_and_no_store_or_source_library() -> None:
    script = (
        "import importlib.metadata as m, importlib.util as u, json; "
        f"print(json.dumps([m.version('dta-toolkit'), [n for n in {NOT_INSTALLED!r} "
        "if u.find_spec(n)]]))"
    )
    done = _run("-c", script, entrypoint="python")
    assert done.returncode == 0, done.stderr
    version, installed = json.loads(done.stdout)
    assert version == kit.VERSION_FILE.read_text(encoding="utf-8").strip()
    assert installed == []
