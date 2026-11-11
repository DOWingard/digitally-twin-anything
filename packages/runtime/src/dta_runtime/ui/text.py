"""Values, times and durations as the page shows them."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError

_DURATION = TypeAdapter(timedelta)
# Largest unit first; a duration is told in its largest unit and the next one.
_UNITS = ((86_400, "d"), (3_600, "h"), (60, "min"), (1, "s"))


def time_text(value: str | None) -> str:
    """`2026-01-05T21:00:00Z` -> `2026-01-05 21:00:00 UTC`; anything else unchanged."""
    if not value:
        return ""
    if value.endswith("Z") and "T" in value:
        return value[:-1].replace("T", " ") + " UTC"
    return value.replace("T", " ")


def minute_text(value: str | None) -> str:
    """`2026-01-05T21:00:30Z` -> `2026-01-05 21:00 UTC`: a time to the minute."""
    if not value:
        return ""
    if value.endswith("Z") and "T" in value and len(value) >= 17:
        return value[:16].replace("T", " ") + " UTC"
    return time_text(value)


def seconds_text(seconds: float) -> str:
    """A duration in its largest unit and the next one: `12 min`, `1 h 5 min`, `45 s`."""
    whole = int(round(abs(seconds)))
    if whole == 0:
        return "0 s"
    for index, (size, name) in enumerate(_UNITS):
        if whole >= size:
            parts = [f"{whole // size} {name}"]
            if index + 1 < len(_UNITS):
                following, label = _UNITS[index + 1]
                rest = (whole % size) // following
                if rest:
                    parts.append(f"{rest} {label}")
            return " ".join(parts)
    return f"{whole} s"


def duration_text(value: str | None) -> str:
    """An ISO 8601 duration as the API writes it (`PT1H`) told as `seconds_text` tells it.

    Anything that is not a duration is shown as it came.
    """
    if not value:
        return ""
    try:
        return seconds_text(_DURATION.validate_python(value).total_seconds())
    except ValidationError:
        return value


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
