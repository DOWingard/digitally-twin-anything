"""Action types and control loops of the manifest as records of the twin.

The manifest declares them; what the twin acts on is what these records say. `rows`
compares what the manifest asks for with what is stored and gives the rows that bring
the two in line, to be written by the caller in the same version as the functions they
refer to: a loop's policy is a reference to its function's record.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import ActionType, Control, ControlLoop

from ..twin import Twin
from .records import (
    ACTION_TYPE,
    ACTIVE,
    ADVISORY,
    DISABLED,
    LOOP_TYPE,
    MANIFEST,
    action_uid,
    canonical,
    duration_text,
    function_uid,
    loop_uid,
)

log = Log("runtime.actions")

SECTIONS = {ACTION_TYPE: "actions", LOOP_TYPE: "loops"}
OUTCOMES = ("created", "changed", "unchanged", "orphaned")

Known = Mapping[str, Mapping[str, Mapping[str, Any]]]


@dataclass
class Registration:
    """The rows to write, by type, and what each record of the manifest came to.

    `answer` holds, for `actions` and for `loops`, the uids `created`, `changed`,
    `unchanged` and `orphaned` (declared by an earlier manifest and no longer: their
    status becomes disabled, and an orphaned loop is disabled as well).
    """

    upserts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    answer: dict[str, dict[str, list[str]]] = field(
        default_factory=lambda: {
            section: {outcome: [] for outcome in OUTCOMES} for section in SECTIONS.values()
        }
    )

    @property
    def written(self) -> int:
        return sum(len(found) for found in self.upserts.values())


def _json(value: Any) -> str | None:
    return None if value is None else canonical(value)


def _action_row(
    action_id: str, action: ActionType, controls: Mapping[str, Control], manifest: Manifest
) -> dict[str, Any]:
    control = controls.get(action.control) if action.control else None
    target = control.target if control is not None else None
    object_type = action.object_type or (target.partition(".")[0] if target else None)
    autonomy = control.autonomy.value if control is not None and control.autonomy else ADVISORY
    bounds = (
        control.bounds.model_dump(mode="json", exclude_none=True)
        if control is not None and control.bounds is not None
        else None
    )
    settle = action.settle
    if settle is None and manifest.sync is not None and manifest.sync.interval is not None:
        settle = 2 * manifest.sync.interval
    return {
        "uid": action_uid(action_id),
        "description": action.description,
        "control": action.control,
        "target": target,
        "object_type": object_type,
        "binding": action.binding,
        "autonomy": autonomy,
        "parameters": canonical(
            {
                name: parameter.model_dump(mode="json", exclude_none=True)
                for name, parameter in action.parameters.items()
            }
        ),
        "bounds": _json(bounds),
        # The executor names the variables it needs; their values are never in the manifest.
        "executor": _json(
            action.executor.model_dump(mode="json") if action.executor is not None else None
        ),
        "settle": duration_text(settle),
        "tolerance": float(action.tolerance),
        "precondition": action.precondition,
        "expected_effect": action.expected_effect,
        "origin": MANIFEST,
        "status": ACTIVE,
    }


def _loop_row(loop_id: str, loop: ControlLoop) -> dict[str, Any]:
    return {
        "uid": loop_uid(loop_id),
        "description": loop.description,
        "controls": list(loop.controls or []),
        "policy": function_uid(loop.policy or ""),
        "interval": duration_text(loop.interval),
        "origin": MANIFEST,
        "status": ACTIVE,
    }


def wanted(manifest: Manifest) -> dict[str, dict[str, dict[str, Any]]]:
    """The records the manifest asks for, by type and uid."""
    controls = dict(manifest.contract.controls) if manifest.contract else {}
    actions = {
        action_uid(action_id): _action_row(action_id, action, controls, manifest)
        for action_id, action in manifest.actions.items()
        if action.binding is not None
    }
    loops = {
        loop_uid(loop_id): _loop_row(loop_id, loop)
        for loop_id, loop in manifest.control_loops.items()
        if loop.policy is not None and loop.interval is not None
    }
    return {ACTION_TYPE: actions, LOOP_TYPE: loops}


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        return list(left or []) == list(right or [])
    return left == right


def _changes(asked: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in asked.items()
        if name != "uid" and not _same(value, stored.get(name))
    }


def rows(manifest: Manifest, known: Known) -> Registration:
    """The `TwinAction` and `TwinControlLoop` rows that bring the stored records in line
    with the manifest. `known` holds the stored records by type name and uid (as `known`
    reads them). A new loop is disabled until it is enabled; a loop that is changed keeps
    whether it is enabled."""
    started = time.perf_counter()
    found = Registration()
    for type_name, asked in wanted(manifest).items():
        section = found.answer[SECTIONS[type_name]]
        stored = known.get(type_name, {})
        for uid, row in asked.items():
            current = stored.get(uid)
            if current is None:
                new = {**row, "enabled": False} if type_name == LOOP_TYPE else row
                section["created"].append(uid)
                found.upserts.setdefault(type_name, []).append(new)
                continue
            changed = _changes(row, current)
            if not changed:
                section["unchanged"].append(uid)
                continue
            section["changed"].append(uid)
            found.upserts.setdefault(type_name, []).append({"uid": uid, **changed})
        for uid, current in sorted(stored.items()):
            if uid in asked or current.get("origin") != MANIFEST:
                continue
            if current.get("status") == DISABLED:
                continue
            gone: dict[str, Any] = {"uid": uid, "status": DISABLED}
            if type_name == LOOP_TYPE:
                gone["enabled"] = False
            section["orphaned"].append(uid)
            found.upserts.setdefault(type_name, []).append(gone)
    log.info(
        "action types and control loops compared with the manifest",
        counts={
            section: {outcome: len(uids) for outcome, uids in parts.items()}
            for section, parts in found.answer.items()
        },
        rows=found.written,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return found


def known(twin: Twin, at: Any = "head") -> dict[str, dict[str, dict[str, Any]]]:
    """The stored `TwinAction` and `TwinControlLoop` records at a version, by type and
    uid, as `rows` compares them."""
    store = twin.store
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for type_name in (ACTION_TYPE, LOOP_TYPE):
        if not twin.ontology.has_type(type_name):
            found[type_name] = {}
            continue
        table = store.table(type_name, at=at, ontology=twin.ontology)
        found[type_name] = {row["uid"]: row for row in table.to_pylist()}
    return found
