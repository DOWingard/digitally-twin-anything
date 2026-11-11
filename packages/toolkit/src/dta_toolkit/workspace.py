"""The twin workspace on disk and the only sanctioned way to write its shared files.

Shared files (the manifest, the ontology, build state, the container stack and
the environment files) have exactly one writer at a time. Every write goes
through `Workspace.exclusive()` and `Workspace.atomic_write()`: a cross-process
lock, then a temporary file renamed over the target, so a reader never sees a
partial file and two writers never interleave.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import filelock
from dta_core.errors import LockTimeout, WorkspaceError
from dta_core.log import Log
from dta_core.manifest import Manifest, dump_manifest, load_manifest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = Log("workspace")

STAGES = {
    0: "intake",
    1: "contract",
    2: "access",
    3: "profiling",
    4: "design",
    5: "gaps",
    6: "build",
    7: "acceptance",
    8: "handover",
}
_TMP_SUFFIX = ".tmp"
_DEFAULT_LOCK_TIMEOUT_S = 30.0


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: int
    passed: bool
    at: datetime
    # Hash of whatever the gate examined; a recorded pass is current only while
    # the gate's subject still hashes to this value.
    subject_hash: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_hash: str
    contract_hash: str
    summary_hash: str
    signed_at: datetime


class State(BaseModel):
    """Build progress. Lives in `.dta/state.json`."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    created_at: datetime
    gates: dict[str, GateResult] = Field(default_factory=dict)  # keyed by stage name
    signatures: list[Signature] = Field(default_factory=list)
    installed_skill: str | None = None


def now() -> datetime:
    return datetime.now(UTC)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "manifest.yaml"
        self.ontology_dir = self.root / "ontology"
        self.profiles_dir = self.root / "profiles"
        self.tasks_dir = self.root / "tasks"
        self.stack_dir = self.root / "stack"
        self.env_path = self.root / ".env"
        self.env_example_path = self.root / ".env.example"
        self.meta_dir = self.root / ".dta"
        self.state_path = self.meta_dir / "state.json"
        self.tasks_state_path = self.meta_dir / "tasks.json"
        self.lock_path = self.meta_dir / "lock"
        self.log_path = self.meta_dir / "logs" / "toolkit.log"
        self._lock = filelock.FileLock(str(self.lock_path))

    # -- existence ----------------------------------------------------------

    def exists(self) -> bool:
        return self.state_path.is_file()

    def require(self) -> None:
        if not self.exists():
            log.error("no workspace at this path", root=str(self.root))
            raise WorkspaceError(
                f"{self.root} is not a twin workspace; run `dta init` first",
                code="workspace.missing",
                detail={"root": str(self.root)},
            )

    # -- the single-writer discipline --------------------------------------

    @property
    def locked(self) -> bool:
        return self._lock.is_locked

    @contextlib.contextmanager
    def exclusive(self, timeout: float = _DEFAULT_LOCK_TIMEOUT_S) -> Iterator[None]:
        """Hold the workspace lock. Re-entrant within one process."""
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=timeout)
        except filelock.Timeout as exc:
            log.warn("workspace lock is held elsewhere", root=str(self.root), waited_s=timeout)
            raise LockTimeout(
                f"another process holds the workspace lock (waited {timeout:g}s)",
                detail={"root": str(self.root)},
            ) from exc
        try:
            yield
        finally:
            self._lock.release()

    def _inside(self, path: Path) -> Path:
        target = path if path.is_absolute() else self.root / path
        target = Path(os.path.normpath(target))
        if target != self.root and self.root not in target.parents:
            log.error("refused a write outside the workspace", target=str(target))
            raise WorkspaceError(
                f"{target} is outside the workspace",
                code="workspace.outside",
                detail={"target": str(target)},
            )
        return target

    def atomic_write(self, path: Path, data: str | bytes) -> Path:
        """Replace `path` in one step. The caller must hold the lock."""
        if not self.locked:
            log.error("refused a write without the workspace lock", target=str(path))
            raise WorkspaceError(
                "shared files may only be written while holding the workspace lock",
                code="workspace.unlocked_write",
                detail={"target": str(path)},
            )
        target = self._inside(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=_TMP_SUFFIX
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException as exc:
            log.error("atomic write failed; target left untouched", exc=exc, target=str(target))
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        log.debug("file replaced", target=str(target), size=len(payload))
        return target

    # -- state --------------------------------------------------------------

    def load_state(self) -> State:
        self.require()
        try:
            return State.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            log.error("build state cannot be read", exc=exc, path=str(self.state_path))
            raise WorkspaceError(
                "build state is unreadable or damaged",
                code="workspace.bad_state",
                detail={"path": str(self.state_path)},
            ) from exc

    def save_state(self, state: State) -> None:
        self.atomic_write(self.state_path, state.model_dump_json(indent=2))

    def record_gate(
        self,
        stage: int,
        passed: bool,
        subject_hash: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> State:
        """Store the outcome of a stage's gate and return the new state."""
        with self.exclusive():
            state = self.load_state()
            state.gates[STAGES[stage]] = GateResult(
                stage=stage, passed=passed, at=now(), subject_hash=subject_hash, detail=detail or {}
            )
            self.save_state(state)
        log.info("gate recorded", stage=STAGES[stage], passed=passed)
        return state

    # -- manifest -----------------------------------------------------------

    def load_manifest(self) -> Manifest:
        self.require()
        return load_manifest(self.manifest_path)

    def save_manifest(self, manifest: Manifest) -> None:
        self.atomic_write(self.manifest_path, dump_manifest(manifest))

    # -- shared zones -------------------------------------------------------

    def shared_zone_files(self) -> list[Path]:
        """Every file that has a single serial writer, in a stable order."""
        files = [
            self.manifest_path,
            self.state_path,
            self.env_path,
            self.env_example_path,
            self.root / ".git" / "index",
        ]
        for directory in (self.ontology_dir, self.stack_dir):
            if directory.is_dir():
                files.extend(p for p in directory.rglob("*") if p.is_file())
        state_installed = None
        if self.exists():
            with contextlib.suppress(WorkspaceError):
                state_installed = self.load_state().installed_skill
        if state_installed:
            skill = Path(state_installed)
            if skill.is_dir():
                files.extend(p for p in skill.rglob("*") if p.is_file())
        return sorted(p for p in files if p.is_file() and not p.name.endswith(_TMP_SUFFIX))

    def fingerprint(self) -> dict[str, str]:
        """Content hash of every shared file, keyed by path."""
        result = {}
        for path in self.shared_zone_files():
            key = str(path.relative_to(self.root)) if self.root in path.parents else str(path)
            result[key] = sha256_file(path)
        return result
