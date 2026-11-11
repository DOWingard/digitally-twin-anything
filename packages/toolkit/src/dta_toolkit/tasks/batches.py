"""Which tasks may run at the same time.

Two tasks may share a batch only if neither writes anything the other reads or
writes. A task writes the manifest keys it owns and its private output
directory; private directories are distinct by construction (ids are unique),
so the decision rests on manifest paths alone.

Batches come out in a fixed order: first by what a task needs (a task runs
after everything it needs), then, inside one such layer, tasks that conflict
are pushed into later batches in id order.
"""

from __future__ import annotations

from collections.abc import Sequence

from dta_core.log import Log

from .catalogue import TaskSpec
from .errors import TaskError
from .paths import overlaps, parse_pattern

log = Log("tasks.batches")


def _writes_into(writer: TaskSpec, other: TaskSpec) -> bool:
    owned = [parse_pattern(text) for text in writer.owns]
    touched = [parse_pattern(text) for text in (*other.reads, *other.owns)]
    return any(overlaps(key, path) for key in owned for path in touched)


def conflicts(first: TaskSpec, second: TaskSpec) -> bool:
    """True when the two tasks must not run at the same time."""
    return _writes_into(first, second) or _writes_into(second, first)


def _layers(tasks: dict[str, TaskSpec]) -> dict[str, int]:
    """Depth of each task in the needs graph: 0 for a task that needs nothing."""
    depth: dict[str, int] = {}
    visiting: list[str] = []

    def resolve(task_id: str) -> int:
        if task_id in depth:
            return depth[task_id]
        if task_id in visiting:
            cycle = [*visiting[visiting.index(task_id) :], task_id]
            log.error("tasks need each other in a cycle", cycle=cycle)
            raise TaskError(
                f"tasks need each other in a cycle: {' -> '.join(cycle)}",
                code="tasks.plan.cycle",
                detail={"cycle": cycle},
            )
        visiting.append(task_id)
        level = 0
        for need in tasks[task_id].needs:
            if need not in tasks:
                log.error("task needs an unknown task", task=task_id, need=need)
                raise TaskError(
                    f"task {task_id} needs {need}, which is not among the tasks",
                    code="tasks.plan.unknown_need",
                    detail={"task": task_id, "need": need},
                )
            level = max(level, resolve(need) + 1)
        visiting.pop()
        depth[task_id] = level
        return level

    for task_id in sorted(tasks):
        resolve(task_id)
    return depth


def plan_batches(specs: Sequence[TaskSpec]) -> list[list[str]]:
    """Group task ids into batches that are safe to run in parallel, in execution order."""
    tasks: dict[str, TaskSpec] = {}
    for spec in specs:
        if spec.id in tasks:
            log.error("task id appears twice", id=spec.id)
            raise TaskError(
                f"task id {spec.id!r} appears twice",
                code="tasks.plan.duplicate",
                detail={"id": spec.id},
            )
        tasks[spec.id] = spec

    depth = _layers(tasks)
    batches: list[list[str]] = []
    for level in sorted(set(depth.values())):
        groups: list[list[str]] = []
        for task_id in sorted(tid for tid, at in depth.items() if at == level):
            for group in groups:
                if not any(conflicts(tasks[task_id], tasks[member]) for member in group):
                    group.append(task_id)
                    break
            else:
                groups.append([task_id])
        batches.extend(groups)
    return batches
