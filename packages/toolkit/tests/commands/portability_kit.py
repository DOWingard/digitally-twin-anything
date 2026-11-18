"""What the export and import tests share: twins of their own, a runner for the
commands, reads of the store and the removal of everything they made.

The twins are the command tests' small plant (`m3tk_kit`), under ids starting
`m8exp-`, with host ports in 13372-13379 and the runtime image
`dta-runtime:main`, so their stacks never meet another test's. The limits are
fixed here, before anything is measured.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import m3tk_kit as kit
import pytest
import yaml
from dta_core.log import bind_trace, configure, new_trace_id
from dta_core.manifest import parse_manifest
from dta_toolkit import bundle as bundles
from dta_toolkit.acceptance import TwinApi
from dta_toolkit.cli import app as dta_app
from dta_toolkit.commands import portability
from dta_toolkit.envfile import read_env
from dta_toolkit.output import Session
from dta_toolkit.stack import Stack
from dta_toolkit.workspace import State, Workspace
from typer.testing import CliRunner, Result

RUNTIME_IMAGE = "dta-runtime:main"
ID_PREFIX = "m8exp-"
PROJECT_PREFIX = f"dta-{ID_PREFIX}"
PORTS = range(13372, 13380)
# Export and import of the small static twin, each.
EXPORT_LIMIT_S = 60.0
IMPORT_LIMIT_S = 60.0
# The commands the tests send to the export and import app; everything else goes to `dta`.
OWN_COMMANDS = frozenset({"export", "import"})
GENERATED = ("DTA_STORE_PASSWORD", "DTA_API_TOKEN")
# Values shorter than this are not searched for: a port number occurs by chance in data.
SEARCHED_MIN = 6


def _image_present(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "image", "inspect", name], capture_output=True, check=False)
    return done.returncode == 0


needs_stack = pytest.mark.skipif(
    not _image_present(RUNTIME_IMAGE),
    reason=f"needs docker and the image {RUNTIME_IMAGE}, built from docker/runtime.Dockerfile",
)


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


def marker(name: str) -> str:
    return f"m8exp-marker-{name.lower().replace('_', '-')}-{secrets.token_hex(6)}"


class Dta:
    """Runs `dta` against one workspace: export and import through their own app."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runner = CliRunner()

    def run(self, *args: str, json_output: bool = True) -> Result:
        command = next((arg for arg in args if not arg.startswith("-")), None)
        if command in OWN_COMMANDS:
            workspace = Workspace(self.root)
            configure(log_file=workspace.log_path if workspace.exists() else None)
            session = Session(
                workspace=workspace, json_output=json_output, trace_id=bind_trace(new_trace_id())
            )
            return self.runner.invoke(portability.app, list(args), obj=session)
        flags = ["--json"] if json_output else []
        return self.runner.invoke(dta_app, ["--workspace", str(self.root), *flags, *args])

    def json(self, *args: str, input: str | None = None) -> tuple[int, dict[str, Any]]:
        """Run with `--json`; stdout must be exactly one JSON document."""
        result = self.run(*args)
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


class PortTwin(kit.KitTwin):
    """The kit's twin under an id, ports and image of these tests.

    With `marked`, every value of its `.env` that is not a number is a marker
    that occurs nowhere else: the store password, the API token, the API's
    address and the folder of the source's files.
    """

    def __init__(
        self,
        base: Path,
        *,
        live: bool = False,
        readings_end: datetime | None = None,
        marked: bool = False,
    ) -> None:
        super().__init__(Dta(base / "ws"), base, live=live, readings_end=readings_end)
        self.base = base
        self.id = f"{ID_PREFIX}{secrets.token_hex(4)}"
        self.project = f"dta-{self.id}"
        self.marked = marked
        if marked:
            folder = base / f"data-{marker('folder')}"
            shutil.move(self.data, folder)
            self.data = folder

    def write(self) -> None:
        super().write()
        data = self.dta.read_manifest()
        data["runtime"]["image"] = RUNTIME_IMAGE
        self.dta.write_manifest(data)

    def fill_env(self) -> int:
        return fill_env(self.env_file, self.data, marked=self.marked)

    def at(self, root: Path) -> PortTwin:
        """The same twin, worked on from another workspace folder."""
        moved = object.__new__(PortTwin)
        moved.__dict__.update(self.__dict__)
        moved.dta = Dta(root)
        moved.root = root
        return moved

    def teardown(self) -> None:
        remove_project(self.project)
        left = leftovers(self.project)
        assert not any(left.values()), left


