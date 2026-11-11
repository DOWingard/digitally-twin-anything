"""Shared-file writes that a dying process cannot leave half recorded.

Replacing a watched file and entering the new content in the ledger are two
steps. A process killed between them leaves a file that is exactly what the
toolkit meant to write but that the ledger does not know, so it would be
reported as changed by someone else and block the batch.

Before each such write this module notes, in a small file of its own, which
file is about to be replaced and the hash of the new content. The next command
finds the note: if the file on disk has exactly that hash, the write did happen
and is simply repeated with the same bytes, which also completes the ledger
entry; otherwise the write never happened and the note is dropped. Content with
any other hash is never accepted this way.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

from dta_core.errors import WorkspaceError
from dta_core.log import Log

from ..workspace import Workspace
from .tree import sha256_bytes

log = Log("tasks.durable")

# The suffix keeps the note out of the watched files, like any other temporary file.
_NOTE = "tasks.pending.tmp"


def _note_path(workspace: Workspace) -> Path:
    return workspace.meta_dir / _NOTE


def write_recorded(
    workspace: Workspace,
    path: Path,
    data: str | bytes,
    write: Callable[[], object] | None = None,
) -> None:
    """Replace a watched file so that the write can be completed if this process dies.

    `write` replaces the file with exactly `data`; by default that is a plain
    atomic write. The caller holds the workspace lock.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    note = {"path": str(path.relative_to(workspace.root)), "sha256": sha256_bytes(payload)}
    workspace.atomic_write(_note_path(workspace), json.dumps(note))
    if write is None:
        workspace.atomic_write(path, payload)
    else:
        write()
    workspace.remove(_note_path(workspace))


def complete_interrupted_write(workspace: Workspace) -> None:
    """Finish, or forget, a write that an earlier process announced. The caller holds the lock."""
    note_path = _note_path(workspace)
    if not note_path.is_file():
        return
    allowed = {
        str(target.relative_to(workspace.root)): target
        for target in (workspace.manifest_path, workspace.tasks_state_path)
    }
    try:
        note = json.loads(note_path.read_text(encoding="utf-8"))
        target = allowed[note["path"]]
        expected = note["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Only this module writes the note, and it writes it atomically.
        log.error("note of a pending write is unusable; dropped", exc=exc, path=str(note_path))
        workspace.remove(note_path)
        return
    try:
        current = target.read_bytes()
    except OSError:
        current = None
    if current is not None and sha256_bytes(current) == expected:
        try:
            workspace.atomic_write(target, current)
        except WorkspaceError as exc:
            # The file is in place but the ledger cannot take the entry. The
            # note stays, so the write is completed once the ledger is usable
            # again; until then every command that checks the ledger refuses.
            log.error(
                "an interrupted write cannot be recorded; its note is kept",
                exc=exc,
                path=note["path"],
            )
            return
        log.warn("completed a write that was interrupted before it was recorded", path=note["path"])
    else:
        log.warn("an announced write never happened; nothing to complete", path=note["path"])
    workspace.remove(note_path)


@contextlib.contextmanager
def critical(workspace: Workspace) -> Iterator[None]:
    """Hold the workspace lock, with any interrupted write of an earlier process completed."""
    with workspace.exclusive():
        complete_interrupted_write(workspace)
        yield
