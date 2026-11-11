"""The life of a delegated task: opened in a batch, claimed, submitted, merged or abandoned.

Every decision here is taken while holding the workspace lock, on state read
inside that same critical section, and every change is written before the lock
is released. Nothing is ever decided on state read outside the lock. A failed
check never changes anything.

Order of writes, chosen so that a process that dies between any two of them
leaves a state the same command repairs when it is run again:

- open:   ledger rebased, then directories, then the state file. Until the
          state file says so, no batch exists; a rebased ledger without an open
          batch is discarded by the next open.
- claim:  `task.json`, then the lease in the state file. The file's content is
          fixed by the task alone, so the next claim finds exactly what it
          would write.
- merge:  manifest, then the state file. Applying a patch twice gives the same
          manifest, so a task that is still marked submitted after its patch
          went in is simply merged again.
- close:  state file (batch closed), then the ledger is dropped. While a batch
          is open the ledger therefore always exists, and its absence is
          itself refused.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest, manifest_hash

from ..workspace import Workspace
from ..workspace import now as _wall_clock
from .catalogue import require_holder, require_task_id
from .checks import (
    STATE_KEY,
    Output,
    apply,
    discard_stale_ledger,
    examine_output,
    ledger_reasons,
    manifest_pin,
    ontology_pin,
    own_reasons,
    pin_reasons,
    read_manifest,
    zone_reasons,
)
from .errors import TaskError, reason, refusal
from .planning import build_plan, describe_plan
from .state import (
    TERMINAL,
    Batch,
    Lease,
    Pins,
    TaskRecord,
    TasksState,
    load_state,
    save_state,
)
from .tree import is_real_dir, remove_entry, scan, sha256_bytes
from .zone import (
    INPUT_DIR,
    OUTPUT_DIR,
    RESULT_FILE,
    SPEC_FILE,
    Finding,
    inspect_task,
    inspect_zone,
    snapshot,
    spec_document,
    task_dir,
)

log = Log("tasks")

DEFAULT_TTL_S = 900
MAX_TTL_S = 86_400


def _now() -> datetime:
    return _wall_clock()


def _checkpoint(name: str) -> None:
    """Marks the gap between two durable writes. Does nothing.

    Tests replace it to stop the process at a named gap and then show that
    running the command again completes the work.
    """


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- plan and open -------------------------------------------------------------


def plan(workspace: Workspace, stage: str) -> dict[str, Any]:
    """The stage's tasks and batches. Changes nothing and takes no lock."""
    workspace.require()
    state = load_state(workspace)
    return describe_plan(build_plan(workspace.load_manifest(), stage, state), state)


def _fresh_task_dir(workspace: Workspace, task_id: str) -> None:
    """Give a task an empty directory. Whatever an earlier attempt left there is discarded."""
    base = task_dir(workspace, task_id)
    remove_entry(base)
    (base / INPUT_DIR).mkdir(parents=True)
    (base / OUTPUT_DIR).mkdir()


