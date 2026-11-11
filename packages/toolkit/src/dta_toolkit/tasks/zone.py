"""What may exist under `tasks/` while a batch is open, and what has to stay as it was.

The ledger of sanctioned writes covers every shared file outside `tasks/`. This
module is its counterpart inside: each task directory holds exactly `task.json`,
`input/` and `output/`, and depending on how far the task has come, each of
those is either still free to change or fixed to a recorded hash.

    open        input/ free (the orchestrator fills it), output/ empty
    claimed     task.json and input/ fixed, output/ free (the holder writes it)
    submitted   all three fixed
    merged      all three fixed
    abandoned   not looked at

Everything else under `tasks/` (directories of earlier batches, anything the
orchestrator keeps there) is fixed to what it was when the batch opened, and
nothing new may appear.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..workspace import Workspace
from .catalogue import require_task_id
from .errors import TaskError
from .state import Batch, TaskRecord, TasksState
from .tree import entry_digest, is_real_dir, read_plain_file, scan, sha256_bytes, spell_name

SPEC_FILE = "task.json"
INPUT_DIR = "input"
OUTPUT_DIR = "output"
RESULT_FILE = "result.json"

RESULT_FORMAT = {
    "manifest_patch": (
        "object mapping a dotted manifest path to the value to set there (null removes it); "
        "every path must lie inside writes.owned_keys"
    ),
    "proposals": (
        "list of objects with 'target' and 'description' for anything wanted outside "
        "writes.owned_keys; kept for the orchestrator, never applied"
    ),
    "artifacts": "list of every other file under writes.zone, as paths relative to it",
    "notes": "free text",
}


@dataclass(frozen=True)
class Finding:
    # Which part of a task directory is concerned: its fixed description, its
    # inputs, its output, or the layout of the zone as a whole.
    part: Literal["spec", "input", "output", "zone"]
    path: str  # relative to the workspace
    problem: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "problem": self.problem}


def task_dir(workspace: Workspace, task_id: str) -> Path:
    """Directory of one task. The id is checked so the result is always a child of `tasks/`."""
    path = workspace.tasks_dir / require_task_id(task_id)
    if path.parent != workspace.tasks_dir:
        raise TaskError("task directory escapes tasks/", code="tasks.id.invalid")
    return path


def spec_document(batch_id: str, task: TaskRecord) -> bytes:
    """Content of `task.json`: everything the holder needs to know, and nothing that varies.

    It carries no holder and no time, so every claim of the same task in the same
    batch produces the same bytes.
    """
    spec = task.spec
    base = f"tasks/{spec.id}"
    document = {
        "version": 1,
        "id": spec.id,
        "kind": spec.kind,
        "stage": spec.stage,
        "batch": batch_id,
        "subject": spec.subject,
        "needs": list(spec.needs),
        "reads": {
            "manifest": list(spec.reads),
            "ontology_file": task.pins.ontology.path,
            "input": f"{base}/{INPUT_DIR}",
        },
        "writes": {
            "zone": f"{base}/{OUTPUT_DIR}",
            "result": f"{base}/{OUTPUT_DIR}/{RESULT_FILE}",
            "owned_keys": list(spec.owns),
        },
        "pins": task.pins.model_dump(mode="json"),
        "result_format": RESULT_FORMAT,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _is_spec_leftover(name: str) -> bool:
    """True for the temporary file an interrupted write of `task.json` leaves behind."""
    return name.startswith(f".{SPEC_FILE}.") and name.endswith(".tmp")


def spec_leftovers(base: Path) -> list[Path]:
    return [base / name for name in sorted(os.listdir(base)) if _is_spec_leftover(name)]


def inspect_task(
    workspace: Workspace, batch: Batch, task: TaskRecord, *, skip_output: bool = False
) -> list[Finding]:
    """Everything about one task's directory that is not as its status requires.

    With `skip_output` the output directory is left to the caller, which is about
    to examine it in detail anyway.
    """
    if task.status == "abandoned":
        return []
    base = task_dir(workspace, task.spec.id)
    where = f"tasks/{task.spec.id}"
    # Nothing is read or written through a link, at any level.
    if not is_real_dir(workspace.tasks_dir):
        return [Finding("zone", "tasks", "missing or not a directory")]
    if not is_real_dir(base):
        return [Finding("zone", where, "missing or not a directory")]

    findings = []
    names = sorted(os.listdir(base))
    for name in names:
        # Only a claim that died while writing the description leaves such a
        # file, and the claim that follows removes it before recording a lease.
        tolerated = task.status == "open" and _is_spec_leftover(name)
        if name not in (SPEC_FILE, INPUT_DIR, OUTPUT_DIR) and not tolerated:
            findings.append(
                Finding("zone", f"{where}/{spell_name(name)}", "not part of a task directory")
            )
    if not is_real_dir(base / INPUT_DIR):
        findings.append(Finding("input", f"{where}/{INPUT_DIR}", "missing or not a directory"))
    if not skip_output and not is_real_dir(base / OUTPUT_DIR):
        findings.append(Finding("output", f"{where}/{OUTPUT_DIR}", "missing or not a directory"))

    if task.status == "open":
        # A claim that stopped half-way leaves the description behind; it is
        # acceptable only if it is exactly what the next claim will write.
        expected = spec_document(batch.id, task)
        if SPEC_FILE in names and read_plain_file(base / SPEC_FILE) != expected:
            findings.append(
                Finding("spec", f"{where}/{SPEC_FILE}", "present before the task was claimed")
            )
        if not skip_output and not scan(base / OUTPUT_DIR).empty:
            findings.append(
                Finding("output", f"{where}/{OUTPUT_DIR}", "not empty before the task was claimed")
            )
        return findings

    spec_bytes = read_plain_file(base / SPEC_FILE)
    if spec_bytes is None or sha256_bytes(spec_bytes) != task.spec_sha256:
        findings.append(
            Finding("spec", f"{where}/{SPEC_FILE}", "changed since the task was claimed")
        )
    tree = scan(base / INPUT_DIR)
    if tree.exists and tree.digest != task.input_sha256:
        findings.append(
            Finding("input", f"{where}/{INPUT_DIR}", "changed since the task was claimed")
        )
    if not skip_output and task.status in ("submitted", "merged"):
        tree = scan(base / OUTPUT_DIR)
        if tree.exists and tree.digest != task.output_sha256:
            findings.append(
                Finding("output", f"{where}/{OUTPUT_DIR}", "changed since it was submitted")
            )
    return findings


def snapshot(workspace: Workspace, exclude: set[str]) -> dict[str, str]:
    """Digest of every entry directly under `tasks/` except the named ones."""
    if not is_real_dir(workspace.tasks_dir):
        return {}
    return {
        spell_name(name): entry_digest(workspace.tasks_dir / name)
        for name in sorted(os.listdir(workspace.tasks_dir))
        if name not in exclude
    }


def inspect_zone(workspace: Workspace, batch: Batch, *, skip: frozenset[str]) -> list[Finding]:
    """Everything under `tasks/` that is not as the open batch requires.

    Tasks named in `skip` are left to the caller.
    """
    if not is_real_dir(workspace.tasks_dir):
        return [Finding("zone", "tasks", "missing or not a directory")]
    findings = []
    # Keyed the way the baseline is, so a name that is not text still matches its record.
    present = {spell_name(name): name for name in os.listdir(workspace.tasks_dir)}
    for task_id, task in sorted(batch.tasks.items()):
        if task_id not in skip:
            findings.extend(inspect_task(workspace, batch, task))
    for key in sorted(set(present) - set(batch.tasks)):
        if key not in batch.baseline:
            findings.append(Finding("zone", f"tasks/{key}", "appeared after the batch opened"))
        elif entry_digest(workspace.tasks_dir / present[key]) != batch.baseline[key]:
            findings.append(Finding("zone", f"tasks/{key}", "changed since the batch opened"))
    for key in sorted(set(batch.baseline) - set(present)):
        findings.append(Finding("zone", f"tasks/{key}", "removed since the batch opened"))
    return findings


def sealed_findings(workspace: Workspace, state: TasksState) -> list[Finding]:
    """Merged results of closed batches that are no longer what was merged.

    While a batch is open its baseline holds them fixed. Between batches
    nothing does, so they are looked at again before the next batch is opened
    and takes them into its baseline as they are. A directory that is gone
    altogether is not reported: removing it whole is how a result is retired.
    """
    if not is_real_dir(workspace.tasks_dir):
        return []
    findings = []
    seen: set[str] = set()
    for batch in reversed(state.batches):
        for task_id, task in sorted(batch.tasks.items()):
            if task_id in seen:
                continue
            seen.add(task_id)  # only the latest attempt at a task owns its directory
            if task.status != "merged" or batch.closed_at is None:
                continue
            if os.path.lexists(task_dir(workspace, task_id)):
                findings.extend(inspect_task(workspace, batch, task))
    return findings
