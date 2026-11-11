"""`dta manifest sign`: the user's approval of the contract.

A signature binds the manifest hash, the contract hash and the hash of the
summary that was shown. Interactive signing needs a person at a terminal who
types the twin id. The non-interactive mode is recorded as such, so nobody can
mistake an approval given on someone's behalf for one typed by them.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest, contract_hash, gaps, manifest_hash
from dta_core.manifest.checks import errors_of, run_checks, summary_hash

from ..output import command, emit, session
from ..workspace import STAGES, GateResult, Signature
from ..workspace import now as utc_now
from .common import build_summary, gap_dict

log = Log("commands.sign")

_GAPS_STAGE = 5


def _is_terminal() -> bool:
    """True when a person can read the summary and answer at a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _require_signable(manifest: Manifest) -> None:
    open_gaps = gaps(manifest)
    if open_gaps:
        log.warn("sign refused: gaps remain", gaps=len(open_gaps))
        raise CheckFailed(
            f"{len(open_gaps)} gap(s) remain; close them before signing",
            code="sign.gaps",
            detail={"gaps": [gap_dict(state) for state in open_gaps]},
        )
    errors = errors_of(run_checks(manifest))
    if errors:
        log.warn("sign refused: error findings", codes=sorted({f.code for f in errors}))
        raise CheckFailed(
            f"{len(errors)} error finding(s); run `dta manifest validate`",
            code="sign.errors",
            detail={"findings": [finding.as_dict() for finding in errors]},
        )


def _ask(twin_id: str, summary: str, to_stderr: bool) -> None:
    """Show the summary and require the twin id to be typed back.

    The answer is read straight from stdin so that nothing but the summary and
    the prompt is written, and both go to stderr when stdout carries JSON.
    """
    typer.echo(summary, err=to_stderr)
    typer.echo(f"To sign, type the twin id ({twin_id}): ", nl=False, err=to_stderr)
    answer = sys.stdin.readline()
    if not answer:
        log.warn("sign aborted at the prompt: input closed")
        raise CheckFailed("signing was aborted", code="sign.declined")
    if answer.strip() != twin_id:
        log.warn("sign declined: typed text does not match the twin id")
        raise CheckFailed("the typed text does not match the twin id", code="sign.declined")


@command
def sign(
    ctx: typer.Context,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Sign without a terminal, on the user's behalf; needs --approved-by.",
        ),
    ] = False,
    approved_by: Annotated[
        str | None, typer.Option("--approved-by", help="Name of the person who approved.")
    ] = None,
) -> None:
    """Record the user's approval of the contract. Requires no gaps and no error findings."""
    current = session(ctx)
    workspace = current.workspace
    approved_by = (approved_by or "").strip() or None
    if non_interactive and approved_by is None:
        log.error("non-interactive sign without --approved-by")
        raise DtaError(
            "--non-interactive needs --approved-by NAME", code="sign.approved_by_missing"
        )
    manifest = workspace.load_manifest()
    workspace.load_state()
    _require_signable(manifest)
    summary = build_summary(workspace, manifest)
    shown = manifest_hash(manifest)

    if non_interactive:
        mode = "non_interactive"
    else:
        if not _is_terminal():
            log.warn("sign refused: no terminal and no --non-interactive")
            raise DtaError(
                "signing needs a person at a terminal; to sign on the user's behalf pass "
                "--non-interactive --approved-by NAME",
                code="sign.not_interactive",
            )
        _ask(manifest.twin.id, summary, to_stderr=current.json_output)
        mode = "interactive"

    with workspace.exclusive():
        latest = workspace.load_manifest()
        if manifest_hash(latest) != shown:
            log.warn("sign refused: manifest changed while signing")
            raise DtaError(
                "the manifest changed while signing; run sign again",
                code="sign.changed",
            )
        state = workspace.load_state()
        signature = Signature(
            manifest_hash=shown,
            contract_hash=contract_hash(latest),
            summary_hash=summary_hash(summary),
            signed_at=utc_now(),
            mode=mode,
            approved_by=approved_by,
        )
        state.signatures.append(signature)
        state.gates[STAGES[_GAPS_STAGE]] = GateResult(
            stage=_GAPS_STAGE,
            passed=True,
            at=signature.signed_at,
            subject_hash=signature.contract_hash,
            detail={"mode": mode, "approved_by": approved_by},
        )
        workspace.atomic_write(workspace.meta_dir / "summary.md", summary)
        workspace.save_state(state)
    log.info(
        "contract signed",
        mode=mode,
        contract_hash=signature.contract_hash,
        approved_by_given=approved_by is not None,
    )
    result = signature.model_dump(mode="json")
    by = f" by {approved_by}" if approved_by else ""
    emit(ctx, result, f"Signed ({mode}){by}. Contract hash {signature.contract_hash}.")
