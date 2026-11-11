"""`dta manifest ...`: validate, list gaps, confirm fields, render the summary."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Owner, Status, gaps, required_fields
from dta_core.manifest.checks import errors_of, run_checks, subject_of, summary_hash

from ..output import command, emit, session
from .common import (
    build_summary,
    fail_check,
    findings_text,
    gap_dict,
    gaps_text,
    resolve_in_workspace,
)

log = Log("commands.manifest")

_DESIGN_STAGE = 4


@command
def validate(
    ctx: typer.Context,
    complete: Annotated[
        bool, typer.Option("--complete", help="Also fail while any required field is open.")
    ] = False,
) -> None:
    """Run every manifest check. Exit 1 on any error finding. Records the design gate."""
    workspace = session(ctx).workspace
    with workspace.exclusive():
        manifest = workspace.load_manifest()
        findings = run_checks(manifest)
        errors = errors_of(findings)
        subject = subject_of(_DESIGN_STAGE, manifest)
        workspace.record_gate(
            _DESIGN_STAGE,
            passed=not errors,
            subject_hash=subject,
            detail={"errors": len(errors), "warnings": len(findings) - len(errors)},
        )
    open_gaps = gaps(manifest) if complete else []

    result: dict = {
        "findings": [finding.as_dict() for finding in findings],
        "errors": len(errors),
        "warnings": len(findings) - len(errors),
        "manifest_hash": subject,
    }
    lines = findings_text(findings)
    if complete:
        result["gaps"] = [gap_dict(state) for state in open_gaps]
        lines += ["", "Gaps:", *gaps_text(open_gaps)]
    tally = f"{result['errors']} error(s), {result['warnings']} warning(s)"
    if complete:
        tally += f", {len(open_gaps)} gap(s)"
    lines += ["", tally]
    text = "\n".join(lines)

    if errors or open_gaps:
        log.warn(
            "manifest validation failed",
            errors=len(errors),
            gaps=len(open_gaps),
            codes=sorted({finding.code for finding in errors}),
        )
        fail_check(ctx, "validate.failed", f"manifest is not valid: {tally}", result, text)
    log.info("manifest valid", warnings=result["warnings"], complete=complete)
    emit(ctx, result, text)


@command
def list_gaps(ctx: typer.Context) -> None:
    """Required fields that are still unknown or waiting for confirmation, by owner."""
    workspace = session(ctx).workspace
    open_gaps = gaps(workspace.load_manifest())
    by_owner = {
        owner.value: [gap_dict(state) for state in open_gaps if state.owner == owner]
        for owner in (Owner.user, Owner.agent)
    }
    log.info("gaps listed", user=len(by_owner["user"]), agent=len(by_owner["agent"]))
    lines = []
    for owner, entries in by_owner.items():
        lines.append(f"{owner} ({len(entries)})")
        lines += [
            f"  {entry['path']}  {entry['status']}  {entry['description']}" for entry in entries
        ]
    emit(ctx, {"total": len(open_gaps), "by_owner": by_owner}, "\n".join(lines))


@command
def confirm(
    ctx: typer.Context,
    paths: Annotated[
        list[str] | None, typer.Argument(help="Dotted paths of user-owned fields to confirm.")
    ] = None,
    all_user: Annotated[
        bool,
        typer.Option("--all-user", help="Confirm every user-owned required field with a value."),
    ] = False,
) -> None:
    """Mark user-owned required fields as confirmed."""
    if not paths and not all_user:
        log.error("confirm called without paths or --all-user")
        raise DtaError("name at least one path, or pass --all-user", code="confirm.nothing")
    workspace = session(ctx).workspace
    with workspace.exclusive():
        manifest = workspace.load_manifest()
        states = {state.path: state for state in required_fields(manifest)}
        refused = []
        wanted = set()
        for path in paths or []:
            state = states.get(path)
            if state is None:
                refused.append(
                    {
                        "path": path,
                        "code": "confirm.not_required",
                        "reason": "not a required field of this manifest",
                    }
                )
            elif state.owner != Owner.user:
                refused.append(
                    {
                        "path": path,
                        "code": "confirm.not_user_field",
                        "reason": "owned by the agent; only user-owned fields are confirmed",
                    }
                )
            elif state.status == Status.unknown:
                refused.append(
                    {"path": path, "code": "confirm.no_value", "reason": "has no value to confirm"}
                )
            else:
                wanted.add(path)
        if refused:
            log.error("confirm refused", refused=refused)
            raise DtaError(
                f"{len(refused)} path(s) cannot be confirmed; nothing was changed",
                code="confirm.refused",
                detail={"refused": refused},
            )
        if all_user:
            wanted |= {
                state.path
                for state in states.values()
                if state.owner == Owner.user and state.status != Status.unknown
            }
        changed = sorted(path for path in wanted if states[path].status != Status.confirmed)
        unchanged = sorted(wanted - set(changed))
        if changed:
            updated = dict(manifest.field_status)
            updated.update({path: Status.confirmed for path in changed})
            manifest.field_status = dict(sorted(updated.items()))
            workspace.save_manifest(manifest)
    log.info("fields confirmed", confirmed=changed, unchanged=len(unchanged))
    result = {"confirmed": changed, "unchanged": unchanged, "remaining_gaps": len(gaps(manifest))}
    lines = [f"confirmed  {path}" for path in changed]
    lines += [f"unchanged  {path} (already confirmed)" for path in unchanged]
    lines.append(f"{result['remaining_gaps']} gap(s) remain.")
    emit(ctx, result, "\n".join(lines))


@command
def summary(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Where to write it, relative to the workspace. Default .dta/summary.md."
        ),
    ] = None,
) -> None:
    """The one-page contract summary in Markdown; written to a file and printed."""
    workspace = session(ctx).workspace
    target = resolve_in_workspace(workspace, out) if out else workspace.meta_dir / "summary.md"
    with workspace.exclusive():
        manifest = workspace.load_manifest()
        text = build_summary(workspace, manifest)
        workspace.atomic_write(target, text)
    digest = summary_hash(text)
    log.info("summary written", path=str(target), summary_hash=digest)
    emit(ctx, {"path": str(target), "summary_hash": digest, "markdown": text}, text.rstrip("\n"))
