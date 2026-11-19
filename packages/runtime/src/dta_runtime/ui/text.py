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


def exact_text(value: Any) -> str:
    """A value as it was sent or seen, to its last digit; a whole float shows no `.0`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value_text(value)


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


# -- the scenarios tab -----------------------------------------------------------------------

SCENARIOS_TAB = "Scenarios"
SCENARIOS_HEADING = "Scenarios and how they compare"
SCENARIO_COLUMNS = {
    "name": "name",
    "base_t": "base time",
    "horizon": "horizon",
    "status": "status",
    "interventions": "interventions",
    "models": "models",
}
# Told by a word and a sign; a deleted or failed scenario is also coloured.
SCENARIO_SIGNS = {"done": "✓", "running": "…", "created": "○", "failed": "✗", "deleted": "⌫"}
CHOOSE_SCENARIO = "Choose a scenario in the table to compare it."
TARGETS_LABEL = "targets: what to compare (Type.property)"
AGAINST_LABEL = "against"
COMPARE = "Compare"
NO_INTERVENTION = "none: the twin as it is"
PREDICTED_MARK = "predicted by its models"
COMPARE_COLUMNS = {
    "target": "target",
    "uid": "object",
    "t": "time",
    "scenario": "scenario",
    "against": "against",
    "difference": "difference",
}
COMPARED_GONE = "This comparison is no longer held by the interface; compare again."
NO_ROWS = "The comparison holds no rows for these targets."
NO_TARGETS = "Choose at least one target to compare."


def scenarios_status(count: int, head_seq: int) -> str:
    return f"{plural(count, 'scenario')} as of the live twin's #{head_seq}, deleted ones too"


def scenario_status_text(status: str | None) -> str:
    if not status:
        return ""
    sign = SCENARIO_SIGNS.get(status)
    return f"{sign} {status}" if sign else status


def intervention_text(intervention: dict[str, Any]) -> str:
    """`Valve.status of link:V1 set to closed from 2026-01-05 12:15 UTC`, with its end."""
    text = (
        f"{intervention.get('target')} of {intervention.get('uid')} set to "
        f"{value_text(intervention.get('value'))} from {minute_text(intervention.get('start'))}"
    )
    if intervention.get("end"):
        text += f" to {minute_text(intervention['end'])}"
    return text


def interventions_text(interventions: list[dict[str, Any]] | None) -> str:
    if not interventions:
        return NO_INTERVENTION
    return "; ".join(intervention_text(found) for found in interventions)


def scenario_heading(scenario: dict[str, Any]) -> str:
    return (
        f"{scenario.get('name')}: from {minute_text(scenario.get('base_t'))} for "
        f"{duration_text(scenario.get('horizon'))}, {scenario_status_text(scenario.get('status'))}"
    )


def target_option(target: str, predicted: bool) -> str:
    return f"{target} · {PREDICTED_MARK}" if predicted else target


def against_option(scenario: dict[str, Any]) -> str:
    return f"the scenario {scenario.get('name')} ({scenario_status_text(scenario.get('status'))})"


def not_comparable_text(scenario: dict[str, Any]) -> str:
    """Said in place of a chart when a scenario has nothing to compare."""
    name, status = scenario.get("name"), scenario.get("status")
    if status == "created":
        return f"{name} has not been run yet: it has nothing to compare."
    if status == "running":
        return f"{name} is running: compare it once it is done."
    if status == "failed":
        return f"{name} failed and has nothing to compare: {scenario.get('error') or 'no reason'}."
    if status == "deleted":
        return f"{name} was deleted: its branch is gone, and it has nothing to compare."
    return f"{name} is {status}: it has nothing to compare."


def compared_text(name: str, against: str, rows: int, targets: int) -> str:
    return (
        f"{name} against {against}: {plural(rows, 'row')} for {plural(targets, 'target')}. "
        "A line stops where a side has no value."
    )


def trace_name(uid: str, side: str) -> str:
    """`node:T1 · valves closed`: an object on one side of a comparison."""
    return f"{uid} · {side}"


def pick_label(target: str, most: int) -> str:
    return f"{target}: objects drawn, at most {most}, the largest differences first"


def picked_cap_text(most: int) -> str:
    return f"At most {most} objects are drawn at once: the {most} chosen last are."


def compare_cut_text(rows: int) -> str:
    return f"Only the first {rows} rows are compared: choose fewer targets."


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


def band_name(uid: str, level: str) -> str:
    """`node:T1 interval 80 %`: a forecast's band."""
    return f"{uid} {BAND_NAME} {level} %"