def open_batch(
    workspace: Workspace, stage: str, store_version: str | None = None
) -> dict[str, Any]:
    """Open the next batch of a stage: baseline the shared files, pin and prepare each task."""
    workspace.require()
    if store_version is not None and not (
        0 < len(store_version) <= 200 and store_version.isprintable()
    ):
        raise TaskError(
            "a store version is 1 to 200 printable characters", code="tasks.store_version.invalid"
        )
    with workspace.exclusive():
        state = load_state(workspace)
        current = state.current()
        if current is not None:
            log.warn("open refused: a batch is already open", batch=current.id)
            raise refusal(
                "tasks.batch.open",
                f"batch {current.id} is still open; merge or abort it first",
                batch=current.id,
                unfinished=sorted(
                    task_id
                    for task_id, task in current.tasks.items()
                    if task.status not in TERMINAL
                ),
            )
        discard_stale_ledger(workspace)

        # From here on every watched file is compared with what it is now. The
        # manifest is read after this point, so a change that slips in between
        # is either part of the baseline and of the pins, or caught later.
        workspace.rebase_ledger()
        _checkpoint("open.ledger_rebased")
        try:
            manifest = workspace.load_manifest()
            staged = build_plan(manifest, stage, state)
            specs = staged.to_open()
            if not specs:
                raise refusal(
                    "tasks.batch.nothing",
                    f"stage {stage} has no unmerged task left to open",
                    stage=stage,
                )
            ontology = ontology_pin(workspace, manifest)
            if workspace.tasks_dir.is_symlink() or (
                workspace.tasks_dir.exists() and not is_real_dir(workspace.tasks_dir)
            ):
                raise TaskError("tasks/ is not a directory", code="tasks.zone.unusable")
            workspace.tasks_dir.mkdir(exist_ok=True)
            records = {}
            for spec in specs:
                _fresh_task_dir(workspace, spec.id)
                records[spec.id] = TaskRecord(
                    spec=spec,
                    pins=Pins(
                        manifest_reads=manifest_pin(manifest, spec),
                        ontology=ontology,
                        store_version=store_version,
                    ),
                )
            batch = Batch(
                id=f"b{len(state.batches) + 1}",
                stage=stage,
                opened_at=_now(),
                baseline=snapshot(workspace, exclude=set(records)),
                tasks=records,
            )
        except BaseException:
            # Nothing was opened, so nothing is to be watched.
            workspace.drop_ledger()
            raise
        state.batches.append(batch)
        state.open_batch = batch.id
        _checkpoint("open.prepared")
        save_state(workspace, state)

    log.info(
        "batch opened",
        batch=batch.id,
        stage=stage,
        tasks=sorted(records),
        store_version=store_version,
    )
    return {
        **describe_plan(staged, state),
        "opened": {
            "batch": batch.id,
            "tasks": sorted(records),
            "store_version": store_version,
            "ontology": ontology.model_dump(),
        },
    }


# -- claim ---------------------------------------------------------------------


def _locate(state: TasksState, task_id: str) -> tuple[Batch, TaskRecord]:
    """The task's record in the open batch, or the reason there is none."""
    batch = state.current()
    if batch is not None and task_id in batch.tasks:
        return batch, batch.tasks[task_id]
    if state.latest(task_id) is None:
        log.warn("unknown task", task=task_id)
        raise TaskError(
            f"task {task_id} has never been opened",
            code="tasks.task.unknown",
            detail={"task": task_id},
        )
    if batch is None:
        raise refusal("tasks.batch.none", "no batch is open", task=task_id)
    raise refusal(
        "tasks.task.not_in_batch",
        f"task {task_id} is not part of the open batch {batch.id}",
        task=task_id,
        batch=batch.id,
    )


def _lease_view(task: TaskRecord, moment: datetime) -> dict[str, Any] | None:
    if task.lease is None:
        return None
    return {
        "holder": task.lease.holder,
        "acquired_at": _iso(task.lease.acquired_at),
        "expires_at": _iso(task.lease.expires_at),
        "expired": task.lease.expires_at <= moment,
        "generation": task.lease.generation,
    }


def _token_matches(task: TaskRecord, token: str) -> bool:
    return task.lease is not None and hmac.compare_digest(
        task.lease.token_sha256, _token_digest(token)
    )


def _require_ttl(ttl: int) -> int:
    if not 1 <= ttl <= MAX_TTL_S:
        raise TaskError(
            f"a lease lasts between 1 and {MAX_TTL_S} seconds",
            code="tasks.ttl.invalid",
            detail={"given": ttl},
        )
    return ttl


def _zone_refusal(task_id: str, findings: list[Finding]) -> Exception:
    codes = {"spec": "tasks.spec.changed", "input": "tasks.input.changed"}
    first = findings[0]
    return refusal(
        codes.get(first.part, "tasks.zone.violation"),
        f"the directory of task {task_id} is not as it should be: {first.path} {first.problem}",
        task=task_id,
        problems=[finding.as_dict() for finding in findings],
    )


