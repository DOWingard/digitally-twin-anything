"""A stage's tasks, the batches they run in, and which batch comes next."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dta_core.manifest import Manifest

from .batches import plan_batches
from .catalogue import TaskSpec, tasks_for
from .state import TasksState
from .zone import INPUT_DIR, OUTPUT_DIR


@dataclass
class Plan:
    stage: str
    specs: dict[str, TaskSpec]
    batches: list[list[str]]
    merged: set[str]
    ontology_file: str | None = None

    @property
    def next_index(self) -> int | None:
        """The first batch that still has unmerged tasks."""
        for index, batch in enumerate(self.batches):
            if any(task_id not in self.merged for task_id in batch):
                return index
        return None

    def to_open(self) -> list[TaskSpec]:
        """Unmerged tasks of the next batch whose needs have all been merged."""
        index = self.next_index
        if index is None:
            return []
        return [
            self.specs[task_id]
            for task_id in self.batches[index]
            if task_id not in self.merged
            and all(need in self.merged for need in self.specs[task_id].needs)
        ]


def build_plan(manifest: Manifest, stage: str, state: TasksState) -> Plan:
    specs = tasks_for(manifest, stage)
    return Plan(
        stage=stage,
        specs={spec.id: spec for spec in specs},
        batches=plan_batches(specs),
        merged=state.merged_ids(),
        ontology_file=manifest.ontology.path if manifest.ontology else None,
    )


def describe_spec(spec: TaskSpec, ontology_file: str | None) -> dict[str, Any]:
    return {
        "id": spec.id,
        "kind": spec.kind,
        "subject": spec.subject,
        "reads": {
            "manifest": list(spec.reads),
            "ontology_file": ontology_file,
            "input": f"tasks/{spec.id}/{INPUT_DIR}",
        },
        "write_zone": f"tasks/{spec.id}/{OUTPUT_DIR}",
        "owned_keys": list(spec.owns),
        "needs": list(spec.needs),
    }


def describe_plan(plan: Plan, state: TasksState) -> dict[str, Any]:
    current = state.current()
    tasks = []
    for spec in plan.specs.values():
        if spec.id in plan.merged:
            status = "merged"
        elif current is not None and spec.id in current.tasks:
            status = current.tasks[spec.id].status
        else:
            status = "pending"
        tasks.append({**describe_spec(spec, plan.ontology_file), "status": status})
    return {
        "stage": plan.stage,
        "tasks": tasks,
        "batches": plan.batches,
        "next_batch": plan.next_index,
        "open_batch": current.id if current else None,
    }
