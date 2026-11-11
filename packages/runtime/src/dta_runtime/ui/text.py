"""Values and times as the page shows them."""

from __future__ import annotations

import json
from typing import Any


def time_text(value: str | None) -> str:
    """`2026-01-05T21:00:00Z` -> `2026-01-05 21:00:00 UTC`; anything else unchanged."""
    if not value:
        return ""
    if value.endswith("Z") and "T" in value:
        return value[:-1].replace("T", " ") + " UTC"
    return value.replace("T", " ")


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)
    return str(value)


def with_unit(text: str, unit: str | None) -> str:
    return f"{text} {unit}" if unit and text else text


def cell(value: Any) -> Any:
    """A value for a grid cell: lists and structured values as JSON text, the rest as is."""
    if isinstance(value, dict | list):
        return value_text(value)
    if isinstance(value, bool):
        return value_text(value)
    return value


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