def claim(
    workspace: Workspace, task_id: str, holder: str, ttl: int = DEFAULT_TTL_S
) -> dict[str, Any]:
    """Take the lease on a task that nobody holds, or whose holder let it expire."""
    require_task_id(task_id)
    require_holder(holder)
    _require_ttl(ttl)
    workspace.require()
    with workspace.exclusive():
        state = load_state(workspace)
        batch, task = _locate(state, task_id)
        moment = _now()
        if task.status in ("submitted", "merged", "abandoned"):
            log.warn("claim refused", task=task_id, holder=holder, status=task.status)
            raise refusal(
                "tasks.claim.closed",
                f"task {task_id} is {task.status} and cannot be claimed",
                task=task_id,
                status=task.status,
            )
        previous = task.lease
        if previous is not None and previous.expires_at > moment:
            log.info("claim refused: lease held", task=task_id, holder=holder, by=previous.holder)
            raise refusal(
                "tasks.lease.held",
                f"task {task_id} is held by {previous.holder} until {_iso(previous.expires_at)}",
                task=task_id,
                lease=_lease_view(task, moment),
            )

        base = task_dir(workspace, task_id)
        findings = inspect_task(workspace, batch, task, skip_output=previous is not None)
        if findings:
            log.warn(
                "claim refused: task directory disturbed",
                task=task_id,
                holder=holder,
                problems=[finding.as_dict() for finding in findings],
            )
            raise _zone_refusal(task_id, findings)
        manifest, reasons = read_manifest(workspace)
        reasons += pin_reasons(workspace, manifest, task)
        if reasons:
            # Work started now could never be submitted.
            log.warn("claim refused: pins are stale", task=task_id, holder=holder, reasons=reasons)
            raise refusal(
                reasons[0]["code"],
                f"task {task_id} can no longer be carried out: {reasons[0]['message']}",
                task=task_id,
                reasons=reasons,
            )

        if previous is None:
            inputs = scan(base / INPUT_DIR)
            if inputs.links or inputs.special:
                log.warn("claim refused: inputs are not plain files", task=task_id, holder=holder)
                raise refusal(
                    "tasks.input.not_plain",
                    "input/ may hold only plain files and directories, so that its content "
                    "can be pinned",
                    task=task_id,
                    paths=sorted([*inputs.links, *inputs.special]),
                )
            document = spec_document(batch.id, task)
            workspace.atomic_write(base / SPEC_FILE, document)
            _checkpoint("claim.spec_written")
            task.spec_sha256 = sha256_bytes(document)
            task.input_sha256 = inputs.digest
        else:
            # The previous holder may have left half a result behind, and may
            # even still be running. The new holder gets a new, empty directory:
            # a process still working inside the old one finds it gone.
            remove_entry(base / OUTPUT_DIR)
            (base / OUTPUT_DIR).mkdir()

        token = secrets.token_urlsafe(32)
        task.lease = Lease(
            holder=holder,
            token_sha256=_token_digest(token),
            acquired_at=moment,
            expires_at=moment + timedelta(seconds=ttl),
            generation=(previous.generation + 1) if previous else 1,
        )
        task.status = "claimed"
        save_state(workspace, state)

    if previous is None:
        log.info("lease taken", task=task_id, holder=holder, ttl_s=ttl, batch=batch.id)
    else:
        log.warn(
            "lease taken over",
            task=task_id,
            holder=holder,
            previous_holder=previous.holder,
            expired_at=_iso(previous.expires_at),
            generation=task.lease.generation,
        )
    return {
        "task": task_id,
        "batch": batch.id,
        "holder": holder,
        "token": token,
        "expires_at": _iso(task.lease.expires_at),
        "generation": task.lease.generation,
        "taken_over_from": previous.holder if previous else None,
        "paths": {
            "spec": f"tasks/{task_id}/{SPEC_FILE}",
            "input": f"tasks/{task_id}/{INPUT_DIR}",
            "output": f"tasks/{task_id}/{OUTPUT_DIR}",
            "result": f"tasks/{task_id}/{OUTPUT_DIR}/{RESULT_FILE}",
        },
    }


def renew(workspace: Workspace, task_id: str, holder: str, token: str, ttl: int) -> dict[str, Any]:
    """Extend a lease. Only the holder of the current token can, expired or not.

    An expired lease that nobody took over still has a single holder, so
    extending it cannot create a second writer.
    """
    require_task_id(task_id)
    require_holder(holder)
    _require_ttl(ttl)
    workspace.require()
    with workspace.exclusive():
        state = load_state(workspace)
        batch, task = _locate(state, task_id)
        moment = _now()
        if task.status not in ("claimed", "submitted"):
            raise refusal(
                "tasks.lease.none",
                f"task {task_id} is {task.status}; there is no lease to renew",
                task=task_id,
                status=task.status,
            )
        if not _token_matches(task, token):
            log.warn("renew refused: token is not the current one", task=task_id, holder=holder)
            raise refusal(
                "tasks.lease.token",
                f"the token is not the current lease token of task {task_id}; the lease "
                "was taken over, stop working on it",
                task=task_id,
                lease=_lease_view(task, moment),
            )
        task.lease.expires_at = moment + timedelta(seconds=ttl)
        task.lease.renewals += 1
        save_state(workspace, state)
    log.info("lease renewed", task=task_id, holder=task.lease.holder, ttl_s=ttl)
    return {
        "task": task_id,
        "batch": batch.id,
        "holder": task.lease.holder,
        "expires_at": _iso(task.lease.expires_at),
        "generation": task.lease.generation,
        "renewals": task.lease.renewals,
    }