def mean_name(uid: str) -> str:
    return f"{uid} {MEAN_NAME}"


def series_cap_text(drawn: int, total: int) -> str:
    return f"The first {drawn} of {total} series are drawn; every row is in the grid below."


# -- the state badge and the failed line ------------------------------------------------------

BADGE_LABEL = "fidelity"
# A state is told by a word and a sign, and coloured as well: never by colour alone.
STATE_SIGNS = {"unknown": "?", "healthy": "✓", "correcting": "◐", "failed": "✗"}
FAILED_WARNING = "The twin has failed: its answers and actions are not to be relied on."


def state_text(state: str | None) -> str:
    """`✓ healthy`."""
    word = state or "unknown"
    sign = STATE_SIGNS.get(word)
    return f"{sign} {word}" if sign else word


def badge_details(since: str | None, open_count: int, failed_count: int) -> str:
    """` since 2026-01-05 06:00 UTC · 1 open incident, 1 failed`."""
    text = f" since {minute_text(since)}" if since else ""
    text += f" · {plural(open_count, 'open incident')}"
    if failed_count:
        text += f", {failed_count} failed"
    return text


def failed_subjects_text(incidents: list[dict[str, Any]]) -> str:
    """` Failed: the stale incident of level_ahead; the breach incident of flow.`"""
    if not incidents:
        return ""
    told = "; ".join(
        f"the {found.get('kind') or ''} incident of {found.get('subject') or found.get('uid')}"
        for found in incidents
    )
    return f" Failed: {told}."


# -- the fidelity tab ---------------------------------------------------------------------------

FIDELITY_TAB = "Fidelity"
FIDELITY_HEADING = "Whether the twin keeps its promises"
NO_FIDELITY = (
    "This twin sets no fidelity interval and window: it is never checked, and its state stays "
    "unknown."
)
LIVE_ONLY = (
    "Fidelity and actions belong to the live twin: choose the live twin in the branch "
    "selector to see them."
)
SUMMARY_LABELS = {
    "state": "state",
    "since": "since",
    "check": "last check",
    "incidents": "incidents by status",
    "signals": "suspect signals",
}
NO_CHECK = "none yet"
NO_INCIDENT = "none"
CHARTS_HEADING = "The error of each predictive question"
RANGE_LABEL = "range of the charts: data time before the selected version"
# Days before the selected version's time that the charts read; 0 reads everything.
RANGE_OPTIONS = {7: "last 7 days", 30: "last 30 days", 90: "last 90 days", 365: "last 365 days"}
RANGE_ALL = "everything"
NO_PREDICTIVE = "The contract has no predictive question: there is no error to chart."
POINTS_NAME = "error per scored origin"
WINDOW_LINE_NAME = "window error"
CORRECTIONS_NAME = "corrections"
X_AXIS_TITLE = "time scored (data time)"
POINTS_HELP = "Filled: counted in the window of the latest check; hollow: not counted."
INCIDENTS_HEADING = "Incidents, newest first"
STATUS_FILTER_LABEL = "status"
# What a filter with nothing chosen lets through.
FILTER_ANY = "any"
INCIDENT_COLUMNS = {
    "kind": "kind",
    "subject": "subject",
    "status": "status",
    "rung": "rung",
    "attempt": "attempt",
    "cycle": "cycle",
    "next_cycle_at": "next cycle",
    "opened_at": "opened",
    "resolved_at": "resolved",
}
# Told by a word and a sign; an unresolved incident is coloured as well.
INCIDENT_SIGNS = {"open": "●", "remodel_requested": "…", "failed": "✗", "resolved": "✓"}
CHOOSE_INCIDENT = "Choose an incident in the grid to see its steps."
STEP_COLUMNS = (
    "when",
    "step",
    "what was tried",
    "outcome",
    "error before",
    "error after",
    "cycle",
    "next cycle",
)
SIGNALS_HEADING = "Signals found suspect"
SIGNAL_COLUMNS = {
    "object": "object",
    "property": "property",
    "status": "status",
    "reason": "reason",
    "since": "since",
    "reading": "raw reading kept",
    "inferred": "inferred value in force",
    "clear_checks": "checks sound so far",
}
CHECKS_HEADING = "The last 50 checks"
CHECK_COLUMNS = {"at": "time", "state": "state", "duration": "duration"}
NOTIFICATIONS_HEADING = "Notifications"
NOTIFICATION_COLUMNS = {
    "type": "type",
    "subject": "subject",
    "time": "time",
    "status": "status",
    "tries": "tries",
}


