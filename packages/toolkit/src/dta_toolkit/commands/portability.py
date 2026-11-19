"""`dta export` and `dta import`: a twin carried to another machine in one file.

Export works on the running stack and leaves it running. Inside the store's
container the server's own `dolt_backup` writes the twin's database to a fresh
directory, which compose copies out and which is removed again whatever
happens; the artifact volume is copied out of the API's container, the one
that mounts it. The workspace gives the files the twin is made from and its
record of what was agreed, never `.env`: the toolkit does not open that file
during an export. Compose itself reads it, as for every command it runs
against the stack, to fill in the stack file.

The heads a bundle records are the ones its backup holds. They are read
before and after each backup; when a branch moved in between, the backup is
synced again, until one ran with no branch moving. A commit made during an
export is therefore in the bundle whole or not at all, and the bundle names
the heads it holds. Branch heads only move forward, so equal heads before and
after a backup are the heads of the backup.

Import unpacks the workspace and creates `.env` with a new store password and
API token; what only the user can know is left to fill in, and the same
command, run again, starts the store alone, restores the database under the
twin's database name, puts the artifacts into their volume and verifies that
every branch is at the head the bundle names. The store needs nothing a
restore lacks: the image makes its one user from the store password on a new
volume, and everything else a build puts into the store is in the database.
The gates the new machine has to pass itself (access, build, acceptance,
handover) are not carried; the signature is, and stays current while the
contract is unchanged.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log, configure
from dta_core.manifest import Manifest, contract_hash, manifest_hash, parse_manifest
from dta_core.manifest.models import ConnectorKind

from .. import bundle as bundles
from .. import codepack, workerimage
from ..envfile import read_env
from ..output import command, emit, session
from ..runtimecheck import require_image_reads
from ..stack import API_SERVICE, STORE_SERVICE, Stack, StackError, plan_stack
from ..workspace import STAGES, Workspace, now
from .build import BASES_KEY, REUSABLE_STEPS, read_record
from .build import record_path as build_record_path
from .common import timed
from .computeops import code_directory
from .env import extend_env, planned_names, write_example

log = Log("commands.portability")

app = typer.Typer(add_completion=False, no_args_is_help=True)

# Where the stack file mounts the artifact volume, and the name of the store's volume in it.
ARTIFACTS_PATH = "/var/lib/dta/artifacts"
STORE_VOLUME = "store-data"
# Fresh directories in the store's container for one backup or one restore.
EXPORT_DIR = "/tmp/dta-export-"
IMPORT_DIR = "/tmp/dta-import-"
# How often a backup is synced again while branches keep moving during it.
BACKUP_TRIES = 5
# Backups, restores and copies of a large twin take long; nothing here is interactive.
LONG_S = 3600.0
# The store's own names, kept here because the toolkit runs without the store's driver and so
# cannot import the store package; a test holds them equal to the store's.
LIVE_BRANCH = "main"
DATABASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
IMPORT_RECORD = "import.json"
# The gates the new machine has to pass itself.
NOT_CARRIED = frozenset({STAGES[2], STAGES[6], STAGES[7], STAGES[8]})
# What the build record says about the store's data, which the backup carries with it.
SETTLED = "head_after_build"
_COMMIT = re.compile(r"^[0-9a-v]{32}$")
_FILE_SCHEME = "file://"
_DOCKER_ENV = (
    "PATH",
    "HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
# The store's own command line client, inside the store's container. The password
# goes by the environment, as in the store's health check: an argument would show in
# the host's process list.
_DOLT = (
    'DOLT_CLI_PASSWORD="$DOLT_ROOT_PASSWORD" exec dolt --host 127.0.0.1 --port 3306 '
    "--user root --no-tls"
)
_SQL_IN = _DOLT + ' --use-db "$1" sql -r json -q "$2"'
_SQL_SERVER = _DOLT + ' sql -r json -q "$1"'

STORE_NOT_RUNNING = "export.store_not_running"
API_MISSING = "export.api_missing"
NOT_INSTALLED = "export.not_installed"
STORE_BUSY = "export.store_busy"
EXPORT_TARGET = "export.target_invalid"
BUNDLE_NOT_FOUND = "import.bundle_not_found"
TARGET_EXISTS = "import.target_exists"
VOLUME_EXISTS = "import.store_volume_exists"
HEAD_MISMATCH = "import.head_mismatch"
BUNDLE_INCONSISTENT = "import.bundle_inconsistent"

UNPACKING, UNPACKED, RESTORED, VERIFIED, FAILED = (
    "unpacking",
    "unpacked",
    "restored",
    "verified",
    "failed",
)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


# -- the store, through its own client ----------------------------------------------------


def _results(stdout: str) -> list[list[dict[str, Any]]]:
    """The rows of each statement, in order: the client prints one JSON line per statement."""
    found: list[list[dict[str, Any]]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        document = json.loads(line)
        found.append(list(document.get("rows") or []))
    return found


def sql(stack: Stack, statements: str, database: str | None) -> list[list[dict[str, Any]]]:
    """Run statements as the store's root user, in `database` or on the server."""
    if database is None:
        args = ("sh", "-c", _SQL_SERVER, "sh", statements)
    else:
        args = ("sh", "-c", _SQL_IN, "sh", database, statements)
    done = stack.compose("exec", "-T", STORE_SERVICE, *args, timeout=LONG_S)
    try:
        return _results(done.stdout)
    except json.JSONDecodeError as exc:
        log.error("the store's client printed something else than JSON", exc=exc)
        raise StackError(
            "the store's client answered in an unexpected form",
            code="stack.bad_output",
            detail={"statements": statements[:200]},
        ) from exc