# -- submit --------------------------------------------------------------------


def submit(workspace: Workspace, task_id: str, token: str) -> dict[str, Any]:
    """Hand a finished task over. Every check runs; all failures are reported together."""
    require_task_id(task_id)
    workspace.require()
    with workspace.exclusive():
        state = load_state(workspace)
        batch, task = _locate(state, task_id)
        moment = _now()
        if task.status in TERMINAL:
            raise refusal(
                "tasks.submit.closed",
                f"task {task_id} is already {task.status}",
                task=task_id,
                status=task.status,
            )
        reasons: list[dict[str, Any]] = []
        if task.lease is None:
            reasons.append(reason("tasks.lease.none", "the task has not been claimed"))
        elif not _token_matches(task, token):
            reasons.append(
                reason(
                    "tasks.lease.token",
                    "the token is not the current lease token; the lease was taken over, "
                    "stop working on this task",
                    lease=_lease_view(task, moment),
                )
            )
        elif task.lease.expires_at <= moment:
            reasons.append(
                reason(
                    "tasks.lease.expired",
                    "the lease has expired; renew it, then submit again",
                    lease=_lease_view(task, moment),
                )
            )

        output = Output(None, None, [])
        manifest, unreadable = read_manifest(workspace)
        if task.lease is not None:
            reasons += own_reasons(inspect_task(workspace, batch, task, skip_output=True))
            output = examine_output(workspace, task, sealed=False)
            reasons += output.reasons
            if not output.reasons and manifest is not None:
                reasons += apply(manifest, task, output.result)[1]
        reasons += ledger_reasons(workspace)
        reasons += unreadable + pin_reasons(workspace, manifest, task)
        reasons += zone_reasons(inspect_zone(workspace, batch, skip=frozenset({task_id})))

        if reasons:
            log.warn(
                "submit refused",
                task=task_id,
                batch=batch.id,
                holder=task.lease.holder if task.lease else None,
                codes=[entry["code"] for entry in reasons],
                reasons=reasons,
            )
            raise refusal(
                "tasks.submit.refused",
                f"task {task_id} was not accepted: "
                + ", ".join(entry["code"] for entry in reasons),
                task=task_id,
                reasons=reasons,
            )

        task.output_sha256 = output.digest
        task.status = "submitted"
        task.submitted_at = moment
        save_state(workspace, state)

    log.info(
        "task submitted",
        task=task_id,
        batch=batch.id,
        holder=task.lease.holder,
        output_sha256=task.output_sha256,
        patch_keys=sorted(output.result.manifest_patch),
        proposals=len(output.result.proposals),
    )
    return {
        "task": task_id,
        "batch": batch.id,
        "status": "submitted",
        "output_sha256": task.output_sha256,
        "patch_keys": sorted(output.result.manifest_patch),
        "proposals": len(output.result.proposals),
        "artifacts": sorted(output.result.artifacts),
    }


# -- merge ---------------------------------------------------------------------


def _close_if_done(state: TasksState, batch: Batch, moment: datetime) -> bool:
    """Mark the batch closed in memory when no task is left; the caller writes the state."""
    if not batch.done:
        return False
    batch.closed_at = moment
    state.open_batch = None
    return True


def _after_close(workspace: Workspace, batch: Batch) -> None:
    """Stop watching once the closed batch is on disk."""
    _checkpoint("close.state_written")
    workspace.drop_ledger()
    log.info(
        "batch closed",
        batch=batch.id,
        merged=sorted(t for t, task in batch.tasks.items() if task.status == "merged"),
        abandoned=sorted(t for t, task in batch.tasks.items() if task.status == "abandoned"),
    )