def milliseconds_text(value: Any) -> str:
    """`127 ms` under a second, `seconds_text` from a second on."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    if value < 1000:
        return f"{round(value)} ms"
    return seconds_text(value / 1000.0)


def fidelity_status(version: dict[str, Any]) -> str:
    return f"as of {version_text(version)} of the live twin"


def check_text(check: dict[str, Any] | None) -> str:
    """`twin_fidelity_check:20260105T060000Z at 2026-01-05 06:00:00 UTC`."""
    if not check:
        return NO_CHECK
    return f"{check.get('uid')} at {time_text(check.get('at'))}"


def counts_text(counts: dict[str, int]) -> str:
    """`2 open, 1 resolved`: incidents by status, in the order of INCIDENT_SIGNS."""
    told = [f"{counts[status]} {status}" for status in INCIDENT_SIGNS if counts.get(status)]
    return ", ".join(told) or NO_INCIDENT


def signals_text(signals: list[dict[str, Any]]) -> str:
    """`2: Gauge.level of gauge:G1 (stuck, value unknown); …`: what stands for each
    suspect signal's value, the forecast made at its flag or nothing."""
    if not signals:
        return NO_INCIDENT
    told = "; ".join(
        f"{found.get('property')} of {found.get('object')} ({found.get('reason')}"
        + (f", value {found['value']})" if found.get("value") else ")")
        for found in signals
    )
    return f"{len(signals)}: {told}"


def incident_status_text(status: str | None) -> str:
    if not status:
        return ""
    sign = INCIDENT_SIGNS.get(status)
    return f"{sign} {status}" if sign else status


def range_text(days: int) -> str:
    return RANGE_OPTIONS.get(days, RANGE_ALL)


def tolerance_name(value: Any, unit: str | None) -> str:
    """`ε = 0.5 m`."""
    return "ε = " + with_unit(number_text(value), unit)


def window_name(window: str | None) -> str:
    return f"window W = {duration_text(window)}"


def axis_title(metric: str | None, unit: str | None) -> str:
    """`error, mae [m]`: the question's metric and unit."""
    text = f"error, {metric}" if metric else "error"
    return f"{text} [{unit}]" if unit else text


def chart_title(question: str, text: str | None, points: int) -> str:
    title = question_title(question, text)
    return f"{title} · {plural(points, 'scored origin')}"


def no_errors_text(question: str) -> str:
    return f"No origin of {question} was scored in this range."


def chart_cut_text(question: str, drawn: int) -> str:
    return f"Only the first {drawn} scored origins of {question} are drawn: choose a shorter range."


def checks_cut_text(drawn: int) -> str:
    return f"The window error is drawn over the newest {drawn} checks only."


def incident_title(incident: dict[str, Any]) -> str:
    """`breach incident of level_ahead: remodel_requested, opened 2026-01-05 07:00:00 UTC`."""
    status = incident_status_text(incident.get("status"))
    opened = time_text(incident.get("opened_at"))
    text = (
        f"{incident.get('kind')} incident of {incident.get('subject')}: {status}, opened {opened}"
    )
    if incident.get("resolved_at"):
        text += f", resolved {time_text(incident['resolved_at'])}"
    return text


def remodel_text(spec: str | None, until: str | None, models: list[str] | None) -> str:
    """What an incident waiting for a re-model waits for, and until when."""
    what = f"the model specification {spec}" if spec else "the question's model specification"
    known = f" Its models when it asked: {', '.join(models)}." if models else ""
    deadline = f" until {time_text(until)}" if until else ""
    return (
        f"Waits for the managing agent to re-model {what}{deadline}: a model of it registered "
        f"or changed brings the reselection back; nothing by then fails the incident.{known}"
    )