def heads(stack: Stack, database: str) -> dict[str, str]:
    """Every branch of the database and the commit it is at."""
    rows = sql(stack, "SELECT `name`, `hash` FROM `dolt_branches` ORDER BY `name`", database)
    return {str(row["name"]): str(row["hash"]) for row in (rows[-1] if rows else [])}


def head_version(stack: Stack, database: str, commit: str) -> bundles.Head:
    """The version a commit of the live line records: its number and its time."""
    if not _COMMIT.match(commit):
        raise StackError(f"{commit!r} is not a commit id", code="stack.bad_output")
    rows = sql(
        stack,
        f"SELECT `commit_hash`, `date`, `message` FROM dolt_log('{commit}') LIMIT 1",
        database,
    )
    row = rows[-1][0] if rows and rows[-1] else None
    try:
        assert row is not None and row["commit_hash"] == commit
        message = json.loads(row["message"])
        seq = int(message["seq"])
        t = datetime.fromisoformat(str(row["date"])).replace(tzinfo=UTC)
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        log.error("the head of the live line is not a version of the twin", commit=commit)
        raise DtaError(
            f"the head of {LIVE_BRANCH} ({commit}) is not a version written by the twin's store",
            code=NOT_INSTALLED,
            detail={"commit": commit},
        ) from exc
    return bundles.Head(branch=LIVE_BRANCH, id=commit, seq=seq, t=t)


def _sync_backup(stack: Stack, database: str, url: str) -> None:
    sql(stack, f"CALL dolt_backup('sync-url', '{url}')", database)


def _remove_in_store(stack: Stack, directory: str) -> None:
    """Remove a directory of ours from the store's container; a failure is only logged."""
    try:
        stack.compose("exec", "-T", STORE_SERVICE, "rm", "-rf", directory)
        log.debug("directory removed from the store's container", directory=directory)
    except StackError as exc:
        log.warn(
            "a directory in the store's container could not be removed; it goes with the container",
            directory=directory,
            code=exc.code,
            detail=exc.detail,
        )


def _moved(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str | None]]:
    return {
        name: [before.get(name), after.get(name)]
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }


