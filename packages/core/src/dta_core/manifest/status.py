"""Which manifest fields are required, who owns them, and which are still open.

A required leaf field has exactly one status:

- no value                      -> unknown
- value and an explicit entry   -> that entry (`field_status` in the manifest)
- value, no entry, user-owned   -> proposed  (the user has not confirmed it)
- value, no entry, agent-owned  -> confirmed (the agent is its owner)

The gap set is every required field whose status is unknown or proposed. The
discussion stage ends exactly when it is empty.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .models import Autonomy, Manifest, Owner, QuestionKind, Status, Tier

OPEN = frozenset({Status.unknown, Status.proposed})


@dataclass(frozen=True)
class FieldState:
    path: str
    owner: Owner
    status: Status
    description: str


def _is_live(manifest: Manifest, _parent: BaseModel) -> bool:
    return manifest.contract is not None and manifest.contract.tier == Tier.live


def _is_forecast(_manifest: Manifest, parent: BaseModel) -> bool:
    return getattr(parent, "kind", None) in (QuestionKind.predictive, QuestionKind.prescriptive)


def _needs_objective(manifest: Manifest, _parent: BaseModel) -> bool:
    contract = manifest.contract
    if contract is None:
        return False
    prescriptive = any(
        q.kind == QuestionKind.prescriptive for q in (contract.questions or {}).values()
    )
    return prescriptive or bool(contract.controls)


def _is_webhook(_manifest: Manifest, parent: BaseModel) -> bool:
    return getattr(parent, "kind", None) == "webhook"


def _is_executor(_manifest: Manifest, parent: BaseModel) -> bool:
    return getattr(parent, "binding", None) == "executor"


def _is_autonomous_executor(manifest: Manifest, parent: BaseModel) -> bool:
    """A control the twin acts on by itself, through an action that calls the real system:
    what it leaves behind when the twin fails has to be chosen."""
    if getattr(parent, "autonomy", None) != Autonomy.autonomous:
        return False
    action_id = getattr(parent, "action", None)
    action = manifest.actions.get(action_id) if action_id else None
    return action is not None and action.binding == "executor"


# Named conditions a required field can depend on. Each takes the whole manifest
# and the object that holds the field.
def _is_live_series(manifest: Manifest, node: BaseModel) -> bool:
    """A source of a live twin that delivers rows with a time of their own.

    Only such a source has a period at which something new appears. One that
    is read whole and compared, such as a register edited now and then, has
    none: each run sees it as it is.
    """
    mappings = getattr(node, "mappings", None) or {}
    return _is_live(manifest, node) and any(
        mapping.valid_time is not None for mapping in mappings.values()
    )


CONDITIONS: dict[str, Callable[[Manifest, BaseModel], bool]] = {
    "live": _is_live,
    "live_series": _is_live_series,
    "forecast": _is_forecast,
    "objective": _needs_objective,
    "webhook": _is_webhook,
    "executor": _is_executor,
    "autonomous_executor": _is_autonomous_executor,
}


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and len(value) == 0)


def _walk(manifest: Manifest, node: BaseModel, prefix: str) -> Iterator[FieldState]:
    for name, info in type(node).model_fields.items():
        meta = (info.json_schema_extra or {}).get("dta") if info.json_schema_extra else None
        value = getattr(node, name)
        path = f"{prefix}{name}"
        required = bool(meta and meta["required"])
        if required and meta["when"] and not CONDITIONS[meta["when"]](manifest, node):
            required = False
        owner = Owner(meta["owner"]) if meta else Owner.agent
        description = info.description or ""

        if _is_empty(value):
            if required:
                yield FieldState(path, owner, Status.unknown, description)
            continue
        if isinstance(value, BaseModel):
            yield from _walk(manifest, value, f"{path}.")
        elif isinstance(value, dict) and all(isinstance(v, BaseModel) for v in value.values()):
            for key, item in value.items():
                yield from _walk(manifest, item, f"{path}.{key}.")
        elif required:
            explicit = manifest.field_status.get(path)
            default = Status.proposed if owner == Owner.user else Status.confirmed
            yield FieldState(path, owner, explicit or default, description)


def required_fields(manifest: Manifest) -> list[FieldState]:
    """Every required field that applies to this manifest, with its current status."""
    return sorted(_walk(manifest, manifest, ""), key=lambda state: state.path)


def gaps(manifest: Manifest) -> list[FieldState]:
    """Required fields that are still unknown or waiting for the user."""
    return [state for state in required_fields(manifest) if state.status in OPEN]


def _value_at(manifest: Manifest, path: str) -> Any:
    """The value a dotted path names, through models and keyed collections; None if absent."""
    node: Any = manifest
    for part in path.split("."):
        if isinstance(node, BaseModel):
            if part not in type(node).model_fields:
                return None
            node = getattr(node, part)
        elif isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        else:
            return None
    return node


def stale_status_entries(manifest: Manifest) -> list[str]:
    """Paths in `field_status` that no longer point at anything.

    An entry is live when it names a required field that has a value, or when
    it marks as derived a value the toolkit computed: such a value need not be
    a required field (a source's profile, a refresh period on a static twin).
    """
    live = {state.path for state in required_fields(manifest) if state.status != Status.unknown}
    return sorted(
        path
        for path, status in manifest.field_status.items()
        if path not in live
        and not (status == Status.derived and not _is_empty(_value_at(manifest, path)))
    )