def step_tried(step: dict[str, Any]) -> str:
    """What a step of an incident tried: the rung of a correction asked for or reported."""
    action = step.get("action")
    if action == "request":
        return f"{step.get('rung')}, attempt {step.get('attempt')}, cycle {step.get('cycle')}"
    if action == "result":
        return str(step.get("rung") or "")
    if action == "remodel_requested":
        return "asked the managing agent to re-model"
    if action in ("retry", "resolve", "closed", "cleared") and step.get("by"):
        note = f": {step['note']}" if step.get("note") else ""
        return f"by {step['by']}{note}"
    return ""


def step_outcome(step: dict[str, Any]) -> str:
    """What became of a step of an incident, in words."""
    action = step.get("action")
    if action == "result":
        if step.get("accepted"):
            fitted_to = step.get("fitted_to")
            fitted = f", fitted to {time_text(fitted_to)}" if fitted_to else ""
            return f"accepted: {step.get('model')}{fitted}"
        return refused_text(step.get("reason"))
    if action == "unknown":
        questions = step.get("questions") or []
        unanswered = f"; without an answer: {', '.join(questions)}" if questions else ""
        return f"value unknown ({step.get('why')}): {step.get('message') or ''}{unanswered}"
    if action == "request":
        return "asked for"
    if action in ("held", "recovered", "resolved", "not_held"):
        counted = f" over {plural(int(step['n']), 'origin')}" if step.get("n") is not None else ""
        words = {
            "held": "held",
            "recovered": "recovered",
            "resolved": "resolved",
            "not_held": "not held",
        }
        return words[str(action)] + counted
    if action == "remodel_requested":
        timeout = step.get("timeout_s")
        return f"waits at most {seconds_text(float(timeout))}" if timeout is not None else "waits"
    if action == "failed":
        return f"failed: {step.get('reason') or 'no reason given'}"
    if action == "cycle":
        return f"cycle {step.get('cycle')} begins"
    if action == "cycle_ended":
        due = step.get("next_cycle_at")
        later = f" at {time_text(due)}" if due else ""
        return f"cycle {step.get('cycle')} ended; cycle {step.get('next_cycle')} begins{later}"
    if action == "cycles_spent":
        return (
            f"every cycle of the budget has run ({plural(int(step.get('cycles') or 0), 'cycle')}); "
            "waits for the re-model"
        )
    if action == "stale":
        return f"stale at {plural(int(step.get('count') or 0), 'check')} in a row"
    if action == "fresh":
        return "fresh again"
    if action == "flagged":
        return f"flagged {step.get('reason') or ''}".strip()
    if action == "cleared":
        return f"cleared by {step.get('by') or 'a check'}{cleared_evidence(step)}"
    return str(action or "")


def refused_text(reason: Any) -> str:
    """Why a correction was not accepted: the evaluation's code and words, the
    acceptance rule and each candidate's numbers."""
    if not isinstance(reason, dict) or not (reason.get("code") or reason.get("message")):
        return "not accepted"
    text = f"not accepted ({reason.get('code') or 'no code'}): {reason.get('message') or ''}"
    numbers = reason.get("numbers") if isinstance(reason.get("numbers"), dict) else {}
    if numbers.get("metric") is not None:
        text += f"; accepted at {numbers['metric']} <= {numbers.get('threshold')}"
    for item in numbers.get("candidates") or []:
        parts = [f"{item.get('uid')}: error {number_text(item.get('error'))}"]
        if item.get("baseline_error") is not None:
            parts.append(f"baseline {number_text(item.get('baseline_error'))}")
        if item.get("skill") is not None:
            parts.append(f"skill {number_text(item.get('skill'))}")
        text += f"; {', '.join(parts)}"
    return text


def cleared_evidence(step: dict[str, Any]) -> str:
    """What a check cleared a signal on: the raw readings moving again, or a reading
    agreeing with the inferred value."""
    if step.get("changes") is not None:
        return (
            f": {plural(int(step['changes']), 'change')} of the raw readings since the flag, "
            f"{plural(int(step.get('run') or 0), 'interval')} without one"
        )
    if step.get("reading") is not None and step.get("inferred") is not None:
        return (
            f": the reading {number_text(step['reading'])} within "
            f"{number_text(2.0 * float(step.get('tolerance') or 0.0))} of the inferred "
            f"{number_text(step['inferred'])}"
        )
    return ""