def backup(stack: Stack, database: str, destination: Path) -> dict[str, Any]:
    """Back the database up into `destination`; the heads the backup holds.

    The backup is written by the server into a fresh directory of the store's
    container and copied out by compose; the directory is removed in any case.
    """
    directory = f"{EXPORT_DIR}{uuid.uuid4().hex}"
    url = f"{_FILE_SCHEME}{directory}"
    attempts = []
    try:
        before = heads(stack, database)
        for attempt in range(1, BACKUP_TRIES + 1):
            started = time.perf_counter()
            _sync_backup(stack, database, url)
            after = heads(stack, database)
            attempts.append({"attempt": attempt, "duration_ms": _ms(started)})
            moved = _moved(before, after)
            if not moved:
                break
            attempts[-1]["moved"] = moved
            log.warn(
                "a branch moved while the backup was written; it is synced again",
                attempt=attempt,
                tries=BACKUP_TRIES,
                moved=moved,
            )
            before = after
        else:
            log.error("branches kept moving during every backup", tries=BACKUP_TRIES)
            raise DtaError(
                f"the twin's branches moved during each of {BACKUP_TRIES} backups; export again "
                "when fewer commits are being made",
                code=STORE_BUSY,
                detail={"attempts": attempts},
            )
        log.info(
            "store backed up",
            database=database,
            attempts=len(attempts),
            duration_ms=sum(entry["duration_ms"] for entry in attempts),
        )
        if LIVE_BRANCH not in after:
            log.error("the store holds no live line", branches=sorted(after))
            raise DtaError(
                f"the twin's store holds no branch {LIVE_BRANCH}: build the twin before "
                "exporting it",
                code=NOT_INSTALLED,
                detail={"branches": sorted(after)},
            )
        head = head_version(stack, database, after[LIVE_BRANCH])
        started = time.perf_counter()
        with timed(log, "backup copied out of the store's container", directory=directory):
            stack.compose("cp", f"{STORE_SERVICE}:{directory}", str(destination), timeout=LONG_S)
        copy_ms = _ms(started)
    finally:
        _remove_in_store(stack, directory)
    return {"head": head, "branches": after, "attempts": attempts, "copy_ms": copy_ms}


def restore(stack: Stack, database: str, source: Path) -> None:
    """Restore the database from a backup folder, under `database`."""
    directory = f"{IMPORT_DIR}{uuid.uuid4().hex}"
    try:
        with timed(log, "backup copied into the store's container", directory=directory):
            stack.compose("cp", str(source), f"{STORE_SERVICE}:{directory}", timeout=LONG_S)
        with timed(log, "database restored", database=database):
            sql(
                stack,
                f"CALL dolt_backup('restore', '{_FILE_SCHEME}{directory}', '{database}')",
                None,
            )
    finally:
        _remove_in_store(stack, directory)


# -- export -------------------------------------------------------------------------------


class _ExportStack(Stack):
    """The stack as export runs it: nothing of `.env` is read by the toolkit.

    Compose reads the file itself to fill in the stack file and names any
    variable that is missing, so the toolkit's own check of the file is left out,
    and so is masking its values, which nothing of an export prints.
    """

    def require_env(self) -> None:
        return None

    def secret_values(self) -> dict[str, str]:
        return {}


def _state_carried(workspace: Workspace) -> str:
    """The workspace's state without the gates the new machine has to pass itself."""
    state = workspace.load_state()
    kept = {name: gate for name, gate in state.gates.items() if name not in NOT_CARRIED}
    carried = state.model_copy(update={"gates": kept, "installed_skill": None})
    return carried.model_dump_json(indent=2)


def _build_carried(workspace: Workspace) -> str | None:
    """What the build record says of the data in the store, which the backup carries.

    With it a build on the new machine finds the twin where the last build left
    it and does not load its sources again; the other steps run on every build.
    """
    if not build_record_path(workspace).is_file():
        return None
    record = read_record(workspace)
    steps = {name: entry for name, entry in record["steps"].items() if name in REUSABLE_STEPS}
    carried: dict[str, Any] = {"version": record.get("version"), "steps": steps}
    for key in (SETTLED, BASES_KEY):
        if key in record:
            carried[key] = record[key]
    return json.dumps(carried, indent=2, default=str) + "\n"


def _is_env_file(relative: Path) -> bool:
    return relative.name == ".env"


