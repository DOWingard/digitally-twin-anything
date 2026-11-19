"""The fidelity tab: whether the live twin keeps its promises, as of the selected version.

Read while the tab is open, once per selected version, each part by a callback of
its own so that they are read side by side: the state and what the last check
found (`/fidelity`) with the incidents counted by status (`/fidelity/incidents`);
per predictive question its errors (`/fidelity/questions/{id}/errors`, the last
30 days before the selected version unless the range says otherwise), the window
error of each check over the same range (`/fidelity/checks`), the tolerance, the
window and the corrections the breach incidents' steps tell (`/fidelity/incidents`);
the incidents, filtered by status, and every step of the one chosen
(`/fidelity/incidents/{id}`); the signals found suspect (`/fidelity/signals`); the
last 50 checks; the notifications, which the API holds in its memory and tells as
they are now (`/lifecycle/notifications`). The manifest at the version gives the
questions, their unit and the window W.

Fidelity is the live twin's: on a scenario's branch the tab says so and reads
nothing. A live twin whose manifest sets no fidelity interval and window is never
checked; the tab says so and reads nothing more. Nothing here changes the twin.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from dta_core.log import Log
from pydantic import TypeAdapter, ValidationError

from . import ids
from . import text as T
from .backend import Backend
from .feedback import guarded, note
from .paging import read_all, read_capped
from .parts import LIVE_BRANCH, branch_of, client_grid, field

log = Log("ui.fidelity")

TAB = "fidelity"
DEFAULT_DAYS = 30
CHECKS_SHOWN = 50
INCIDENTS_CAP = 10_000
# Bounds on what one chart reads, whatever the range.
ERRORS_CAP = 20_000
CHECKS_CAP = 20_000
TOLERANCE_COLOUR = "#b42318"
POINTS_COLOUR = "#0b62a4"
WINDOW_COLOUR = "#1b6e2d"
CORRECTION_COLOUR = "#7a3fb0"
_DURATION = TypeAdapter(timedelta)


# -- times ---------------------------------------------------------------------------------


def parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        log.warn("a time from the API cannot be read; it is left out", value=text[:40])
        return None


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def plot_time(text: Any) -> str | None:
    """A time as the charts take it: UTC without its zone, `2026-01-05 06:00:00`."""
    moment = parse_time(text)
    return None if moment is None else moment.strftime("%Y-%m-%d %H:%M:%S")


def seconds_of(duration: Any) -> float | None:
    """The seconds of a duration as the manifest gives it: ISO 8601 text or a number."""
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        return float(duration)
    if not isinstance(duration, str) or not duration:
        return None
    try:
        return _DURATION.validate_python(duration).total_seconds()
    except ValidationError:
        log.warn("a duration of the manifest cannot be read", value=duration[:40])
        return None


def range_start(version_t: Any, days: int | None) -> str | None:
    """The data time a chart reads from: `days` before the selected version; None reads
    everything."""
    moment = parse_time(version_t)
    if not days or moment is None:
        return None
    return iso(moment - timedelta(days=int(days)))


# -- what the manifest says of the questions ------------------------------------------------


def predictive_questions(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The contract's predictive questions, the most important first."""
    declared = ((manifest.get("contract") or {}).get("questions")) or {}
    found = [
        (str(name), dict(question or {}))
        for name, question in declared.items()
        if (question or {}).get("kind") == "predictive"
    ]
    ranked = [
        (q.get("priority") if isinstance(q.get("priority"), int) else 1_000_000, name)
        for name, q in found
    ]
    order = {name: rank for rank, name in ranked}
    return sorted(found, key=lambda item: (order[item[0]], item[0]))


def tolerance_of(question: dict[str, Any]) -> tuple[Any, str | None, str | None]:
    """The tolerance ε of a question, its unit and its metric."""
    tolerance = question.get("tolerance") or {}
    return tolerance.get("value"), tolerance.get("unit"), tolerance.get("metric")


