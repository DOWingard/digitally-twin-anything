"""The checks that stand between a task and shared state.

Two families guard what tasks must not touch:

- outside `tasks/`: the ledger of sanctioned writes. Any watched file that
  differs from it was changed by something other than the toolkit.
- inside `tasks/`: the zone rules. Each task directory, and everything else
  that was there when the batch opened, is either free to change or fixed to a
  recorded hash, depending on how far the task has come.

A third family guards what a task may rely on: its pins, taken when the batch
opened. And the last looks at what the task hands over: its output directory,
its result file and the patch in it.

Every check returns the reasons it failed, each with a code of its own, and
changes nothing. The callers hold the workspace lock.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Any

from dta_core.errors import ManifestError, WorkspaceError
from dta_core.log import Log
from dta_core.manifest import Manifest, subtree_hash, to_plain

from ..workspace import Workspace, sha256_file
from .batches import plan_batches
from .catalogue import TaskSpec, stages, tasks_for
from .errors import TaskError, reason, refusal
from .paths import owned_paths, pinned_paths
from .result import (
    MAX_RESULT_BYTES,
    Result,
    apply_patch,
    artifact_reasons,
    ownership_reasons,
    parse_result,
    side_effect_reasons,
    stability_reasons,
)
from .state import OntologyPin, TaskRecord
from .tree import Scan, is_real_dir, read_plain_file, scan, sha256_bytes, spell_name
from .zone import OUTPUT_DIR, RESULT_FILE, Finding, task_dir

log = Log("tasks.checks")


# -- pins ---------------------------------------------------------------------


def ontology_pin(workspace: Workspace, manifest: Manifest) -> OntologyPin:
    """Where the manifest says the ontology file is, and the hash of its content."""
    path = manifest.ontology.path if manifest.ontology else None
    if not path:
        return OntologyPin()
    target = (workspace.root / path).resolve()
    if workspace.root not in target.parents:
        log.error("ontology path leaves the workspace", path=path)
        raise TaskError(
            f"ontology.path {path!r} points outside the workspace",
            code="tasks.ontology.outside",
            detail={"path": path},
        )
    try:
        digest = sha256_file(target) if target.is_file() else None
    except OSError as exc:
        # Gone, or made unreadable, between being found and being read. Either
        # way it is not the file that was pinned.
        log.warn("ontology file cannot be read", exc=exc, path=path)
        digest = None
    return OntologyPin(path=path, sha256=digest)


def manifest_pin(manifest: Manifest, spec: TaskSpec) -> str:
    """Hash of the manifest values a task reads, leaving out the keys it owns itself."""
    return subtree_hash(manifest, pinned_paths(to_plain(manifest), spec.reads, spec.owns))


def owned_pin(manifest: Manifest, spec: TaskSpec) -> str:
    """Hash of what the manifest holds under the keys a task owns.

    While the task is out nobody else may write there. The merge is the one
    writer, and it will not overwrite a value that is no longer the one the
    task started from.
    """
    return subtree_hash(manifest, owned_paths(to_plain(manifest), spec.owns))


def load_manifest(workspace: Workspace) -> Manifest:
    """Read the manifest, having made sure that reading it cannot block or wander off.

    A pipe in its place would hold the reader, and with it the workspace lock,
    for ever. A link would have the file that is read differ from the file
    that is watched.
    """
    try:
        mode = os.lstat(workspace.manifest_path).st_mode
    except OSError:
        mode = None  # missing or out of reach: the loader says so in its own words
    if mode is not None and not stat.S_ISREG(mode):
        log.error("manifest is not a regular file", path=str(workspace.manifest_path))
        raise TaskError(
            "manifest.yaml is not a regular file",
            code="tasks.manifest.not_plain",
            detail={"path": str(workspace.manifest_path)},
        )
    return workspace.load_manifest()


def read_manifest(workspace: Workspace) -> tuple[Manifest | None, list[dict[str, Any]]]:
    try:
        return load_manifest(workspace), []
    except (ManifestError, TaskError) as exc:
        problem = exc.message
    except (UnicodeError, RecursionError) as exc:
        problem = type(exc).__name__
    log.warn("manifest cannot be read", problem=problem)
    return None, [reason("tasks.manifest.unreadable", f"the manifest cannot be read: {problem}")]


def pin_reasons(
    workspace: Workspace, manifest: Manifest | None, task: TaskRecord
) -> list[dict[str, Any]]:
    """Pins that no longer hold. The store version is recorded but has nothing to be compared to."""
    if manifest is None:
        return []
    reasons = []
    try:
        current = manifest_pin(manifest, task.spec)
    except TaskError as exc:
        current = None
        reasons.append(reason("tasks.pin.manifest", exc.message))
    if current is not None and current != task.pins.manifest_reads:
        reasons.append(
            reason(
                "tasks.pin.manifest",
                "manifest values this task reads have changed since its batch opened",
                reads=list(task.spec.reads),
            )
        )
    if owned_pin(manifest, task.spec) != task.pins.manifest_owned:
        reasons.append(
            reason(
                "tasks.pin.owned",
                "manifest keys this task owns were written since its batch opened",
                owned_keys=list(task.spec.owns),
            )
        )
    try:
        ontology = ontology_pin(workspace, manifest)
    except TaskError as exc:
        reasons.append(reason("tasks.pin.ontology", exc.message))
    else:
        if ontology != task.pins.ontology:
            reasons.append(
                reason(
                    "tasks.pin.ontology",
                    "the ontology file has changed since the batch opened",
                    pinned=task.pins.ontology.model_dump(),
                    current=ontology.model_dump(),
                )
            )
    return reasons


# -- shared files outside tasks/ ----------------------------------------------


def unsettled(exc: OSError) -> TaskError:
    """The error for watched files that changed under the very comparison looking at them.

    Hashing every watched file takes time, and a file can vanish between being
    listed and being read. Nothing can be concluded from such a pass, and
    nothing has been written; the command is simply to be run again.
    """
    log.warn("watched files changed while they were being read", exc=exc)
    return TaskError(
        "shared files changed while they were being read; run the command again",
        code="tasks.files.unsettled",
        detail={"problem": str(exc)},
    )


def _foreign_changes(workspace: Workspace) -> list[dict[str, str]]:
    try:
        return workspace.foreign_changes()
    except OSError as exc:
        raise unsettled(exc) from exc


def ledger_reasons(workspace: Workspace) -> list[dict[str, Any]]:
    """Changes to watched files that did not come through the toolkit, one reason per kind."""
    if not workspace.ledger_path.is_file():
        return [
            reason(
                "tasks.ledger.missing",
                "the ledger of sanctioned writes is gone while a batch is open, so changes "
                "to shared files can no longer be told apart; abort the batch",
            )
        ]
    try:
        changes = _foreign_changes(workspace)
    except WorkspaceError as exc:
        return [reason("tasks.ledger.unreadable", exc.message)]
    wording = {
        "modified": "shared files were modified outside the toolkit",
        "added": "files appeared outside the task directories",
        "removed": "shared files were removed outside the toolkit",
    }
    reasons = []
    for change, message in wording.items():
        paths = [entry["path"] for entry in changes if entry["change"] == change]
        if paths:
            reasons.append(reason(f"tasks.foreign.{change}", message, paths=paths))
    return reasons


def state_key(workspace: Workspace) -> str:
    """The name under which the ledger knows the task state file."""
    return str(workspace.tasks_state_path.relative_to(workspace.root))


def state_file_changed(workspace: Workspace) -> bool:
    """True when the task state file itself differs from the ledger."""
    key = state_key(workspace)
    return any(entry["path"] == key for entry in _foreign_changes(workspace))


def touches_state(workspace: Workspace, reasons: list[dict[str, Any]]) -> bool:
    """True when the task state file is among the files these reasons name."""
    key = state_key(workspace)
    return any(key in entry["detail"].get("paths", []) for entry in reasons)


def rewrite_reasons(workspace: Workspace) -> list[dict[str, Any]]:
    """Why the task state may not be rewritten now, for commands that check nothing else.

    A command that loads the state file and writes it back enters the result
    in the ledger. If the file it loaded was put there by someone else, that
    someone's edit would from then on pass for a sanctioned write.
    """
    reasons = ledger_reasons(workspace)
    if touches_state(workspace, reasons):
        log.error("task state changed outside the toolkit", path=state_key(workspace))
        reasons.insert(
            0,
            reason(
                "tasks.state.tampered",
                "the task state file was changed outside the toolkit; what it says about "
                "tasks and leases cannot be relied on. Abort the batch",
                path=state_key(workspace),
            ),
        )
    return reasons


def discard_stale_ledger(workspace: Workspace) -> None:
    """Remove a ledger left behind after its batch ended. The caller holds the lock.

    A ledger outlives its batch only if a process stopped between closing the
    batch and dropping the ledger, or between rebasing and opening. In both
    cases the state file still matches it. If it does not, the state file was
    replaced or removed behind the toolkit's back, and what it now says about
    batches cannot be relied on.
    """
    if not workspace.ledger_path.is_file():
        return
    try:
        changed = state_file_changed(workspace)
    except WorkspaceError as exc:
        log.error("a ledger left behind cannot be read", exc=exc)
        raise refusal(
            "tasks.ledger.unreadable",
            "a ledger of sanctioned writes was left behind and cannot be read, so the task "
            "state file cannot be checked against it; inspect .dta/tasks.json, then delete "
            ".dta/ledger.json to accept it as it is",
        ) from exc
    if changed:
        log.error("task state changed outside the toolkit", path=state_key(workspace))
        raise refusal(
            "tasks.state.tampered",
            "the task state file was changed or removed outside the toolkit while a batch "
            "was being tracked; inspect .dta/tasks.json, then delete .dta/ledger.json to "
            "accept it as it is",
            path=state_key(workspace),
        )
    workspace.drop_ledger()
    log.warn("ledger left behind by an interrupted command was discarded")


# -- task directories ---------------------------------------------------------


def own_reasons(findings: list[Finding]) -> list[dict[str, Any]]:
    """Findings about the task's own directory, each under the code of the part concerned."""
    wording = {
        "spec": ("tasks.spec.changed", "task.json is not what was written at claim"),
        "input": ("tasks.input.changed", "the input directory changed after the task was claimed"),
        "output": ("tasks.output.changed", "the output directory changed after it was submitted"),
        "zone": (
            "tasks.zone.violation",
            "the task directory holds something other than task.json, input/ and output/",
        ),
    }
    reasons = []
    for part, (code, message) in wording.items():
        problems = [finding.as_dict() for finding in findings if finding.part == part]
        if problems:
            reasons.append(reason(code, message, problems=problems))
    return reasons


