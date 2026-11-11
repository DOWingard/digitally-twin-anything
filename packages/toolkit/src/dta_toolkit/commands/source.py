"""`dta source probe`: read a few rows of every source from inside the twin's stack.

The probe runs in the twin's own image on the twin's own networks, so it
answers whether the twin will reach its sources, not whether this machine can.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from dta_core.log import Log
from dta_core.manifest import dump_manifest
from dta_core.manifest.checks import subject_of

from ..output import command, emit, session
from ..stack import Stack
from .common import fail_check, require_complete_env, scrub_values, timed

log = Log("commands.source")

ACCESS_STAGE = 2
ACCESS_FAILED = "access.failed"
PROBE_TIMEOUT_S = 600.0


def failing_entries(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each source or dataset that could not be read, with its error code."""
    failing = []
    for report in reports:
        if report.get("ok"):
            continue
        source_id = report.get("source_id")
        if report.get("error_code"):
            failing.append(
                {
                    "source": source_id,
                    "dataset": None,
                    "error_code": report.get("error_code"),
                    "error": report.get("error"),
                }
            )
        for dataset in report.get("datasets") or []:
            if not dataset.get("ok"):
                failing.append(
                    {
                        "source": source_id,
                        "dataset": dataset.get("dataset"),
                        "error_code": dataset.get("error_code"),
                        "error": dataset.get("error"),
                    }
                )
        if not report.get("error_code") and all(d.get("ok") for d in report.get("datasets") or []):
            failing.append(
                {"source": source_id, "dataset": None, "error_code": None, "error": "not ok"}
            )
    return failing


def _text(reports: list[dict[str, Any]], failing: list[dict[str, Any]], recorded: bool) -> str:
    lines = []
    for report in reports:
        datasets = report.get("datasets") or []
        state = "ok  " if report.get("ok") else "FAIL"
        lines.append(f"{state} {report.get('source_id')} ({len(datasets)} dataset(s))")
    for entry in failing:
        where = entry["source"] + (f".{entry['dataset']}" if entry["dataset"] else "")
        lines.append(f"  {where}: {entry['error_code']}: {entry['error']}")
    lines.append(
        "Access gate recorded." if recorded else "Limited to some sources: no gate recorded."
    )
    return "\n".join(lines)


@command
def probe(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Probe only this source; may be repeated. Records no gate."),
    ] = None,
) -> None:
    """Read a few rows of each source from inside the stack. Records the access gate."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    stack = Stack(workspace, manifest)
    env = require_complete_env(stack)
    args = ["probe"]
    for source_id in source or []:
        args += ["--source", source_id]
    with timed(log, "probe", sources=source or "all"):
        document = stack.runtime(
            *args, input=dump_manifest(manifest), store=False, timeout=PROBE_TIMEOUT_S
        )
    reports = scrub_values(document.get("sources") or [], env)
    ok = bool(document.get("ok"))
    failing = failing_entries(reports)
    recorded = not source
    if recorded:
        workspace.record_gate(
            ACCESS_STAGE,
            passed=ok,
            subject_hash=subject_of(ACCESS_STAGE, manifest),
            detail={
                "sources": len(reports),
                "failing": sorted({str(entry["source"]) for entry in failing}),
            },
        )
    result = {"ok": ok, "sources": reports, "failing": failing, "gate_recorded": recorded}
    text = _text(reports, failing, recorded)
    if not ok:
        log.warn("probe found sources that cannot be read", failing=failing)
        names = sorted({str(entry["source"]) for entry in failing})
        message = f"cannot read {', '.join(names)}" if names else "no source was probed"
        fail_check(ctx, ACCESS_FAILED, message, result, text)
    log.info("every source can be read", sources=len(reports), gate_recorded=recorded)
    emit(ctx, result, text)
