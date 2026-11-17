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


def search_label(uid: str, type_name: str | None, name: str | None) -> str:
    """`node:J2 · Junction · Junction J2`: what a search found, by uid, type and name."""
    return " · ".join(part for part in (uid, type_name, name) if part)


def more_found_text(more: int, typed: str) -> str:
    """Said below the objects a search offers when more match. It holds what was typed, so
    that the dropdown, which filters its options by the text typed, keeps it."""
    return f"… {plural(more, 'more object')} match {typed}: type more of the uid or name"


def number_text(value: Any) -> str:
    """A number to four significant digits, as measures and predictions are shown."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value_text(value)
    return f"{value:.4g}"


def version_text(version: dict[str, Any] | None) -> str:
    """`#53 · 2026-01-05 12:00:00 UTC`."""
    if not version:
        return ""
    return f"#{version.get('seq')} · {time_text(version.get('t'))}"


# -- the questions tab ----------------------------------------------------------------------

QUESTIONS_TAB = "Questions"
QUESTIONS_HEADING = "The questions of the contract"
QUESTION_COLUMNS = {
    "priority": "priority",
    "id": "question",
    "text": "text",
    "kind": "kind",
    "status": "status",
    "reason": "why",
    "error": "measured error",
    "tolerance": "tolerance",
    "basis": "basis",
    "function": "function",
    "model": "active model",
    "measured_at": "measured at (data time)",
}
# A status is told by a word and a sign, and coloured as well: never by colour alone.
STATUS_SIGNS = {"validated": "✓", "provisional": "◐", "failing": "✗"}
CHOOSE_QUESTION = "Choose a question in the table to ask it."
ASK = "Ask"
ASK_HEADING = "Ask"
ANSWER_HEADING = "Answer"
AT_LABEL = "at: the version read"
AT_PLACEHOLDER = "head, #<n>, a version id or a time"
BRANCH_LABEL = "branch"
NO_ARGUMENTS = "Its function takes no other argument when it is asked."
NO_FUNCTION = "This question names no function."
CODE_ARGUMENTS = "The twin's own code states no arguments: those it is registered with are fixed."
LIST_PLACEHOLDER = "comma-separated"
NO_VALUE = "The answer holds no value."
FORECAST_HEADING = "Forecast"
ROWS_HEADING = "Rows"
PROVISIONAL_LABEL = "Provisional"
NOTES_LABEL = "Notes"
WROTE_LABEL = "This run wrote to the twin"
BAND_NAME = "interval"
MEAN_NAME = "mean"
ANSWER_GONE = "The rows of this answer are no longer held by the interface; ask again."
CHOICE_TRUE, CHOICE_FALSE = "true", "false"
BAD_ARGUMENT = "An argument cannot be read"
NOT_ASKED = "Nothing has been asked yet: fill in the form and press Ask."


def on_branch_text(branch: str | None, scenario: str | None = None) -> str | None:
    """How a read off the live twin is named: `the scenario valves closed`; None on the
    live twin."""
    if not branch or branch == "main":
        return None
    return f"the scenario {scenario}" if scenario else f"the branch {branch}"


def question_title(question: str, text: str | None) -> str:
    return f"{question}: {text}" if text else question


# -- the branch selector ---------------------------------------------------------------------

BRANCH_SELECT_LABEL = "Branch"
LIVE_OPTION = "live twin"
FOLLOW_LABEL = " Follow head"
SCENARIO_BADGE = "SCENARIO"
# How a value is marked in an object's provenance when no source observed it.
FACT_MARKS = {
    "assumed": "assumed by a scenario or a default, not observed",
    "simulated": "simulated by a model, not observed",
}


def scenario_option(name: str, base_t: str | None, status: str | None) -> str:
    """`valves closed · base 2026-01-05 12:00 UTC · done`."""
    return " · ".join(
        part for part in (name, f"base {minute_text(base_t)}" if base_t else "", status) if part
    )


def scenario_notice(
    name: str, base_t: str | None, base_seq: int | None, head_seq: int | None
) -> str:
    """Said in the header while a scenario is shown, so that it is never taken for the twin."""
    span = f" Its versions run from #{base_seq} to #{head_seq}." if base_seq is not None else ""
    return (
        f"Scenario shown, not the live twin: {name}, from its base at {minute_text(base_t)}. "
        f"What it changed was assumed, and what followed was simulated.{span}"
    )


def scenario_gone(name: str) -> str:
    return f"The scenario {name} no longer has a branch: the live twin is shown again."


def status_text(status: str | None) -> str:
    """`✓ validated`."""
    if not status:
        return ""
    sign = STATUS_SIGNS.get(status)
    return f"{sign} {status}" if sign else status


def measure_text(value: Any, unit: str | None, metric: str | None = None) -> str:
    """`0.12 m (mae)`; empty when nothing was measured."""
    if value is None:
        return ""
    text = with_unit(number_text(value), unit)
    return f"{text} ({metric})" if metric else text


def tolerance_text(value: Any, unit: str | None, metric: str | None = None) -> str:
    """`≤ 0.5 m (mae)`."""
    if value is None:
        return ""
    return "≤ " + measure_text(value, unit, metric)


def questions_status(count: int, version: dict[str, Any], branch_label: str | None) -> str:
    where = f" of {branch_label}" if branch_label else ""
    return (
        f"{plural(count, 'question')} as of {version_text(version)}{where}, "
        "the most important first"
    )


def argument_label(name: str, kind: str | None, required: bool) -> str:
    """`from · version · required`."""
    parts = [name, kind or "any value"]
    if required:
        parts.append("required")
    return " · ".join(parts)


def default_text(default: Any) -> str:
    if default is None:
        return ""
    if isinstance(default, list | tuple):
        return "default " + ", ".join(value_text(item) for item in default)
    return f"default {value_text(default)}"


def bad_argument_text(name: str, kind: str | None, raw: Any) -> str:
    return f"{name}: {value_text(raw)!r} is not {kind_phrase(kind)}"


def kind_phrase(kind: str | None) -> str:
    return {
        "number": "a number",
        "integer": "a whole number",
        "numbers": "a list of numbers",
        "boolean": "true or false",
    }.get(kind or "", "a value of that kind")


def answered_text(answer: dict[str, Any], branch_label: str | None) -> str:
    """`Answered at #65 · 2026-01-05 12:00:00 UTC by level_forecast`."""
    where = f" of {branch_label}" if branch_label else ""
    text = f"Answered at {version_text(answer.get('version'))}{where} by {answer.get('function')}"
    if answer.get("model"):
        text += f", predicted by the model {answer['model']}"
    return text


def model_text(model: str) -> str:
    return f"Model: {model}"


def interval_text(interval: dict[str, Any] | None, unit: str | None) -> str:
    """`80 % interval 4.1 to 4.9 m`."""
    if not interval:
        return ""
    low, high = number_text(interval.get("low")), number_text(interval.get("high"))
    return with_unit(f"{number_text(interval.get('level'))} % interval {low} to {high}", unit)


def provisional_text(reasons: list[str]) -> str:
    because = "; ".join(reasons) if reasons else "the question is not validated"
    return f"{PROVISIONAL_LABEL}: {because}."


def notes_text(notes: list[str]) -> str:
    return f"{NOTES_LABEL}: " + ", ".join(notes)


def wrote_text(version: dict[str, Any] | None, rows: Any) -> str:
    counted = f" ({plural(int(rows), 'row')})" if isinstance(rows, int) else ""
    return f"{WROTE_LABEL}: version {version_text(version)}{counted}."


def path_text(nodes: list[Any]) -> str:
    """A path told by the objects it passes: `node:J1 → node:J2 → node:T1`."""
    return " → ".join(value_text(node) for node in nodes)


def path_summary(entry: dict[str, Any], unit: str | None) -> str:
    """`node:J1 → node:T1 (1 hop, weight 3 m)`."""
    hops = entry.get("hops")
    parts = [plural(int(hops), "hop")] if isinstance(hops, int) else []
    if entry.get("weight") is not None:
        parts.append("weight " + with_unit(number_text(entry["weight"]), unit))
    told = path_text(entry.get("nodes") or [])
    return f"{told} ({', '.join(parts)})" if parts else told


# Columns of an answer's table whose values are in the answer's unit; a forecast's bounds
# (`lo_80`, `hi_80`) are too.
UNIT_COLUMNS = ("value", "mean", "change", "before", "integral")
BOUND_PREFIXES = ("lo_", "hi_")


def column_header(name: str, unit: str | None) -> str:
    """A column of an answer: its name, with the answer's unit for its values."""
    in_unit = name in UNIT_COLUMNS or name.startswith(BOUND_PREFIXES)
    return f"{name} [{unit}]" if unit and in_unit else name


def series_cap_text(drawn: int, total: int) -> str:
    return f"The first {drawn} of {total} series are drawn; every row is in the grid below."