def unit_of(manifest: dict[str, Any], subject: str | None) -> str | None:
    declared = ((manifest.get("contract") or {}).get("questions")) or {}
    return tolerance_of(declared.get(subject or "") or {})[1]


# -- the summary ------------------------------------------------------------------------------


def summary_block(
    document: dict[str, Any], counts: dict[str, int], version: dict[str, Any]
) -> html.Div:
    state = str(document.get("state") or "unknown")
    entries = [
        ("state", html.Strong(T.state_text(state), className=f"state-word state-{state}")),
        ("since", T.time_text(document.get("since")) or T.NO_CHECK),
        ("check", T.check_text(document.get("check"))),
        ("incidents", T.counts_text(counts)),
        ("signals", T.signals_text(document.get("suspect_signals") or [])),
    ]
    rows: list[Any] = []
    for name, value in entries:
        rows.append(html.Dt(T.SUMMARY_LABELS[name]))
        rows.append(html.Dd(value, **{"data-field": name}))
    return html.Div(
        [html.Div(T.fidelity_status(version), className="muted"), html.Dl(rows)],
        className="fidelity-summary-block",
    )


# -- the charts --------------------------------------------------------------------------------


def corrections_of(incidents: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """The corrections reported on the breach incidents of a question, oldest first."""
    found = [
        step
        for incident in incidents
        if incident.get("kind") == "breach" and incident.get("subject") == question
        for step in incident.get("steps") or []
        if step.get("action") == "result"
    ]
    return sorted(found, key=lambda step: str(step.get("at")))


def window_line(checks: list[dict[str, Any]], question: str) -> tuple[list[str], list[float]]:
    """The window error each check measured of a question, oldest check first."""
    xs: list[str] = []
    ys: list[float] = []
    for check in sorted(checks, key=lambda found: str(found.get("at"))):
        entry = (check.get("questions") or {}).get(question) or {}
        error = (entry.get("window") or {}).get("error")
        moment = plot_time(check.get("at"))
        if error is None or moment is None:
            continue
        xs.append(moment)
        ys.append(float(error))
    return xs, ys


def error_figure(
    question: str,
    declared: dict[str, Any],
    errors: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    window: Any,
) -> go.Figure:
    """A question's errors: each scored origin a point at the time it was scored (filled
    when it counts in the latest check's window), the window error of each check a line,
    ε a dashed line, the window W of the latest check shaded, the corrections marked."""
    tolerance, unit, metric = tolerance_of(declared)
    figure = go.Figure()
    xs = [plot_time(row.get("scored_at")) for row in errors]
    figure.add_trace(
        go.Scatter(
            x=xs,
            y=[row.get("error") for row in errors],
            mode="markers",
            name=T.POINTS_NAME,
            meta="errors",
            customdata=[
                [T.time_text(row.get("origin")), row.get("n"), row.get("model")] for row in errors
            ],
            hovertemplate="%{y:.4g} · origin %{customdata[0]} · %{customdata[2]}<extra></extra>",
            marker={
                "color": POINTS_COLOUR,
                "size": 7,
                "symbol": ["circle" if row.get("in_window") else "circle-open" for row in errors],
            },
        )
    )
    line_x, line_y = window_line(checks, question)
    figure.add_trace(
        go.Scatter(
            x=line_x,
            y=line_y,
            mode="lines",
            name=T.WINDOW_LINE_NAME,
            meta="window",
            line={"color": WINDOW_COLOUR, "width": 2},
        )
    )
    every = sorted(x for x in [*xs, *line_x] if x)
    if tolerance is not None and every:
        figure.add_trace(
            go.Scatter(
                x=[every[0], every[-1]],
                y=[tolerance, tolerance],
                mode="lines",
                name=T.tolerance_name(tolerance, unit),
                meta="tolerance",
                line={"color": TOLERANCE_COLOUR, "dash": "dash", "width": 2},
            )
        )
    if corrections:
        marks = [plot_time(step.get("at")) for step in corrections]
        figure.add_trace(
            go.Scatter(
                x=marks,
                y=[tolerance if tolerance is not None else 0.0] * len(marks),
                mode="markers",
                name=T.CORRECTIONS_NAME,
                meta="corrections",
                text=[f"{step.get('rung')}: {T.step_outcome(step)}" for step in corrections],
                hovertemplate="%{x} · %{text}<extra></extra>",
                marker={"color": CORRECTION_COLOUR, "size": 11, "symbol": "triangle-up"},
            )
        )
        for mark in marks:
            figure.add_vline(x=mark, line={"color": CORRECTION_COLOUR, "dash": "dot", "width": 1})
    latest = parse_time(checks[0].get("at")) if checks else None
    span = seconds_of(window)
    if latest is not None and span:
        figure.add_vrect(
            x0=(latest - timedelta(seconds=span)).strftime("%Y-%m-%d %H:%M:%S"),
            x1=latest.strftime("%Y-%m-%d %H:%M:%S"),
            fillcolor=WINDOW_COLOUR,
            opacity=0.08,
            layer="below",
            line_width=0,
            annotation_text=T.window_name(window if isinstance(window, str) else None),
            annotation_position="top left",
        )
    figure.update_layout(
        template="simple_white",
        height=320,
        margin={"l": 60, "r": 10, "t": 40, "b": 40},
        title={"text": T.chart_title(question, declared.get("text"), len(errors)), "x": 0.01},
        xaxis_title=T.X_AXIS_TITLE,
        yaxis_title=T.axis_title(metric, unit),
        legend={"orientation": "h", "y": -0.25},
        uirevision=question,
    )
    return figure


def chart(question: str, figure: go.Figure, notes: list[str]) -> html.Div:
    return html.Div(
        [
            dcc.Graph(
                id={"role": ids.FIDELITY_CHART, "question": question},
                figure=figure,
                # Without the button that would upload the chart to an outside service.
                config={
                    "displaylogo": False,
                    "responsive": True,
                    "modeBarButtonsToRemove": ["sendChartToCloud"],
                },
                className="fidelity-chart",
            ),
            *[note(text) for text in notes],
        ],
        className="fidelity-question",
        **{"data-question": question},
    )


# -- the grids ---------------------------------------------------------------------------------


def incident_row(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": incident.get("uid"),
        "kind": incident.get("kind"),
        "subject": incident.get("subject"),
        "status": T.incident_status_text(incident.get("status")),
        "status_key": incident.get("status") or "",
        "rung": incident.get("rung"),
        "attempt": incident.get("attempt"),
        "cycle": incident.get("cycle"),
        "next_cycle_at": T.time_text((incident.get("detail") or {}).get("next_cycle_at")),
        "opened_at": T.time_text(incident.get("opened_at")),
        "resolved_at": T.time_text(incident.get("resolved_at")),
    }


def inferred_text(signal: dict[str, Any]) -> str:
    """The value in force of a suspect signal: the estimate written in place of its
    readings, or the value it had when it was flagged, which stays when nothing estimates
    it. A signal sound again has its readings in force, and none is inferred."""
    if signal.get("status") != "suspect":
        return ""
    estimate = (signal.get("detail") or {}).get("estimate") or {}
    if estimate.get("value") is not None:
        by = estimate.get("model") or estimate.get("question")
        return T.number_text(estimate["value"]) + (f" ({by})" if by else "")
    if signal.get("value_at_flag") is not None:
        return T.value_text(signal["value_at_flag"])
    return ""


def signal_row(signal: dict[str, Any]) -> dict[str, Any]:
    reading = signal.get("reading")
    told = ""
    if reading is not None:
        told = T.value_text(reading)
        if signal.get("reading_at"):
            told += f" ({T.time_text(signal['reading_at'])})"
    return {
        "uid": signal.get("uid"),
        "object": signal.get("object"),
        "property": signal.get("property"),
        "status": signal.get("status"),
        "reason": signal.get("reason"),
        "since": T.time_text(signal.get("since")),
        "reading": told,
        "inferred": inferred_text(signal),
        "clear_checks": signal.get("clear_checks"),
    }


def check_row(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": check.get("uid"),
        "at": T.time_text(check.get("at")),
        "state": T.state_text(check.get("state")),
        "duration": T.milliseconds_text(check.get("duration_ms")),
    }


def notification_row(found: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": found.get("sequence"),
        "type": found.get("type"),
        "subject": found.get("subject"),
        "time": T.time_text(found.get("time")),
        "status": found.get("status"),
        "tries": found.get("tries"),
    }


def incident_columns() -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name, header in T.INCIDENT_COLUMNS.items():
        column: dict[str, Any] = {"field": name, "headerName": header, "tooltipField": name}
        if name == "status":
            column["cellClassRules"] = {
                f"incident-{status}": f"params.data.status_key == '{status}'"
                for status in T.INCIDENT_SIGNS
            }
        if name == "subject":
            column["minWidth"] = 200
        columns.append(column)
    return columns


def plain_columns(headers: dict[str, str], wide: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name, header in headers.items():
        column: dict[str, Any] = {"field": name, "headerName": header, "tooltipField": name}
        if name in wide:
            column["minWidth"] = 200
        columns.append(column)
    return columns


# -- the chosen incident -------------------------------------------------------------------------


def error_cell(value: Any, unit: str | None) -> str:
    return T.measure_text(value, unit) if value is not None else ""


def steps_table(steps: list[dict[str, Any]], unit: str | None) -> html.Table:
    """Every step of an incident in order: when, what was tried, its outcome and the error
    before and after it (the error the last step before it measured, and its own)."""
    rows = []
    before: Any = None
    for step in steps:
        after = step.get("error")
        rows.append(
            html.Tr(
                [
                    html.Td(T.time_text(step.get("at"))),
                    html.Td(step.get("action")),
                    html.Td(T.step_tried(step)),
                    html.Td(T.step_outcome(step)),
                    html.Td(error_cell(before, unit)),
                    html.Td(error_cell(after, unit)),
                    html.Td("" if step.get("cycle") is None else str(step["cycle"])),
                    html.Td(T.time_text(step.get("next_cycle_at"))),
                ],
                className=f"step step-{step.get('action')}",
            )
        )
        if after is not None:
            before = after
    return html.Table(
        [html.Thead(html.Tr([html.Th(label) for label in T.STEP_COLUMNS])), html.Tbody(rows)],
        className="table incident-steps",
    )


def remodel_deadline(incident: dict[str, Any]) -> str | None:
    """When a re-model request fails the incident: when it was asked for plus the timeout
    its step recorded."""
    asked = parse_time(incident.get("requested_at"))
    timeouts = [
        step.get("timeout_s")
        for step in incident.get("steps") or []
        if step.get("action") == "remodel_requested" and step.get("timeout_s") is not None
    ]
    if asked is None or not timeouts:
        return None
    return iso(asked + timedelta(seconds=float(timeouts[-1])))


def remodel_models(detail: dict[str, Any]) -> list[str]:
    models = (detail.get("remodel") or {}).get("models")
    if isinstance(models, dict):
        return sorted(str(name) for name in models)
    if isinstance(models, list):
        return [str(name) for name in models]
    return []


def incident_detail(incident: dict[str, Any], unit: str | None) -> html.Div:
    parts: list[Any] = [html.H5(T.incident_title(incident), className="incident-title")]
    if incident.get("status") == "remodel_requested":
        detail = incident.get("detail") or {}
        parts.append(
            html.Div(
                T.remodel_text(
                    detail.get("spec"), remodel_deadline(incident), remodel_models(detail)
                ),
                className="notice remodel",
            )
        )
    parts.append(steps_table(list(incident.get("steps") or []), unit))
    return html.Div(parts, className="incident-detail-block", **{"data-uid": incident.get("uid")})


# -- layout and callbacks --------------------------------------------------------------------------


def range_options() -> list[dict[str, Any]]:
    return [{"label": T.range_text(days), "value": days} for days in T.RANGE_OPTIONS] + [
        {"label": T.RANGE_ALL, "value": 0}
    ]


def layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id=ids.FIDELITY_SHOWN),
            dcc.Store(id=ids.INCIDENT_CHOSEN),
            html.H3(T.FIDELITY_HEADING),
            html.Div(id=ids.FIDELITY_FEEDBACK),
            html.Div(id=ids.FIDELITY_SUMMARY, className="fidelity-summary"),
            html.H4(T.CHARTS_HEADING),
            html.Div(
                field(
                    T.RANGE_LABEL,
                    dcc.Dropdown(
                        id=ids.FIDELITY_RANGE,
                        options=range_options(),
                        value=DEFAULT_DAYS,
                        clearable=False,
                        searchable=False,
                    ),
                ),
                className="filters",
            ),
            html.Div(T.POINTS_HELP, className="muted"),
            html.Div(id=ids.FIDELITY_CHARTS_FEEDBACK),
            html.Div(id=ids.FIDELITY_CHARTS, className="fidelity-charts"),
            html.H4(T.INCIDENTS_HEADING),
            html.Div(
                field(
                    T.STATUS_FILTER_LABEL,
                    dcc.Dropdown(
                        id=ids.INCIDENTS_STATUS,
                        options=[],
                        placeholder=T.FILTER_ANY,
                        searchable=False,
                    ),
                ),
                className="filters",
            ),
            html.Div(id=ids.INCIDENTS_FEEDBACK),
            client_grid(ids.INCIDENTS_GRID, incident_columns(), "uid", class_name="incidents-grid"),
            html.Div(id=ids.INCIDENT_FEEDBACK),
            html.Div(note(T.CHOOSE_INCIDENT), id=ids.INCIDENT_DETAIL, className="incident-detail"),
            html.H4(T.SIGNALS_HEADING),
            html.Div(id=ids.SIGNALS_FEEDBACK),
            client_grid(
                ids.SIGNALS_GRID,
                plain_columns(T.SIGNAL_COLUMNS, wide=("object", "reading", "inferred")),
                "uid",
                height="24vh",
                class_name="signals-grid",
            ),
            html.H4(T.CHECKS_HEADING),
            html.Div(id=ids.CHECKS_FEEDBACK),
            client_grid(
                ids.CHECKS_GRID,
                plain_columns(T.CHECK_COLUMNS),
                "uid",
                height="28vh",
                class_name="checks-grid",
            ),
            html.H4(T.NOTIFICATIONS_HEADING),
            html.Div(id=ids.NOTIFICATIONS_STATUS, className="muted"),
            html.Div(id=ids.NOTIFICATIONS_FEEDBACK),
            client_grid(
                ids.NOTIFICATIONS_GRID,
                plain_columns(T.NOTIFICATION_COLUMNS, wide=("type", "subject")),
                "sequence",
                height="28vh",
                class_name="notifications-grid",
            ),
        ],
        className="fidelity-tab",
    )