def workspace_files(workspace: Workspace, manifest: Manifest) -> dict[str, Path]:
    """The files of the workspace a bundle carries, by their path in the workspace.

    The manifest, the ontology, the twin's code and its requirements,
    `.env.example`, `discussion.md`, `.gitignore`, the reports, the profiles and
    the signed summary. Never `.env`, a log, `tasks/`, the stack file or the skill.
    """
    root = workspace.root
    found: dict[str, Path] = {}

    def add(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                log.warn("a link in the workspace is not carried", path=str(path))
            return
        relative = path.relative_to(root)
        if _is_env_file(relative):
            log.warn("a file named .env is never carried", path=str(relative))
            return
        found[relative.as_posix()] = path

    def add_tree(directory: Path) -> None:
        if directory.is_dir() and not directory.is_symlink():
            for path in sorted(directory.rglob("*")):
                add(path)

    add(workspace.manifest_path)
    add_tree(workspace.ontology_dir)
    if manifest.ontology and manifest.ontology.path:
        add(root / manifest.ontology.path)
    code = code_directory(workspace, manifest)
    if code is not None:
        files, links, _total = codepack.collect(code)
        for _name, path in files:
            add(path)
        if links:
            log.warn("links in the twin's code are not carried", links=links)
    requirements = workerimage.requirements_path(workspace, manifest)
    if requirements is not None:
        add(requirements)
    for name in (".env.example", "discussion.md", ".gitignore"):
        add(root / name)
    add_tree(root / "reports")
    add_tree(workspace.profiles_dir)
    add(workspace.meta_dir / "summary.md")
    return found


def local_sources(manifest: Manifest) -> list[bundles.Note]:
    """A note for each source that reads a folder of this machine: it does not travel."""
    notes = []
    for source_id, source in sorted((manifest.sources or {}).items()):
        location = source.location or ""
        if source.connector == ConnectorKind.file and location.startswith(_FILE_SCHEME):
            notes.append(
                bundles.Note(
                    code="bundle.local_source",
                    source=source_id,
                    message=f"source {source_id} reads a folder of the exporting machine "
                    f"({location}); the bundle does not carry it: copy the folder to the new "
                    "machine and name it in .env there",
                )
            )
    return notes


def _store_row(stack: Stack) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rows = stack.services()
    return next((row for row in rows if row["service"] == STORE_SERVICE), None), rows


def _versions(stack: Stack, manifest: Manifest, dolt: str | None) -> bundles.Versions:
    try:
        toolkit = importlib.metadata.version("dta-toolkit")
    except importlib.metadata.PackageNotFoundError:
        log.warn("the toolkit's version is not known to the installed packages")
        toolkit = None
    try:
        runtime_id = workerimage.image_id(manifest.runtime.image)
    except DtaError as exc:
        log.warn("the runtime image's id cannot be read", image=manifest.runtime.image, exc=exc)
        runtime_id = None
    return bundles.Versions(
        store={"image": stack.plan.store_image, "dolt": dolt},
        runtime={"image": manifest.runtime.image, "image_id": runtime_id},
        toolkit=toolkit,
    )


def _target(out: Path | None, name: str) -> Path:
    if out is None:
        return Path.cwd() / name
    out = out.expanduser()
    return out / name if out.is_dir() else out


def _staging_parent(out: Path | None) -> Path:
    """A folder on the same file system as the bundle will be, so that it is renamed there."""
    if out is None:
        return Path.cwd()
    out = out.expanduser()
    parent = out if out.is_dir() else out.parent
    if not parent.is_dir():
        log.error("the bundle's folder does not exist", folder=str(parent))
        raise DtaError(
            f"{parent} does not exist; give --out a file in an existing folder",
            code=EXPORT_TARGET,
            detail={"folder": str(parent)},
        )
    return parent


def _export_text(result: dict[str, Any]) -> str:
    head = result["head"]
    lines = [
        f"Exported twin {result['twin_id']} at version {head['seq']} ({head['id'][:8]}) to "
        f"{result['bundle']} ({result['size_bytes'] / 1e6:.1f} MB) in "
        f"{result['duration_ms'] / 1000:.1f} s."
    ]
    lines += [f"warning: {note['message']}" for note in result["warnings"]]
    return "\n".join(lines)


@command
def export(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="File to write, or a folder to write it in; the current folder by default.",
        ),
    ] = None,
) -> None:
    """Write the running twin to one bundle file: workspace, store backup and artifacts.

    The twin keeps running. `.env` is not carried and not read: its secrets are
    made anew where the bundle is imported.
    """
    workspace = session(ctx).workspace
    started = time.perf_counter()
    timings: dict[str, float] = {}
    with workspace.exclusive():
        manifest = workspace.load_manifest()
        stack = _ExportStack(workspace, manifest)
        database = stack.plan.database
        row, rows = _store_row(stack)
        if row is None or row["state"] != "running" or row["health"] != "healthy":
            log.error("the store is not running; nothing exported", store=row)
            raise DtaError(
                "the twin's store is not running; start the twin with dta up and export again",
                code=STORE_NOT_RUNNING,
                detail={"store": row},
            )
        if not any(entry["service"] == API_SERVICE for entry in rows):
            log.error("the API's container does not exist; its artifact volume is out of reach")
            raise DtaError(
                "the twin's API has no container, and the artifact volume is reached through "
                "it; start the twin with dta up and export again",
                code=API_MISSING,
                detail={"services": [entry["service"] for entry in rows]},
            )
        parent = _staging_parent(out)
        log.info("export started", twin_id=stack.plan.twin_id, database=database)
        with tempfile.TemporaryDirectory(prefix=".dta-export-", dir=parent) as temporary:
            staging = Path(temporary)
            step = time.perf_counter()
            carried = workspace_files(workspace, manifest)
            for relative, path in carried.items():
                destination = staging / bundles.WORKSPACE / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            meta = staging / bundles.WORKSPACE / workspace.meta_dir.name
            meta.mkdir(parents=True, exist_ok=True)
            (meta / workspace.state_path.name).write_text(
                _state_carried(workspace), encoding="utf-8"
            )
            record = _build_carried(workspace)
            if record is not None:
                (meta / build_record_path(workspace).name).write_text(record, encoding="utf-8")
            timings["workspace_ms"] = _ms(step)
            log.info("workspace files collected", files=len(carried), **timings)

            step = time.perf_counter()
            dolt = sql(stack, "SELECT dolt_version() AS `version`", None)
            held = backup(stack, database, staging / bundles.STORE)
            timings["backup_ms"] = round(sum(a["duration_ms"] for a in held["attempts"]), 1)
            timings["backup_copy_ms"] = held["copy_ms"]

            step = time.perf_counter()
            with timed(log, "artifacts copied out of the API's container"):
                stack.compose(
                    "cp",
                    f"{API_SERVICE}:{ARTIFACTS_PATH}",
                    str(staging / bundles.ARTIFACTS),
                    timeout=LONG_S,
                )
            timings["artifacts_ms"] = _ms(step)

            step = time.perf_counter()
            members, directories = bundles.scan(staging)
            head: bundles.Head = held["head"]
            warnings = local_sources(manifest)
            for note in warnings:
                log.warn("a source does not travel with the bundle", source=note.source)
            descriptor = bundles.Descriptor(
                twin=bundles.TwinRef(
                    id=stack.plan.twin_id,
                    name=manifest.twin.name if manifest.twin else None,
                    tier=manifest.contract.tier.value
                    if manifest.contract and manifest.contract.tier
                    else None,
                ),
                manifest_hash=manifest_hash(manifest),
                contract_hash=contract_hash(manifest),
                database=database,
                head=head,
                branches=held["branches"],
                versions=_versions(
                    stack, manifest, (dolt[-1][0] if dolt and dolt[-1] else {}).get("version")
                ),
                exported_at=now(),
                backup={"attempts": held["attempts"], "heads_read": "before_and_after"},
                members=members,
                directories=directories,
                warnings=warnings,
            )
            target = _target(out, bundles.bundle_name(stack.plan.twin_id, head))
            size = bundles.write(staging, descriptor, target)
            timings["write_ms"] = _ms(step)
    elapsed = _ms(started)
    log.info(
        "export finished",
        bundle=str(target),
        size_bytes=size,
        seq=head.seq,
        head=head.id,
        duration_ms=elapsed,
        **timings,
    )
    result = {
        "bundle": str(target),
        "size_bytes": size,
        "twin_id": stack.plan.twin_id,
        "head": head.model_dump(mode="json"),
        "branches": held["branches"],
        "warnings": [note.model_dump(mode="json") for note in warnings],
        "backup_attempts": len(held["attempts"]),
        "duration_ms": elapsed,
        "timings_ms": timings,
    }
    emit(ctx, result, _export_text(result))