def sealed_reasons(findings: list[Finding]) -> list[dict[str, Any]]:
    """Findings about results that were merged in batches since closed."""
    if not findings:
        return []
    return [
        reason(
            "tasks.results.changed",
            "results merged in earlier batches are no longer what was merged; restore "
            "them, or delete a task's whole directory to retire its result",
            problems=[finding.as_dict() for finding in findings],
        )
    ]


def zone_reasons(findings: list[Finding]) -> list[dict[str, Any]]:
    """Findings about the rest of `tasks/`: other tasks' directories and anything stray."""
    if not findings:
        return []
    return [
        reason(
            "tasks.zone.violation",
            "something under tasks/ changed that no task was allowed to change",
            problems=[finding.as_dict() for finding in findings],
        )
    ]


@dataclass
class Output:
    digest: str | None
    result: Result | None
    reasons: list[dict[str, Any]]


def examine_output(workspace: Workspace, task: TaskRecord, *, sealed: bool) -> Output:
    """Check a task's output directory and read its result.

    The result file is read once and its bytes are tied to the hash the walk
    computed, so what is parsed is exactly what the tree digest covers. With
    `sealed`, the tree must still be the one that was submitted.
    """
    base = task_dir(workspace, task.spec.id)
    out_dir = base / OUTPUT_DIR
    where = f"tasks/{task.spec.id}/{OUTPUT_DIR}"
    # A link anywhere on the way would have the walk read some other directory.
    reachable = is_real_dir(workspace.tasks_dir) and is_real_dir(base)
    raw = read_plain_file(out_dir / RESULT_FILE, limit=MAX_RESULT_BYTES) if reachable else None
    tree = scan(out_dir) if reachable else Scan(exists=False)
    if not tree.exists:
        return Output(
            None, None, [reason("tasks.output.missing", f"{where} is missing or not a directory")]
        )
    reasons = []
    if tree.links:
        reasons.append(
            reason(
                "tasks.output.symlink",
                "the output directory may not contain links",
                links=[
                    {"path": path, "target": spell_name(target)}
                    for path, target in tree.links.items()
                ],
            )
        )
    if tree.special:
        reasons.append(
            reason(
                "tasks.output.special",
                "the output directory may hold only plain files and directories",
                paths=tree.special,
            )
        )
    if tree.shared:
        reasons.append(
            reason(
                "tasks.output.hardlink",
                "files in the output directory may not have a second name elsewhere",
                paths=tree.shared,
            )
        )
    if sealed and tree.digest != task.output_sha256:
        reasons.append(
            reason(
                "tasks.output.changed",
                "the output directory is not what was submitted; submit it again",
                path=where,
            )
        )
        return Output(tree.digest, None, reasons)
    if raw is None:
        reasons.append(reason("tasks.result.missing", f"{where}/{RESULT_FILE} is not a file"))
        return Output(tree.digest, None, reasons)
    if len(raw) > MAX_RESULT_BYTES:
        reasons.append(
            reason(
                "tasks.result.invalid",
                f"{RESULT_FILE} is larger than {MAX_RESULT_BYTES} bytes; put bulk into "
                "other files of the output directory",
                limit=MAX_RESULT_BYTES,
            )
        )
        return Output(tree.digest, None, reasons)
    if tree.files.get(RESULT_FILE) != sha256_bytes(raw):
        reasons.append(
            reason(
                "tasks.output.unstable",
                "the output directory changed while it was being checked; finish writing, "
                "then try again",
                path=where,
            )
        )
        return Output(tree.digest, None, reasons)

    result, problems = parse_result(raw)
    reasons += problems
    if result is not None:
        reasons += ownership_reasons(task.spec, result.manifest_patch)
        reasons += artifact_reasons(result.artifacts, set(tree.files) - {RESULT_FILE})
    return Output(tree.digest, result, reasons)