# -- the actions tab ------------------------------------------------------------------------------

ACTIONS_TAB = "Actions"
ACTIONS_HEADING = "What the twin does to the real system"
TYPES_HEADING = "Action types"
TYPE_COLUMNS = {
    "id": "id",
    "control": "control",
    "target": "target",
    "autonomy": "autonomy",
    "binding": "binding",
    "bounds": "bounds",
    "settle": "settle",
    "on_failure": "when the twin fails",
    "status": "status",
}
EXECUTOR_ON = "An executor is configured: the twin makes the calls of the actions it may execute."
EXECUTOR_OFF = "No executor is configured: the twin makes no external call."
RECORDS_HEADING = "Records of the proposals, newest first"
ACTION_FILTER_LABEL = "action"
ORIGIN_FILTER_LABEL = "origin"
# Where a record came from, as the API's `origin` says.
ORIGIN_WORDS = {
    "failsafe": "fail-safe",
    "loop": "control loop",
    "proposal": "proposal",
}
RECORD_COLUMNS = {
    "proposed_at": "proposed at",
    "action": "action",
    "object": "object",
    "params": "parameters",
    "origin": "origin",
    "source": "source",
    "fidelity": "fidelity at the proposal",
    "status": "status",
    "reconciliation": "reconciliation",
    "effective_at": "effective at",
}
# Told by a word and a sign; a refused or failed record is coloured as well.
RECORD_SIGNS = {
    "proposed": "○",
    "approved": "…",
    "executed": "✓",
    "rejected": "✗",
    "blocked": "⊘",
    "failed": "✗",
}
CHOOSE_RECORD = "Choose a record in the grid to see everything about it."
RECORD_PARTS = {
    "proposal": "Proposal",
    "reasons": "Why it was refused",
    "approval": "Decision",
    "execution": "Execution",
    "observed": "Observed against expected",
}
NO_REASONS = "No reason was recorded: it was not refused."
NO_APPROVAL = "No decision was recorded."
NO_EXECUTION = "No call was made."
NO_OBSERVED = "Not reconciled yet."
LOOPS_HEADING = "Control loops"
LOOP_COLUMNS = {
    "id": "id",
    "policy": "policy",
    "interval": "interval",
    "enabled": "enabled",
    "last_run_at": "last run at",
}
LAST_RUNS_HEADING = "The last run of each loop"
LAST_RUN_COLUMNS = (
    "loop",
    "at",
    "status",
    "proposed",
    "executed",
    "blocked",
    "rejected",
    "failed",
    "awaiting",
    "notes",
    "error",
)
NO_LAST_RUN = "No loop has run yet."
NO_LOOPS = "The manifest declares no control loop."
ENABLED_WORDS = {True: "yes", False: "no"}


def record_status_text(status: str | None) -> str:
    if not status:
        return ""
    sign = RECORD_SIGNS.get(status)
    return f"{sign} {status}" if sign else status


def bounds_text(bounds: dict[str, Any] | None) -> str:
    """`1 to 200, steps of at most 50` or `one of true, false`."""
    if not bounds:
        return ""
    parts = []
    if bounds.get("allowed") is not None:
        parts.append("one of " + ", ".join(value_text(value) for value in bounds["allowed"]))
    low, high = bounds.get("minimum"), bounds.get("maximum")
    if low is not None and high is not None:
        parts.append(f"{number_text(low)} to {number_text(high)}")
    elif low is not None:
        parts.append(f"at least {number_text(low)}")
    elif high is not None:
        parts.append(f"at most {number_text(high)}")
    if bounds.get("max_step") is not None:
        parts.append(f"steps of at most {number_text(bounds['max_step'])}")
    return ", ".join(parts)


def params_text(params: dict[str, Any] | None) -> str:
    """`value = 20`, each parameter of a proposal."""
    return ", ".join(f"{name} = {exact_text(value)}" for name, value in (params or {}).items())


def reason_text(found: dict[str, Any]) -> str:
    """`value: 500 is above the maximum 200`."""
    member = found.get("member")
    message = found.get("message") or ""
    return f"{member}: {message}" if member else message