# -- import -------------------------------------------------------------------------------


def _docker_lines(*args: str) -> list[str]:
    if shutil.which("docker") is None:
        log.error("docker is not installed or not on the PATH")
        raise StackError("the docker command is not available", code="stack.docker_unavailable")
    environment = {name: os.environ[name] for name in _DOCKER_ENV if name in os.environ}
    try:
        done = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=60.0,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("docker could not be asked", exc=exc, args=list(args[:2]))
        raise StackError(
            f"docker could not be asked: {type(exc).__name__}", code="stack.docker_unavailable"
        ) from exc
    if done.returncode != 0:
        log.error("docker command failed", args=list(args[:2]), stderr=done.stderr[-500:])
        raise StackError(
            f"`docker {' '.join(args[:2])}` failed with exit code {done.returncode}",
            code="stack.command_failed",
            detail={"stderr": done.stderr[-500:]},
        )
    return [line for line in done.stdout.split() if line]


def store_volume(project: str) -> str | None:
    """The store volume of a stack of this project name on this machine, if there is one."""
    names = _docker_lines(
        "volume",
        "ls",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.volume={STORE_VOLUME}",
    )
    return names[0] if names else None


def _record_path(workspace: Workspace) -> Path:
    return workspace.meta_dir / IMPORT_RECORD


