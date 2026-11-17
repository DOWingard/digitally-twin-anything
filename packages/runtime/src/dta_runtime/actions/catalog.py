"""The action types of a twin as they are handed out and as proposals are judged by."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from dta_core.log import Log

from ..twin import Twin
from .records import (
    ACTION_TYPE,
    ADVISORY,
    action_uid,
    bare,
    duration_of,
    from_json,
    get,
)

log = Log("runtime.actions")


def action_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """A `TwinAction` record with its JSON fields parsed and its id."""
    return {
        "id": bare(row["uid"]),
        "uid": row["uid"],
        "description": row.get("description"),
        "control": row.get("control"),
        "target": row.get("target"),
        "object_type": row.get("object_type"),
        "binding": row.get("binding"),
        "autonomy": row.get("autonomy") or ADVISORY,
        "parameters": from_json(row.get("parameters"), {}),
        "bounds": from_json(row.get("bounds")),
        "executor": from_json(row.get("executor")),
        "settle": row.get("settle"),
        "tolerance": float(row.get("tolerance") or 0.0),
        "precondition": row.get("precondition"),
        "expected_effect": row.get("expected_effect"),
        "origin": row.get("origin"),
        "status": row.get("status"),
    }


def settle_of(document: Mapping[str, Any]) -> Any:
    """How long after its effective time an action's effect must be observable."""
    return duration_of(document.get("settle"))


def types(twin: Twin, at: Any = None) -> list[dict[str, Any]]:
    """Every action type at a version of the live line (the head by default), by id:
    its parameters, the bounds and autonomy of its control, its binding."""
    started = time.perf_counter()
    if not twin.ontology.has_type(ACTION_TYPE):
        return []
    table = twin.store.table(ACTION_TYPE, at=at or "head", ontology=twin.ontology)
    found = sorted((action_document(row) for row in table.to_pylist()), key=lambda d: d["id"])
    log.debug(
        "action types read",
        count=len(found),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return found


def find(twin: Twin, action: str, at: Any = "head") -> dict[str, Any] | None:
    """One action type by id or uid; None when there is none."""
    row = get(twin, action_uid(action), ACTION_TYPE, at)
    return None if row is None else action_document(row)
