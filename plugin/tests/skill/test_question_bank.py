"""The question bank asks for exactly the contract fields the user owns."""

from __future__ import annotations

import typing
from typing import Any

from dta_core.manifest.models import Contract
from manifest_reference import KEY_NAMES
from pydantic import BaseModel
from skilldocs import REFERENCE, tables_headed, unquote

BANK = REFERENCE / "question-bank.md"


def _section(annotation: Any) -> type[BaseModel] | None:
    for candidate in (annotation, *typing.get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _map_section(annotation: Any) -> type[BaseModel] | None:
    for candidate in (annotation, *typing.get_args(annotation)):
        if typing.get_origin(candidate) is dict:
            return _section(typing.get_args(candidate)[1])
    return None


def _has_required(model: type[BaseModel]) -> bool:
    return any(
        (info.json_schema_extra or {}).get("dta", {}).get("required")  # type: ignore[union-attr]
        for info in model.model_fields.values()
    )


def _user_fields(model: type[BaseModel], prefix: str, required: bool) -> set[str]:
    """User-owned fields a person answers, required or optional as asked: leaves, and a
    required section with nothing required inside it (it is given as a whole)."""
    found: set[str] = set()
    for name, info in model.model_fields.items():
        meta = (info.json_schema_extra or {}).get("dta", {})  # type: ignore[union-attr]
        path = f"{prefix}{name}"
        section, items = _section(info.annotation), _map_section(info.annotation)
        if items is not None:
            found |= _user_fields(items, f"{path}.<{KEY_NAMES.get(name, 'key')}>.", required)
            continue
        if section is not None:
            found |= _user_fields(section, f"{path}.", required)
            if _has_required(section) or not meta.get("required"):
                continue
        if meta.get("owner") == "user" and bool(meta.get("required")) == required:
            found.add(path)
    return found


def _bank(first: str) -> set[str]:
    [table] = tables_headed(BANK, first)
    return {unquote(row[0]) for row in table.rows}


def test_bank_asks_every_required_user_field_of_the_contract() -> None:
    expected = _user_fields(Contract, "contract.", required=True)
    assert "contract.controls.<control>.bounds" in expected
    assert _bank("Contract field") == expected


def test_bank_covers_every_optional_user_field_of_the_contract() -> None:
    expected = _user_fields(Contract, "contract.", required=False)
    assert "contract.addons.executors" in expected
    assert _bank("Optional contract field") == expected