def _read_import(workspace: Workspace) -> dict[str, Any] | None:
    path = _record_path(workspace)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warn("the record of an import cannot be read", path=str(path), error=type(exc).__name__)
        return None
    return document if isinstance(document, dict) else None


def _write_import(workspace: Workspace, record: dict[str, Any]) -> None:
    with workspace.exclusive():
        workspace.atomic_write(
            _record_path(workspace), json.dumps(record, indent=2, default=str) + "\n"
        )


def _manifest_of(reader: bundles.Reader) -> Manifest:
    name = f"{bundles.WORKSPACE}/manifest.yaml"
    if name not in reader.descriptor.members:
        raise DtaError(
            "the bundle holds no manifest; export the twin again",
            code=BUNDLE_INCONSISTENT,
            detail={"member": name},
        )
    data = yaml.safe_load(reader.read(name).decode("utf-8"))
    return parse_manifest(data, origin=name)


def _collisions(target: Path, reader: bundles.Reader) -> list[str]:
    """Files of the bundle's workspace that the target folder already holds."""
    taken = []
    for name in reader.files(bundles.WORKSPACE):
        relative = name[len(bundles.WORKSPACE) + 1 :]
        if (target / relative).exists() or (target / relative).is_symlink():
            taken.append(relative)
    return taken