def fill_env(env_file: Path, data: Path, *, marked: bool = False, port: int | None = None) -> int:
    """Set the names `.env` leaves empty: the port, the API address and the data folder.

    With `marked`, the generated store password and API token are replaced by markers.
    """
    api_port = port or free_port()
    values = {
        "DTA_API_PORT": str(api_port),
        kit.DATA_ENV: str(data),
        "DTA_BASE_URL": f"http://127.0.0.1:{api_port}",
        "DTA_UI_PORT": str(free_port({api_port})),
    }
    replaced = {name: marker(name) for name in GENERATED} if marked else {}
    lines = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name in replaced:
            line = f"{name}={replaced[name]}"
        elif name in values and not value:
            line = f"{name}={values[name]}"
        lines.append(line)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return api_port


def env_values(env_file: Path) -> dict[str, str]:
    return {name: value for name, value in read_env(env_file).items() if value}


def api(root: Path) -> TwinApi:
    """A client of the twin's API, as the toolkit makes one."""
    configure()
    manifest = parse_manifest(yaml.safe_load((root / "manifest.yaml").read_text("utf-8")))
    return TwinApi(manifest, read_env(root / ".env"))


# -- what the process opens --------------------------------------------------------------

_opened: list[list[str]] = []
_hooked: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _opened and event == "open" and isinstance(args[0], str | bytes | Path):
        _opened[-1].append(
            str(args[0], "utf-8", "replace") if isinstance(args[0], bytes) else str(args[0])
        )


@contextlib.contextmanager
def opened_paths() -> Iterator[list[str]]:
    """Every path this process opens inside the block, as the interpreter audits it."""
    if not _hooked:
        # An audit hook cannot be removed again; it records only while a block is open.
        sys.addaudithook(_audit)
        _hooked.append(True)
    paths: list[str] = []
    _opened.append(paths)
    try:
        yield paths
    finally:
        _opened.remove(paths)


# -- docker ---------------------------------------------------------------------------


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _ids(*args: str) -> list[str]:
    return [line for line in _docker(*args).stdout.split() if line]


def leftovers(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": _ids("ps", "-aq", "--filter", label),
        "volumes": _ids("volume", "ls", "-q", "--filter", label),
        "networks": _ids("network", "ls", "-q", "--filter", label),
    }


def remove_project(project: str) -> None:
    """Stop and remove every container, volume and network of one of these stacks."""
    assert project.startswith(PROJECT_PREFIX), project
    if shutil.which("docker") is None:
        return
    left = leftovers(project)
    if left["containers"]:
        _docker("stop", *left["containers"])
        _docker("rm", *left["containers"])
    if left["volumes"]:
        _docker("volume", "rm", *left["volumes"])
    if left["networks"]:
        _docker("network", "rm", *left["networks"])


# -- the store and the artifacts, read from inside the stack ---------------------------


def stack_of(root: Path) -> Stack:
    configure()
    workspace = Workspace(root)
    return Stack(workspace, workspace.load_manifest())


def sql(root: Path, statement: str) -> list[dict[str, Any]]:
    """Rows of the last statement, run as root in the twin's database."""
    stack = stack_of(root)
    results = portability.sql(stack, statement, stack.plan.database)
    return results[-1] if results else []


def branches(root: Path) -> dict[str, str]:
    return portability.heads(stack_of(root), stack_of(root).plan.database)


def users(root: Path) -> list[tuple[str, str, str]]:
    """Every user of the store's server with its grants."""
    found = []
    for row in sql(root, "SELECT `user`, `host` FROM mysql.user ORDER BY `user`, `host`"):
        grants = sql(root, f"SHOW GRANTS FOR '{row['user']}'@'{row['host']}'")
        found.append((row["user"], row["host"], json.dumps(grants, sort_keys=True)))
    return found


def tables_text(root: Path) -> str:
    """Every table of the twin's database at the live line's head, and its log, as text."""
    names = [next(iter(row.values())) for row in sql(root, "SHOW TABLES")]
    parts = []
    for name in names:
        parts.append(json.dumps(sql(root, f"SELECT * FROM `{name}`"), sort_keys=True))
    parts.append(json.dumps(sql(root, "SELECT * FROM dolt_log"), sort_keys=True))
    return "\n".join(parts)