def apply(
    manifest: Manifest, task: TaskRecord, result: Result
) -> tuple[Manifest | None, list[dict[str, Any]]]:
    """The manifest with the task's patch applied, or why it may not be applied.

    The patched manifest has to match the schema, may differ from the current
    one only inside the keys the task owns, has to survive being written and
    read back, and must leave the work of every stage plannable.
    """
    try:
        patched = apply_patch(manifest, result.manifest_patch)
    except ManifestError as exc:
        return None, [
            reason(
                "tasks.patch.schema",
                "with this patch applied the manifest no longer matches its schema",
                problems=exc.detail.get("problems", []),
            )
        ]
    reasons = side_effect_reasons(task.spec, manifest, patched)
    reasons += stability_reasons(task.spec, manifest, patched, result.manifest_patch)
    broken = _unplannable(patched)
    if broken:
        already = _unplannable(manifest)
        broken = {stage: why for stage, why in broken.items() if stage not in already}
    if broken:
        reasons.append(
            reason(
                "tasks.patch.unplannable",
                "with this patch applied the tasks of a stage could no longer be worked out",
                stages=broken,
            )
        )
    return (None, reasons) if reasons else (patched, [])


def _unplannable(manifest: Manifest) -> dict[str, str]:
    """Stages whose tasks, batches or pins cannot be worked out from a manifest, and why.

    A value a task may legitimately set can still be one the planner cannot
    use, such as a new entry whose id cannot appear in a task id. Accepting it
    would leave every later batch of that stage impossible to open.
    """
    broken = {}
    for stage in stages():
        try:
            specs = tasks_for(manifest, stage)
            plan_batches(specs)
            for spec in specs:
                manifest_pin(manifest, spec)
                owned_pin(manifest, spec)
        except TaskError as exc:
            broken[stage] = exc.message
    return broken