def origin_text(origin: Any) -> str:
    return ORIGIN_WORDS.get(str(origin), value_text(origin)) if origin else ""


def failure_text(on_failure: dict[str, Any] | None) -> str:
    """What a control sends when the twin fails, in words: `holds: nothing is sent`,
    `release by release_pump with mode = local`, `the value 0 by set_pump`, each followed
    by `, or after 2 h of old data` when old data also sends it."""
    if not on_failure or not on_failure.get("mode"):
        return ""
    mode = on_failure["mode"]
    params = dict(on_failure.get("parameters") or {})
    if mode == "hold":
        return "holds: nothing is sent"
    if mode == "safe_value":
        value = params.pop("value", None)
        text = f"the value {value_text(value)}"
        if on_failure.get("action"):
            text += f" by {on_failure['action']}"
    else:
        text = f"release by {on_failure.get('action')}"
    if params:
        text += f" with {params_text(params)}"
    if on_failure.get("after_stale"):
        text += f", or after {duration_text(on_failure['after_stale'])} of old data"
    return text


def proposal_lines(record: dict[str, Any]) -> list[str]:
    origin = record.get("origin")
    sent_by = (
        f"sent by the fail-safe of {str(record.get('source') or '').split(':', 1)[-1]}"
        if origin == "failsafe"
        else f"proposed by {record.get('source')}"
    )
    return [
        f"{record.get('action')} on {record.get('object')}: {params_text(record.get('params'))}",
        f"{sent_by} at {time_text(record.get('proposed_at'))}, "
        f"the twin {record.get('fidelity') or 'unknown'}",
        f"status {record_status_text(record.get('status'))}"
        + (f", reconciliation {record['reconciliation']}" if record.get("reconciliation") else ""),
        f"reason given: {record.get('reason')}" if record.get("reason") else "no reason given",
        f"uid {record.get('uid')}, judged at the version {record.get('at_version')}",
    ]


def approval_text(approval: dict[str, Any]) -> str:
    """`approved by operator@plant at 2026-01-05 00:30:00 UTC: looks right`."""
    note = f": {approval['note']}" if approval.get("note") else ""
    when = time_text(approval.get("at"))
    return f"{approval.get('decision')} by {approval.get('by')} at {when}{note}"


def execution_lines(execution: dict[str, Any]) -> list[str]:
    """The external call as the API tells it; it never holds a secret."""
    lines = [
        f"sent at {time_text(execution.get('at'))}",
        f"HTTP status {value_text(execution.get('http_status')) or 'none'}",
        f"tries {value_text(execution.get('attempts'))}",
    ]
    if execution.get("duration_ms") is not None:
        lines.append(f"took {number_text(execution['duration_ms'])} ms")
    if execution.get("effective_at"):
        lines.append(f"effective at {time_text(execution['effective_at'])}")
    if execution.get("error"):
        lines.append(f"error: {execution['error']}")
    if execution.get("excerpt"):
        lines.append(f"answer: {value_text(execution['excerpt'])}")
    if execution.get("idempotency_key"):
        lines.append(f"Idempotency-Key {execution['idempotency_key']}")
    return lines


def observed_lines(observed: dict[str, Any], reconciled_at: str | None) -> list[str]:
    """`observed 11 against expected 20 (difference 9, tolerance 0.5)`, where and when."""
    value, expected = exact_text(observed.get("value")), exact_text(observed.get("expected"))
    difference = exact_text(observed.get("difference"))
    tolerance = exact_text(observed.get("tolerance"))
    lines = [
        f"observed {value} against expected {expected} "
        f"(difference {difference}, tolerance {tolerance})"
    ]
    if observed.get("t"):
        lines.append(f"observed value valid from {time_text(observed['t'])}")
    if observed.get("source") or observed.get("kind"):
        lines.append(f"from {observed.get('source') or '?'} ({observed.get('kind') or '?'})")
    if reconciled_at:
        lines.append(f"reconciled at {time_text(reconciled_at)}")
    return lines


def executor_text(configured: bool) -> str:
    return EXECUTOR_ON if configured else EXECUTOR_OFF


def notifications_status(kind: str | None, count: int) -> str:
    return (
        f"{plural(count, 'notification')}, newest first: the last 200 since the API started, "
        f"sent as {kind or 'unknown'}"
    )