def artifacts(root: Path) -> str:
    """Checksum of each file in the artifact volume and every owner there, read in the API."""
    done = stack_of(root).compose(
        "exec",
        "-T",
        "api",
        "sh",
        "-c",
        f"cd {portability.ARTIFACTS_PATH} && find . -type f -exec sha256sum {{}} + | sort && "
        "find . -exec stat -c 'owner %u:%g' {} + | sort -u",
    )
    return done.stdout


def leftover_backups(root: Path) -> list[str]:
    """Directories of export or import left in the store's container."""
    done = stack_of(root).compose("exec", "-T", "store", "ls", "/tmp")
    return [name for name in done.stdout.split() if name.startswith("dta-")]


# -- bundles ---------------------------------------------------------------------------


def members(bundle: Path) -> dict[str, bytes]:
    """Every regular file of a bundle, by its name in the archive."""
    found = {}
    with tarfile.open(bundle) as archive:
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                found[member.name] = handle.read()
    return found


def descriptor(bundle: Path) -> dict[str, Any]:
    return json.loads(members(bundle)[bundles.DESCRIPTOR])


def rewrite(bundle: Path, target: Path, change: Callable[[str, bytes], bytes]) -> Path:
    """A copy of a bundle with each regular member's bytes passed through `change`."""
    with tarfile.open(bundle) as source, tarfile.open(target, "w") as copy:
        for member in source.getmembers():
            if member.isfile():
                handle = source.extractfile(member)
                assert handle is not None
                data = change(member.name, handle.read())
                member.size = len(data)
                copy.addfile(member, _Bytes(data))
            else:
                copy.addfile(member)
    return target


def with_descriptor(bundle: Path, target: Path, change: Callable[[dict], None]) -> Path:
    """A copy of a bundle whose bundle.json was changed by `change`; members untouched."""

    def edit(name: str, data: bytes) -> bytes:
        if name != bundles.DESCRIPTOR:
            return data
        document = json.loads(data)
        change(document)
        return json.dumps(document, indent=2).encode("utf-8")

    return rewrite(bundle, target, edit)


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self.data) if size < 0 else self.offset + size
        chunk = self.data[self.offset : end]
        self.offset += len(chunk)
        return chunk


def synthetic_bundle(base: Path, twin_id: str | None = None) -> Path:
    """A well-formed bundle of the kit's twin, made without a stack: for the refusals."""
    twin_id = twin_id or f"{ID_PREFIX}{secrets.token_hex(4)}"
    staging = base / "staging"
    workspace = staging / bundles.WORKSPACE
    (workspace / ".dta").mkdir(parents=True)
    (workspace / "ontology").mkdir()
    manifest = kit.manifest(twin_id)
    manifest["runtime"]["image"] = RUNTIME_IMAGE
    (workspace / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), "utf-8")
    (workspace / "ontology" / "plant.yaml").write_text(kit.ONTOLOGY, "utf-8")
    state = State(created_at=datetime(2026, 11, 1, tzinfo=UTC))
    (workspace / ".dta" / "state.json").write_text(state.model_dump_json(indent=2), "utf-8")
    (staging / bundles.STORE).mkdir()
    (staging / bundles.STORE / "manifest").write_bytes(b"not a real backup")
    members_, directories = bundles.scan(staging)
    head = bundles.Head(branch="main", id="0" * 32, seq=0, t=datetime(2026, 1, 1, tzinfo=UTC))
    document = bundles.Descriptor(
        twin=bundles.TwinRef(id=twin_id, name="Plant levels (test)", tier="static"),
        manifest_hash="0" * 64,
        contract_hash="0" * 64,
        database="twin_" + twin_id.replace("-", "_"),
        head=head,
        branches={"main": head.id},
        exported_at=datetime.now(UTC),
        members=members_,
        directories=directories,
    )
    target = base / bundles.bundle_name(twin_id, head)
    bundles.write(staging, document, target)
    shutil.rmtree(staging)
    return target


@pytest.fixture
def make_port_twin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Creates twins of these tests; each one's stack is removed with its data afterwards."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    made: list[PortTwin] = []

    def make(
        *, live: bool = False, readings_end: datetime | None = None, marked: bool = False
    ) -> PortTwin:
        base = tmp_path / f"twin{len(made)}"
        base.mkdir()
        twin = PortTwin(base, live=live, readings_end=readings_end, marked=marked)
        made.append(twin)
        return twin

    try:
        yield make
    finally:
        problems = []
        for twin in made:
            try:
                twin.teardown()
            except AssertionError as exc:
                problems.append(f"{twin.project}: {exc}")
        assert not problems, problems
