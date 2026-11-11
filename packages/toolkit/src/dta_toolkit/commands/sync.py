"""`dta sync derive`: the sync interval the contract and the sources call for.

The rule is applied to what the manifest holds: the sources' refresh and rate
limit periods, the questions' stalenesses and the change times profiling
measured. With `--write` the interval it gives is recorded as derived, with
the inputs it came from. An interval somebody confirmed is never replaced.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed
from dta_core.log import Log
from dta_core.manifest import Manifest, Status
from dta_core.manifest.checks.view import fmt_duration
from dta_core.manifest.derive import IntervalDerivation, derive_interval
from dta_core.manifest.models import Sync
from pydantic import TypeAdapter

from ..output import command, emit, session
from ..tasks.state import load_state as load_tasks_state
from ..workspace import Workspace
from .common import fail_check

log = Log("commands.sync")

INTERVAL_PATH = "sync.interval"
DERIVATION_PATH = "sync.derivation"
UNDERIVABLE = "sync.underivable"
INFEASIBLE = "sync.infeasible"
INTERVAL_CONFIRMED = "sync.interval_confirmed"
BATCH_OPEN = "sync.batch_open"

_DURATION = TypeAdapter(timedelta)
_INPUTS = ("source_period", "rate_period", "min_staleness", "freshness_bound", "change_time")


def _refuse_while_batch_open(workspace: Workspace) -> None:
    """The rule's inputs are what the tasks of an open batch are still measuring."""
    batch = load_tasks_state(workspace).current()
    if batch is not None:
        log.warn("interval not written: a task batch is open", batch=batch.id)
        raise CheckFailed(
            f"task batch {batch.id} is open and its tasks may still change what the interval "
            "is derived from; merge or abort the batch first, or run without --write",
            code=BATCH_OPEN,
            detail={"batch": batch.id},
        )


def _iso(value: timedelta | None) -> str | None:
    """A duration as the manifest writes it."""
    return _DURATION.dump_python(value, mode="json") if value is not None else None


def _stated(manifest: Manifest) -> tuple[timedelta | None, bool]:
    """The interval the manifest holds, and whether somebody stands behind it."""
    value = manifest.sync.interval if manifest.sync else None
    status = manifest.field_status.get(INTERVAL_PATH, Status.confirmed)
    return value, value is not None and status == Status.confirmed


def _result(manifest: Manifest, found: IntervalDerivation) -> dict[str, Any]:
    stated, confirmed = _stated(manifest)
    return {
        "interval": _iso(found.interval),
        "binding": found.binding,
        "feasible": found.feasible,
        "problems": list(found.problems),
        "rule": found.inputs.rule,
        "inputs": {name: _iso(getattr(found.inputs, name)) for name in _INPUTS},
        "manifest_interval": _iso(stated),
        "manifest_interval_confirmed": confirmed,
        "agree": stated is not None and stated == found.interval,
        "written": False,
    }


def _text(result: dict[str, Any], found: IntervalDerivation) -> str:
    lines = [f"Rule:     {result['rule']}"]
    for name in _INPUTS:
        value = getattr(found.inputs, name)
        lines.append(f"  {name:<16} {fmt_duration(value) if value is not None else 'not known'}")
    if found.interval is None:
        lines.append("Interval: cannot be derived; nothing the rule needs is known")
    else:
        lines.append(f"Interval: {fmt_duration(found.interval)} (decided by {found.binding})")
    lines += [f"  problem  {problem}" for problem in found.problems]
    stated = result["manifest_interval"]
    if stated is None:
        lines.append("Manifest: sync.interval is not set")
    else:
        kind = "confirmed" if result["manifest_interval_confirmed"] else "not confirmed"
        same = "the same" if result["agree"] else "differs"
        lines.append(f"Manifest: sync.interval is {stated} ({kind}); {same}")
    if result["written"]:
        lines.append(f"Wrote {INTERVAL_PATH} and {DERIVATION_PATH}, marked derived.")
    return "\n".join(lines)


@command
def derive(
    ctx: typer.Context,
    write: Annotated[
        bool,
        typer.Option("--write", help="Record the interval and its inputs in the manifest."),
    ] = False,
) -> None:
    """Apply the interval rule to the manifest. Exit 1 when no interval meets the contract."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    found = derive_interval(manifest)
    result = _result(manifest, found)
    if found.interval is None:
        log.warn("interval cannot be derived: no input of the rule is known")
        fail_check(
            ctx,
            UNDERIVABLE,
            "the interval cannot be derived: profile the sources, or state their refresh "
            "periods and the questions' stalenesses",
            result,
            _text(result, found),
        )
    if not found.feasible:
        log.warn("no interval meets the contract", problems=list(found.problems))
        fail_check(
            ctx,
            INFEASIBLE,
            f"no interval meets the contract: {found.problems[0]}",
            result,
            _text(result, found),
        )
    if write and not result["agree"]:
        with workspace.exclusive():
            _refuse_while_batch_open(workspace)
            current = workspace.load_manifest()
            stated, confirmed = _stated(current)
            if confirmed:
                log.warn(
                    "derived interval not written: the manifest's interval is confirmed",
                    stated=_iso(stated),
                    derived=_iso(found.interval),
                )
                fail_check(
                    ctx,
                    INTERVAL_CONFIRMED,
                    f"sync.interval is confirmed as {_iso(stated)}; it is not replaced by the "
                    f"derived {_iso(found.interval)}. Change it in the manifest to use the rule.",
                    result,
                    _text(result, found),
                )
            sync = current.sync or Sync()
            sync.interval = found.interval
            sync.derivation = found.inputs
            current.sync = sync
            statuses = dict(current.field_status)
            statuses[INTERVAL_PATH] = Status.derived
            statuses[DERIVATION_PATH] = Status.derived
            current.field_status = dict(sorted(statuses.items()))
            workspace.save_manifest(current)
        result["written"] = True
        result["manifest_interval"] = result["interval"]
        result["manifest_interval_confirmed"] = False
        result["agree"] = True
        log.info(
            "sync interval derived and written",
            interval=result["interval"],
            binding=found.binding,
        )
    else:
        log.info("sync interval derived", interval=result["interval"], binding=found.binding)
    emit(ctx, result, _text(result, found))
