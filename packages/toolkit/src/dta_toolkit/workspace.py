"""The twin workspace on disk and the only sanctioned way to write its shared files.

Shared files (the manifest, the ontology, build state, the container stack and
the environment files) have exactly one writer at a time. Every write goes
through `Workspace.exclusive()` and `Workspace.atomic_write()`: a cross-process
lock, then a temporary file renamed over the target, so a reader never sees a
partial file and two writers never interleave.

Every such write is also entered in a ledger of content hashes. Comparing the
ledger with the files on disk reveals any change that did not come through this
class, which is how work delegated to other agents is kept out of shared files:
everything outside the private task directories is watched.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
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
    # "interactive": a person typed the approval at a terminal.
    # "non_interactive": recorded by a caller on someone's behalf; always shown as such.
    mode: Literal["interactive", "non_interactive"]
    approved_by: str | None = None


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
        self.ledger_path = self.meta_dir / "ledger.json"
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
        self._ledger_note(target)
        return target

    def remove(self, path: Path) -> None:
        """Delete a file as a sanctioned change. The caller must hold the lock."""
        if not self.locked:
            log.error("refused a delete without the workspace lock", target=str(path))
            raise WorkspaceError(
                "shared files may only be removed while holding the workspace lock",
                code="workspace.unlocked_write",
                detail={"target": str(path)},
            )
        target = self._inside(path)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        self._ledger_note(target)

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

    # -- watched files and the ledger --------------------------------------

    def _watched(self, path: Path) -> bool:
        """True for files that only the toolkit, or the orchestrator between batches, may change."""
        if path.name.endswith(_TMP_SUFFIX) or path in (self.lock_path, self.ledger_path):
            return False
        if self.root not in path.parents:
            return True  # the installed skill lives outside the workspace
        relative = path.relative_to(self.root).parts
        if relative[0] == "tasks":
            return False  # private task directories are checked by the task commands
        if relative[:2] == (".dta", "logs"):
            return False
        if relative[0] == ".git":
            return relative in ((".git", "index"), (".git", "HEAD"))
        return True

    def _key(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if self.root in path.parents else str(path)

    def watched_files(self) -> list[Path]:
        candidates = [p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()]
        if self.exists():
            with contextlib.suppress(WorkspaceError):
                installed = self.load_state().installed_skill
                if installed and Path(installed).is_dir():
                    candidates.extend(p for p in Path(installed).rglob("*") if p.is_file())
        return sorted(p for p in candidates if self._watched(p))

    def fingerprint(self) -> dict[str, str]:
        """Content hash of every watched file, keyed by path."""
        return {self._key(path): sha256_file(path) for path in self.watched_files()}

    def _read_ledger(self) -> dict[str, str] | None:
        if not self.ledger_path.is_file():
            return None
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("ledger cannot be read", exc=exc, path=str(self.ledger_path))
            raise WorkspaceError(
                "the ledger of sanctioned writes is unreadable",
                code="workspace.bad_ledger",
                detail={"path": str(self.ledger_path)},
            ) from exc

    def _write_ledger(self, ledger: dict[str, str]) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=self.meta_dir, prefix=".ledger.", suffix=_TMP_SUFFIX)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, self.ledger_path)

    def _ledger_note(self, target: Path) -> None:
        """Record a sanctioned change to one file, if a ledger is being kept."""
        if not self._watched(target):
            return
        ledger = self._read_ledger()
        if ledger is None:
            return
        if target.is_file():
            ledger[self._key(target)] = sha256_file(target)
        else:
            ledger.pop(self._key(target), None)
        self._write_ledger(ledger)

    def rebase_ledger(self) -> dict[str, str]:
        """Accept the files as they are now as the baseline. The caller must hold the lock."""
        if not self.locked:
            raise WorkspaceError(
                "the ledger may only be rebased while holding the workspace lock",
                code="workspace.unlocked_write",
            )
        ledger = self.fingerprint()
        self._write_ledger(ledger)
        log.info("ledger rebased", files=len(ledger))
        return ledger

    def drop_ledger(self) -> None:
        """Stop watching. The caller must hold the lock."""
        if not self.locked:
            raise WorkspaceError(
                "the ledger may only be dropped while holding the workspace lock",
                code="workspace.unlocked_write",
            )
        with contextlib.suppress(FileNotFoundError):
            self.ledger_path.unlink()
        log.info("ledger dropped")

    def foreign_changes(self) -> list[dict[str, str]]:
        """Watched files that differ from the ledger: changes not made through this class.

        Empty when no ledger is being kept.
        """
        ledger = self._read_ledger()
        if ledger is None:
            return []
        current = self.fingerprint()
        changes = []
        for key in sorted(set(ledger) | set(current)):
            if key not in current:
                changes.append({"path": key, "change": "removed"})
            elif key not in ledger:
                changes.append({"path": key, "change": "added"})
            elif ledger[key] != current[key]:
                changes.append({"path": key, "change": "modified"})
        if changes:
            log.warn("watched files changed outside the toolkit", changes=changes)
        return changes