def unreadable(shown: dict[str, Any]) -> str | None:
    """Why the tab reads nothing at what is shown; None when it reads."""
    if shown.get("branch") != LIVE_BRANCH:
        return T.LIVE_ONLY
    if not shown.get("fidelity"):
        return T.NO_FIDELITY
    return None


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.FIDELITY_SHOWN, "data"),
        Input(ids.TABS, "value"),
        Input(ids.VERSION, "data"),
        Input(ids.FEATURES, "data"),
        State(ids.FIDELITY_SHOWN, "data"),
    )
    def gate(
        tab: str | None,
        version: dict[str, Any] | None,
        features: dict[str, Any] | None,
        shown: dict[str, Any] | None,
    ) -> Any:
        """What the tab shows: the selected version, while the tab is open and once the
        twin's tabs are known. Every part reads again when it changes."""
        if tab != TAB or not version or not features or not features.get("live"):
            raise PreventUpdate
        key = {
            "at": str(version["id"]),
            "seq": version.get("seq"),
            "t": version.get("t"),
            "branch": branch_of(version),
            "fidelity": bool(features.get("fidelity")),
        }
        if key == shown:
            raise PreventUpdate
        log.debug("the fidelity tab shows another version", at=key["at"], branch=key["branch"])
        return key

    @app.callback(
        Output(ids.FIDELITY_SUMMARY, "children"),
        Output(ids.FIDELITY_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
    )
    @guarded(1, fallback=(None,))
    def summary(shown: dict[str, Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        why = unreadable(shown)
        if why:
            return note(why, warn=True)
        started = time.perf_counter()
        where = {"at": shown["at"]}
        document = backend.client.get("/fidelity", params=where)
        incidents = read_all(
            backend.client, "/fidelity/incidents", where, cap=INCIDENTS_CAP, page=1_000
        )
        counts = dict(Counter(str(found.get("status")) for found in incidents))
        log.info(
            "fidelity summary read",
            at=shown["at"],
            state=document.get("state"),
            incidents=len(incidents),
            duration_ms=_ms(started),
        )
        return summary_block(document, counts, shown)

    @app.callback(
        Output(ids.FIDELITY_CHARTS, "children"),
        Output(ids.FIDELITY_CHARTS_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
        Input(ids.FIDELITY_RANGE, "value"),
    )
    @guarded(1, fallback=(None,))
    def charts(shown: dict[str, Any] | None, days: int | None) -> Any:
        """Per predictive question its errors over the range, read at the version."""
        if not shown:
            raise PreventUpdate
        if unreadable(shown):
            return None
        started = time.perf_counter()
        manifest = backend.manifest(shown["at"])
        questions = predictive_questions(manifest)
        if not questions:
            return note(T.NO_PREDICTIVE)
        since = range_start(shown.get("t"), days)
        where: dict[str, Any] = {"at": shown["at"], "from": since}
        checks, checks_cut = read_capped(backend.client, "/fidelity/checks", where, cap=CHECKS_CAP)
        breaches = read_all(
            backend.client,
            "/fidelity/incidents",
            {"at": shown["at"], "kind": "breach"},
            cap=INCIDENTS_CAP,
            page=1_000,
        )
        window = (manifest.get("fidelity") or {}).get("window")
        drawn = []
        points = 0
        for question, declared in questions:
            errors, cut = read_capped(
                backend.client,
                "/fidelity/questions/{id}/errors",
                where,
                cap=ERRORS_CAP,
                path={"id": question},
            )
            points += len(errors)
            notes = []
            if not errors:
                notes.append(T.no_errors_text(question))
            if cut:
                notes.append(T.chart_cut_text(question, len(errors)))
            if checks_cut:
                notes.append(T.checks_cut_text(len(checks)))
            figure = error_figure(
                question, declared, errors, checks, corrections_of(breaches, question), window
            )
            drawn.append(chart(question, figure, notes))
        log.info(
            "fidelity charts read",
            at=shown["at"],
            questions=len(questions),
            points=points,
            checks=len(checks),
            days=days,
            duration_ms=_ms(started),
        )
        return drawn

    @app.callback(
        Output(ids.INCIDENTS_GRID, "rowData"),
        Output(ids.INCIDENTS_STATUS, "options"),
        Output(ids.INCIDENTS_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
        Input(ids.INCIDENTS_STATUS, "value"),
        State(ids.INCIDENTS_STATUS, "options"),
    )
    @guarded(2, fallback=([], no_update))
    def incidents(
        shown: dict[str, Any] | None, status: str | None, options: list[Any] | None
    ) -> Any:
        if not shown:
            raise PreventUpdate
        if unreadable(shown):
            return [], no_update
        started = time.perf_counter()
        params = {"at": shown["at"], "status": status or None}
        found = read_all(
            backend.client, "/fidelity/incidents", params, cap=INCIDENTS_CAP, page=1_000
        )
        offered = no_update if options else backend.enum("/fidelity/incidents", "status")
        log.info(
            "incidents read",
            at=shown["at"],
            status=status,
            incidents=len(found),
            duration_ms=_ms(started),
        )
        return [incident_row(incident) for incident in found], offered

    @app.callback(
        Output(ids.INCIDENT_CHOSEN, "data"),
        Input(ids.INCIDENTS_GRID, "cellClicked"),
        State(ids.INCIDENT_CHOSEN, "data"),
        prevent_initial_call=True,
    )
    def choose_incident(clicked: dict[str, Any] | None, chosen: str | None) -> Any:
        uid = clicked.get("rowId") if isinstance(clicked, dict) else None
        if not uid or uid == chosen:
            raise PreventUpdate
        log.debug("an incident was chosen")
        return str(uid)

    @app.callback(
        Output(ids.INCIDENT_DETAIL, "children"),
        Output(ids.INCIDENT_FEEDBACK, "children"),
        Input(ids.INCIDENT_CHOSEN, "data"),
        Input(ids.FIDELITY_SHOWN, "data"),
    )
    @guarded(1, fallback=(None,))
    def show_incident(chosen: str | None, shown: dict[str, Any] | None) -> Any:
        """Every step of the incident chosen, read at the version shown; read again when
        another version is shown, where it may be gone."""
        if not shown or unreadable(shown):
            raise PreventUpdate
        if not chosen:
            if ctx.triggered_id == ids.INCIDENT_CHOSEN:
                raise PreventUpdate
            return note(T.CHOOSE_INCIDENT)
        started = time.perf_counter()
        found = backend.client.get(
            "/fidelity/incidents/{id}", path={"id": chosen}, params={"at": shown["at"]}
        )["data"]
        unit = unit_of(backend.manifest(shown["at"]), found.get("subject"))
        log.info(
            "an incident was read with its steps",
            status=found.get("status"),
            steps=len(found.get("steps") or []),
            duration_ms=_ms(started),
        )
        return incident_detail(found, unit)

    @app.callback(
        Output(ids.SIGNALS_GRID, "rowData"),
        Output(ids.SIGNALS_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
    )
    @guarded(1, fallback=([],))
    def signals(shown: dict[str, Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        if unreadable(shown):
            return []
        started = time.perf_counter()
        found = backend.client.get("/fidelity/signals", params={"at": shown["at"]})["data"]
        log.info("signals read", at=shown["at"], signals=len(found), duration_ms=_ms(started))
        return [signal_row(signal) for signal in found]

    @app.callback(
        Output(ids.CHECKS_GRID, "rowData"),
        Output(ids.CHECKS_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
    )
    @guarded(1, fallback=([],))
    def checks(shown: dict[str, Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        if unreadable(shown):
            return []
        started = time.perf_counter()
        found = backend.client.get(
            "/fidelity/checks", params={"at": shown["at"], "limit": CHECKS_SHOWN}
        )["data"]
        log.info("checks read", at=shown["at"], checks=len(found), duration_ms=_ms(started))
        return [check_row(check) for check in found]

    @app.callback(
        Output(ids.NOTIFICATIONS_GRID, "rowData"),
        Output(ids.NOTIFICATIONS_STATUS, "children"),
        Output(ids.NOTIFICATIONS_FEEDBACK, "children"),
        Input(ids.FIDELITY_SHOWN, "data"),
    )
    @guarded(2, fallback=([], ""))
    def notifications(shown: dict[str, Any] | None) -> Any:
        """What the API told, as it holds it now: not of a version."""
        if not shown:
            raise PreventUpdate
        if unreadable(shown):
            return [], ""
        started = time.perf_counter()
        document = backend.client.get("/lifecycle/notifications")
        found = document.get("data") or []
        log.info("notifications read", notifications=len(found), duration_ms=_ms(started))
        return (
            [notification_row(item) for item in found],
            T.notifications_status(document.get("kind"), len(found)),
        )


__all__ = ["TAB", "layout", "register"]
