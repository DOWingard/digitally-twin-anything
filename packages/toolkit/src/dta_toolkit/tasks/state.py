"""The record of batches, tasks and leases. Lives in `.dta/tasks.json`.

Only read for a decision, and only written, while holding the workspace lock.
The file is one of the watched shared files, so an edit that did not come
through the toolkit shows up as a foreign change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from dta_core.log import Log
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..workspace import Workspace
from .catalogue import TaskSpec
from .durable import write_recorded
from .errors import TaskError

log = Log("tasks.state")

TaskStatus = Literal["open", "claimed", "submitted", "merged", "abandoned"]
# A task in one of these states takes no further part in its batch.
TERMINAL = frozenset({"merged", "abandoned"})


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OntologyPin(_Record):
    path: str | None = None  # as written in the manifest, relative to the workspace
    sha256: str | None = None  # None while the file does not exist


class Pins(_Record):
    """What a task's result is valid against. Taken when its batch opens."""

    manifest_reads: str  # hash of the manifest values the task reads, own keys cut out
    manifest_owned: str  # hash of the values under the keys the task owns
    ontology: OntologyPin
    store_version: str | None = None


class Lease(_Record):
    holder: str
    # The token itself is known only to the holder; the state file is readable by every agent.
    token_sha256: str
    acquired_at: datetime
    expires_at: datetime
    # Counts claims of this task: 1 for the first holder, +1 at each takeover.
    generation: int = 1
    renewals: int = 0


class TaskRecord(_Record):
    spec: TaskSpec
    status: TaskStatus = "open"
    pins: Pins
    lease: Lease | None = None
    spec_sha256: str | None = None  # of task.json as written at the first claim
    input_sha256: str | None = None  # of the input tree at the first claim
    output_sha256: str | None = None  # of the output tree as submitted
    submitted_at: datetime | None = None
    merged_at: datetime | None = None
    abandoned_at: datetime | None = None
    patch_keys: list[str] = Field(default_factory=list)  # manifest keys its merge set
    proposals: list[dict[str, Any]] = Field(default_factory=list)  # kept, never applied
    notes: str = ""


class Batch(_Record):
    id: str
    stage: str
    opened_at: datetime
    closed_at: datetime | None = None
    # Everything else that was under `tasks/` when the batch opened, by name.
    # None of it may change while the batch is open.
    baseline: dict[str, str] = Field(default_factory=dict)
    tasks: dict[str, TaskRecord]

    @property
    def done(self) -> bool:
        return all(task.status in TERMINAL for task in self.tasks.values())


class TasksState(_Record):
    version: Literal[1] = 1
    open_batch: str | None = None
    batches: list[Batch] = Field(default_factory=list)

    def current(self) -> Batch | None:
        """The open batch, if there is one."""
        if self.open_batch is None:
            return None
        for batch in self.batches:
            if batch.id == self.open_batch:
                return batch
        raise TaskError(
            f"task state names an open batch {self.open_batch!r} that it does not contain",
            code="tasks.state.damaged",
        )

    def merged_ids(self) -> set[str]:
        return {
            task_id
            for batch in self.batches
            for task_id, task in batch.tasks.items()
            if task.status == "merged"
        }

    def latest(self, task_id: str) -> tuple[Batch, TaskRecord] | None:
        """The most recent attempt at a task, in any batch."""
        for batch in reversed(self.batches):
            if task_id in batch.tasks:
                return batch, batch.tasks[task_id]
        return None


def load_state(workspace: Workspace) -> TasksState:
    path = workspace.tasks_state_path
    if not path.is_file():
        return TasksState()
    try:
        return TasksState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        log.error("task state cannot be read", exc=exc, path=str(path))
        raise TaskError(
            "task state is unreadable or damaged",
            code="tasks.state.damaged",
            detail={"path": str(path)},
        ) from exc


def save_state(workspace: Workspace, state: TasksState) -> None:
    """Replace the state file. The caller must hold the workspace lock."""
    write_recorded(workspace, workspace.tasks_state_path, state.model_dump_json(indent=2) + "\n")
