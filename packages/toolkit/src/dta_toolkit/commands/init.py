"""`dta init`: create a twin workspace."""

from __future__ import annotations

import re
from typing import Annotated

import typer
from dta_core.errors import DtaError, WorkspaceError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import Twin

from ..output import command, emit, session
from ..workspace import State, Workspace, now

log = Log("commands.init")

_ID = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_FALLBACK_ID = "twin"


def default_id(directory_name: str) -> str:
    """The directory name made into a valid twin id."""
    text = re.sub(r"[^a-z0-9_-]+", "-", directory_name.lower())
    text = re.sub(r"^[^a-z]+", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text[:63].rstrip("-_") or _FALLBACK_ID


def _ensure_env_ignored(workspace: Workspace) -> bool:
    """Make `.gitignore` list `.env`, keeping whatever it already says. True if written."""
    path = workspace.root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if ".env" in {line.strip() for line in existing.splitlines()}:
        return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    workspace.atomic_write(path, existing + ".env\n")
    return True


def _create(workspace: Workspace, twin_id: str, name: str) -> bool:
    """Lay out the workspace. Returns whether a new manifest was written."""
    for directory in (
        workspace.ontology_dir,
        workspace.profiles_dir,
        workspace.tasks_dir,
        workspace.stack_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    manifest_created = False
    if workspace.manifest_path.exists():
        log.warn(
            "a manifest already exists; it is kept as it is", path=str(workspace.manifest_path)
        )
    else:
        workspace.save_manifest(Manifest(twin=Twin(id=twin_id, name=name)))
        manifest_created = True
    _ensure_env_ignored(workspace)
    # Written last: its presence is what marks the directory as a workspace, so an
    # interrupted init is completed by running init again.
    workspace.atomic_write(workspace.state_path, State(created_at=now()).model_dump_json(indent=2))
    return manifest_created


@command
def init(
    ctx: typer.Context,
    twin_id: Annotated[
        str | None,
        typer.Option("--id", help="Twin id; defaults to the directory name made into a valid id."),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Display name; defaults to the directory name.")
    ] = None,
) -> None:
    """Create the workspace and a manifest skeleton. Changes nothing in an existing workspace."""
    workspace = session(ctx).workspace
    root = workspace.root
    existing = {"created": False, "root": str(root)}
    if workspace.exists():
        if twin_id or name:
            log.warn("options ignored: the workspace already exists", root=str(root))
        log.info("workspace already exists; nothing changed", root=str(root))
        emit(ctx, existing, f"Workspace already exists at {root}; nothing changed.")
        return

    resolved_id = twin_id or default_id(root.name)
    if not _ID.match(resolved_id):
        log.error("twin id is not valid", twin_id=resolved_id)
        raise DtaError(
            f"'{resolved_id}' is not a valid twin id (lowercase letter, then letters, digits,"
            " '-' or '_'; at most 63 characters)",
            code="init.bad_id",
            detail={"id": resolved_id, "pattern": _ID.pattern},
        )
    resolved_name = name or root.name or resolved_id

    try:
        with workspace.exclusive():
            if workspace.exists():
                log.info("workspace was created concurrently; nothing changed", root=str(root))
                emit(ctx, existing, f"Workspace already exists at {root}; nothing changed.")
                return
            manifest_created = _create(workspace, resolved_id, resolved_name)
    except OSError as exc:
        log.error("workspace cannot be created", exc=exc, root=str(root))
        raise WorkspaceError(
            f"cannot create a workspace at {root}: {exc.strerror or exc}",
            code="workspace.unwritable",
            detail={"root": str(root)},
        ) from exc

    log.info("workspace created", root=str(root), twin_id=resolved_id, manifest=manifest_created)
    result = {
        "created": True,
        "root": str(root),
        "manifest_created": manifest_created,
        # The id and name are only those of the new skeleton; a kept manifest says its own.
        "twin": {"id": resolved_id, "name": resolved_name} if manifest_created else None,
    }
    text = (
        f"Created workspace for twin '{resolved_id}' at {root}."
        if manifest_created
        else f"Created workspace at {root}; the existing manifest was kept."
    )
    emit(ctx, result, text)