def _unpack(workspace: Workspace, reader: bundles.Reader) -> int:
    """Write the bundle's workspace files; the state last, as it marks a workspace."""
    state_name = workspace.state_path.relative_to(workspace.root).as_posix()
    later = None
    written = 0
    with workspace.exclusive():
        for directory in (
            workspace.ontology_dir,
            workspace.profiles_dir,
            workspace.tasks_dir,
            workspace.stack_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for relative, data, mode in reader.iter_files(bundles.WORKSPACE):
            if Path(relative).name == ".env":
                log.warn("a file named .env in the bundle is not unpacked", path=relative)
                continue
            if relative == state_name:
                later = data
                continue
            workspace.atomic_write(
                workspace.root / relative, data, mode=0o755 if mode & 0o100 else 0o644
            )
            written += 1
        if later is None:
            raise DtaError(
                "the bundle holds no workspace state; export the twin again",
                code=BUNDLE_INCONSISTENT,
            )
        workspace.atomic_write(workspace.state_path, later)
    return written + 1


def _to_fill(workspace: Workspace, manifest: Manifest, stack: Stack) -> list[str]:
    values = read_env(workspace.env_path)
    return [entry.name for entry in planned_names(manifest, stack) if not values.get(entry.name)]


def _again(bundle: Path, target: Path, replace_data: bool) -> str:
    flag = " --replace-data" if replace_data else ""
    return f"dta import {bundle} --into {target}{flag}"


def _artifacts_in(stack: Stack, reader: bundles.Reader, staging: Path) -> int:
    """Put the bundle's artifacts into the artifact volume; how many files that was.

    The volume is reached through the API's container, which is created for
    it and not started: the twin is not served before the build on this machine.
    Copied in archive mode, the files belong to the user the twin runs as.
    """
    files = reader.files(bundles.ARTIFACTS)
    folders = [name for name in reader.descriptor.directories if name != bundles.ARTIFACTS]
    if not files and not folders:
        log.info("the bundle holds no artifacts")
        return 0
    source = reader.extract(bundles.ARTIFACTS, staging)
    stack.compose("create", "--no-recreate", API_SERVICE, timeout=LONG_S)
    stack.compose(
        "cp", "--archive", f"{source}/.", f"{API_SERVICE}:{ARTIFACTS_PATH}", timeout=LONG_S
    )
    return len(files)


def _import_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    if result["status"] == UNPACKED:
        lines.append(
            f"Unpacked twin {result['twin_id']} into {result['workspace']}. Fill in "
            f"{result['env_file']}: {', '.join(result['to_fill'])}; then run "
            f"`{result['next']}` to restore its store."
        )
    else:
        head = result["head"]
        lines.append(
            f"Imported twin {result['twin_id']} into {result['workspace']}: every branch is at "
            f"the head the bundle names ({len(result['branches'])} branches; {LIVE_BRANCH} at "
            f"version {head['seq']}, {head['id'][:8]}) in {result['duration_ms'] / 1000:.1f} s."
        )
        if result.get("to_fill"):
            lines.append(f"Still to fill in {result['env_file']}: {', '.join(result['to_fill'])}.")
        lines.append(f"Next: {'; '.join(result['next'])}.")
    lines += [f"warning: {note['message']}" for note in result["warnings"]]
    return "\n".join(lines)


@command
def import_bundle(
    ctx: typer.Context,
    bundle: Annotated[Path, typer.Argument(help="The bundle file `dta export` wrote.")],
    into: Annotated[
        Path | None,
        typer.Option("--into", help="Folder of the new workspace; --workspace by default."),
    ] = None,
    replace_data: Annotated[
        bool,
        typer.Option(
            "--replace-data",
            help="Replace the store of a stack of the same name on this machine: its data is lost.",
        ),
    ] = False,
) -> None:
    """Make a workspace and a store from a bundle; run again once `.env` is filled in.

    The first run unpacks the workspace and writes `.env` with a new store
    password and API token. Once the other variables are set, the same command
    restores the store and checks every branch against the bundle. Then run
    dta source probe, dta build, dta accept and dta install-skill.
    """
    started = time.perf_counter()
    path = bundle.expanduser().resolve()
    if not path.is_file():
        log.error("no bundle at this path", path=str(path))
        raise DtaError(
            f"no bundle at {path}; give the path of the .dtabundle file dta export wrote",
            code=BUNDLE_NOT_FOUND,
            detail={"path": str(path)},
        )
    workspace = Workspace(into.expanduser() if into is not None else session(ctx).workspace.root)
    timings: dict[str, float] = {}
    with bundles.Reader(path) as reader:
        descriptor = reader.descriptor
        verified = reader.verify()
        timings["verify_ms"] = verified.duration_ms
        manifest = _manifest_of(reader)
        plan = plan_stack(manifest)
        if plan.twin_id != descriptor.twin.id or plan.database != descriptor.database:
            log.error(
                "the bundle's manifest names another twin than its description",
                manifest_twin=plan.twin_id,
                described=descriptor.twin.id,
            )
            raise DtaError(
                "the bundle's manifest and its bundle.json name different twins; export the "
                "twin again",
                code=BUNDLE_INCONSISTENT,
                detail={"manifest": plan.twin_id, "bundle": descriptor.twin.id},
            )
        if not DATABASE_NAME.match(descriptor.database):
            raise DtaError(
                "the bundle names a database the store cannot hold",
                code=BUNDLE_INCONSISTENT,
                detail={"database": descriptor.database},
            )
        record = _read_import(workspace)
        resuming = (
            record is not None
            and (record.get("imported_from") or {}).get("digest") == reader.digest
            and record.get("status") != VERIFIED
        )
        target = workspace.root
        if not resuming:
            taken = _collisions(target, reader)
            if workspace.exists() or taken:
                log.error(
                    "the target folder already holds a workspace",
                    target=str(target),
                    files=taken[:10],
                )
                raise DtaError(
                    f"{target} already holds a workspace; import into a new or empty folder "
                    "with --into",
                    code=TARGET_EXISTS,
                    detail={"target": str(target), "files": taken[:20]},
                )
        volume = store_volume(plan.project)
        if volume is not None and not replace_data:
            log.error("a store of this stack exists on this machine", volume=volume)
            raise DtaError(
                f"this machine already has the store of stack {plan.project} (volume {volume}); "
                "remove it with dta down --destroy-data in its workspace, or pass "
                "--replace-data to replace its data",
                code=VOLUME_EXISTS,
                detail={"project": plan.project, "volume": volume},
            )
        warnings = [note.model_dump(mode="json") for note in descriptor.warnings]
        for note in descriptor.warnings:
            log.warn("a source does not travel with the bundle", source=note.source)
        imported_from = {
            "bundle": path.name,
            "path": str(path),
            "digest": reader.digest,
            "twin_id": descriptor.twin.id,
            "exported_at": descriptor.exported_at.isoformat(),
            "head": descriptor.head.model_dump(mode="json"),
        }
        if not resuming or (record or {}).get("status") == UNPACKING:
            target.mkdir(parents=True, exist_ok=True)
            step = time.perf_counter()
            # Written first, so that an unpack cut short is completed by the same command.
            _write_import(workspace, {"status": UNPACKING, "imported_from": imported_from})
            written = _unpack(workspace, reader)
            configure(log_file=workspace.log_path)
            stack = Stack(workspace, manifest)
            names = planned_names(manifest, stack)
            write_example(workspace, manifest, stack)
            _env_written, generated = extend_env(workspace, names)
            _write_import(workspace, {"status": UNPACKED, "imported_from": imported_from})
            timings["unpack_ms"] = _ms(step)
            log.info(
                "workspace unpacked",
                target=str(target),
                files=written,
                generated=generated,
                **timings,
            )
        else:
            configure(log_file=workspace.log_path)
            log.info("import continued", target=str(target), status=(record or {}).get("status"))
        stack = Stack(workspace, manifest)
        to_fill = _to_fill(workspace, manifest, stack)
        absent = stack.missing_env()
        base = {
            "workspace": str(target),
            "env_file": str(workspace.env_path),
            "twin_id": descriptor.twin.id,
            "bundle": str(path),
            "warnings": warnings,
            "to_fill": to_fill,
        }
        if absent:
            log.info("the workspace waits for its .env to be filled in", missing=absent)
            result = {
                **base,
                "status": UNPACKED,
                "next": _again(path, target, replace_data),
                "missing": absent,
                "duration_ms": _ms(started),
            }
            emit(ctx, result, _import_text(result))
            return

        require_image_reads(session(ctx), stack, manifest)
        with workspace.exclusive():
            if volume is not None:
                log.warn("replacing the store of this stack, as asked", volume=volume)
                with timed(log, "stack removed with its data", project=plan.project):
                    stack.down(volumes=True)
            step = time.perf_counter()
            with timed(log, "store started", project=plan.project):
                stack.up(STORE_SERVICE)
            timings["store_start_ms"] = _ms(step)
            with tempfile.TemporaryDirectory(prefix="import-", dir=workspace.meta_dir) as temp:
                staging = Path(temp)
                step = time.perf_counter()
                source = reader.extract(bundles.STORE, staging)
                timings["store_unpack_ms"] = _ms(step)
                step = time.perf_counter()
                restore(stack, plan.database, source)
                timings["restore_ms"] = _ms(step)
                _write_import(workspace, {"status": RESTORED, "imported_from": imported_from})
                step = time.perf_counter()
                with timed(log, "artifacts copied into the artifact volume"):
                    artifact_files = _artifacts_in(stack, reader, staging)
                timings["artifacts_ms"] = _ms(step)
            found = heads(stack, plan.database)
            differing = {
                name: {"expected": expected, "found": found.get(name)}
                for name, expected in sorted(descriptor.branches.items())
                if found.get(name) != expected
            }
            for name in sorted(set(found) - set(descriptor.branches)):
                differing[name] = {"expected": None, "found": found[name]}
            if differing:
                first, ids = next(iter(differing.items()))
                _write_import(
                    workspace,
                    {
                        "status": FAILED,
                        "imported_from": imported_from,
                        "differing": differing,
                        "checked_at": now().isoformat(),
                    },
                )
                log.error("a branch is not at the head the bundle names", differing=differing)
                raise CheckFailed(
                    f"after the restore, branch {first} is at {ids['found']}, the bundle names "
                    f"{ids['expected']}; the store does not hold the twin that was exported: "
                    "export it again, then import with --replace-data",
                    code=HEAD_MISMATCH,
                    detail={"branches": differing, "workspace": str(target)},
                )
            head = head_version(stack, plan.database, found[LIVE_BRANCH])
            _write_import(
                workspace,
                {
                    "status": VERIFIED,
                    "imported_from": imported_from,
                    "head_verified": True,
                    "head": head.model_dump(mode="json"),
                    "branches": found,
                    "verified_at": now().isoformat(),
                },
            )
    elapsed = _ms(started)
    log.info(
        "import finished",
        workspace=str(target),
        seq=head.seq,
        head=head.id,
        branches=len(found),
        artifacts=artifact_files,
        duration_ms=elapsed,
        **timings,
    )
    result = {
        **base,
        "status": VERIFIED,
        "head": head.model_dump(mode="json"),
        "branches": found,
        "artifacts": artifact_files,
        "next": ["dta source probe", "dta build", "dta accept", "dta install-skill"],
        "duration_ms": elapsed,
        "timings_ms": timings,
    }
    emit(ctx, result, _import_text(result))


app.command("export")(export)
app.command("import")(import_bundle)