def _merge_one(
    workspace: Workspace,
    state: TasksState,
    batch: Batch,
    task: TaskRecord,
    elsewhere: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check one submitted task again and, if everything holds, apply it. Returns refusals."""
    task_id = task.spec.id
    manifest, reasons = read_manifest(workspace)
    reasons = ledger_reasons(workspace) + reasons + pin_reasons(workspace, manifest, task)
    reasons += own_reasons(inspect_task(workspace, batch, task, skip_output=True))
    output = examine_output(workspace, task, sealed=True)
    reasons += output.reasons + elsewhere
    merged: Manifest | None = None
    if not reasons:
        merged, reasons = apply(manifest, task, output.result)
    if reasons:
        log.warn(
            "merge refused",
            task=task_id,
            batch=batch.id,
            codes=[entry["code"] for entry in reasons],
            reasons=reasons,
        )
        return reasons

    workspace.save_manifest(merged)
    _checkpoint("merge.manifest_written")
    moment = _now()
    task.status = "merged"
    task.merged_at = moment
    task.patch_keys = sorted(output.result.manifest_patch)
    task.proposals = [proposal.model_dump() for proposal in output.result.proposals]
    task.notes = output.result.notes
    closed = _close_if_done(state, batch, moment)
    save_state(workspace, state)
    log.info(
        "merge applied",
        task=task_id,
        batch=batch.id,
        patch_keys=task.patch_keys,
        proposals=len(task.proposals),
        manifest_hash=manifest_hash(merged),
    )
    if closed:
        _after_close(workspace, batch)
    return []


def merge(
    workspace: Workspace, task_id: str | None = None, everything: bool = False
) -> dict[str, Any]:
    """Apply submitted results to the manifest, one task at a time in id order."""
    if (task_id is None) == (not everything):
        raise TaskError("name one task or pass --all, not both", code="tasks.usage")
    if task_id is not None:
        require_task_id(task_id)
    workspace.require()
    with workspace.exclusive():
        state = load_state(workspace)
        batch = state.current()
        if batch is None:
            discard_stale_ledger(workspace)

        already: list[str] = []
        targets: list[TaskRecord] = []
        if task_id is not None:
            found = state.latest(task_id)
            if found is None:
                raise TaskError(
                    f"task {task_id} has never been opened",
                    code="tasks.task.unknown",
                    detail={"task": task_id},
                )
            record = found[1]
            if record.status == "merged":
                already.append(task_id)
            elif record.status == "submitted" and batch is not None and found[0].id == batch.id:
                targets.append(record)
            else:
                raise refusal(
                    "tasks.merge.not_submitted",
                    f"task {task_id} is {record.status}; only a submitted task can be merged",
                    task=task_id,
                    status=record.status,
                )
        elif batch is not None:
            targets = [
                task for _, task in sorted(batch.tasks.items()) if task.status == "submitted"
            ]

        merged: list[str] = []
        refused: list[dict[str, Any]] = []
        if targets:
            # One sweep over everything under tasks/ that is not about to be
            # examined task by task.
            others = inspect_zone(
                workspace, batch, skip=frozenset(task.spec.id for task in targets)
            )
            elsewhere = zone_reasons(others)
            for task in targets:
                reasons = _merge_one(workspace, state, batch, task, elsewhere)
                if reasons:
                    refused.append({"task": task.spec.id, "reasons": reasons})
                else:
                    merged.append(task.spec.id)

    outcome = {
        "batch": batch.id if batch else None,
        "merged": merged,
        "already_merged": already,
        "refused": refused,
        "batch_closed": batch is not None and batch.closed_at is not None,
    }
    if refused:
        raise refusal(
            "tasks.merge.refused",
            "not merged: "
            + "; ".join(
                f"{entry['task']} ({', '.join(r['code'] for r in entry['reasons'])})"
                for entry in refused
            ),
            **outcome,
        )
    return outcome


# -- abort ---------------------------------------------------------------------


def abort(
    workspace: Workspace, task_id: str | None = None, whole_batch: bool = False
) -> dict[str, Any]:
    """Give up one task, or every unfinished task of the open batch.

    This is the way out of a batch that can no longer complete, so it never
    refuses because of what the checks would find; it reports it instead.
    """
    if (task_id is None) == (not whole_batch):
        raise TaskError("name one task or pass --batch, not both", code="tasks.usage")
    if task_id is not None:
        require_task_id(task_id)
    workspace.require()
    with workspace.exclusive():
        state = load_state(workspace)
        batch = state.current()
        if batch is None:
            discard_stale_ledger(workspace)
            if task_id is not None:
                found = state.latest(task_id)
                if found is None:
                    raise TaskError(
                        f"task {task_id} has never been opened",
                        code="tasks.task.unknown",
                        detail={"task": task_id},
                    )
                if found[1].status == "merged":
                    raise refusal(
                        "tasks.abort.merged",
                        f"task {task_id} is merged; its result is already in the manifest",
                        task=task_id,
                    )
            return {
                "batch": None,
                "abandoned": [],
                "batch_closed": False,
                "findings": [],
                "state_file_changed": False,
            }

        if task_id is not None:
            _, record = _locate(state, task_id)
            if record.status == "merged":
                raise refusal(
                    "tasks.abort.merged",
                    f"task {task_id} is merged; its result is already in the manifest",
                    task=task_id,
                )
            targets = [] if record.status == "abandoned" else [record]
        else:
            targets = [task for task in batch.tasks.values() if task.status not in TERMINAL]

        # Once the batch is closed the ledger is gone, so this is the last
        # moment at which what happened to the shared files can be told.
        findings = ledger_reasons(workspace)
        state_file_changed = any(
            STATE_KEY in entry["detail"].get("paths", []) for entry in findings
        )
        if findings:
            log.warn("batch is being aborted with shared files disturbed", findings=findings)
        if state_file_changed:
            log.error(
                "task state was changed outside the toolkit; the statuses recorded in it "
                "cannot be relied on",
                batch=batch.id,
            )
        moment = _now()
        for task in targets:
            task.status = "abandoned"
            task.abandoned_at = moment
            log.info(
                "task abandoned",
                task=task.spec.id,
                batch=batch.id,
                holder=task.lease.holder if task.lease else None,
            )
        closed = _close_if_done(state, batch, moment)
        if targets or closed:
            save_state(workspace, state)
        if closed:
            _after_close(workspace, batch)

    return {
        "batch": batch.id,
        "abandoned": sorted(task.spec.id for task in targets),
        "batch_closed": closed,
        "findings": findings,
        "state_file_changed": state_file_changed,
    }


# -- status --------------------------------------------------------------------


def status(workspace: Workspace, verify: bool = False) -> dict[str, Any]:
    """Where every task of the open batch stands, and the proposals stored so far.

    With `verify` it also runs the checks a submit or merge would run on shared
    state, under the lock so that no sanctioned write is seen half-done.
    """
    workspace.require()
    state = load_state(workspace)
    batch = state.current()
    moment = _now()
    report: dict[str, Any] = {
        "open_batch": None,
        "batches": len(state.batches),
        "merged": sorted(state.merged_ids()),
        "proposals": [
            {"task": task_id, "batch": entry.id, **proposal}
            for entry in state.batches
            for task_id, task in sorted(entry.tasks.items())
            for proposal in task.proposals
        ],
    }
    if batch is not None:
        report["open_batch"] = {
            "id": batch.id,
            "stage": batch.stage,
            "opened_at": _iso(batch.opened_at),
            "tasks": [
                {
                    "id": task_id,
                    "status": task.status,
                    "lease": _lease_view(task, moment),
                    "submitted_at": _iso(task.submitted_at),
                    "merged_at": _iso(task.merged_at),
                    "store_version": task.pins.store_version,
                }
                for task_id, task in sorted(batch.tasks.items())
            ],
        }
    if verify:
        with workspace.exclusive():
            state = load_state(workspace)
            batch = state.current()
            problems: list[dict[str, Any]] = []
            if batch is not None:
                problems += ledger_reasons(workspace)
                problems += zone_reasons(inspect_zone(workspace, batch, skip=frozenset()))
                manifest, unreadable = read_manifest(workspace)
                problems += unreadable
                for task_id, task in sorted(batch.tasks.items()):
                    if task.status in TERMINAL:
                        continue
                    for entry in pin_reasons(workspace, manifest, task):
                        problems.append({**entry, "detail": {**entry["detail"], "task": task_id}})
        report["verified"] = {"clean": not problems, "problems": problems}
    return report
