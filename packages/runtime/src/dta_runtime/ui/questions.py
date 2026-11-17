"""The questions tab: what each question of the contract may claim, and asking one.

The table is read when the tab is open, once per selected version: the
questions' statuses and measures from `/questions`, their text, kind and
priority from the manifest in force at that version. Choosing a question
reads its function and offers the arguments a caller may give (those its
registration does not fix), with the version and branch the session is at.
Nothing is asked until the operator presses Ask; a refusal is shown beside
the form, which keeps what was typed.
"""

from __future__ import annotations

import time
from typing import Any

import dash_ag_grid as dag
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import answers, ids
from . import text as T
from .backend import Backend
from .client import ApiProblem
from .feedback import guarded, note, problem_box
from .paging import read_all
from .parts import LIVE_BRANCH, at_version, branch_of, field

log = Log("ui.questions")

TAB = "questions"
# Kinds of argument given as a list, typed comma-separated.
LIST_KINDS = ("numbers", "uids", "types", "filters", "edge_filters")
NUMBER_KINDS = ("number", "integer")
# Priority of a question the manifest gives none: after every other.
UNRANKED = 1_000_000


def question_rows(questions: list[dict[str, Any]], stated: dict[str, Any]) -> list[dict[str, Any]]:
    """The table's rows: the API's questions with what the manifest states of each, the most
    important first."""
    rows = []
    for found in questions:
        manifest = stated.get(str(found.get("id"))) or {}
        tolerance = manifest.get("tolerance") or {}
        unit = found.get("unit") or tolerance.get("unit")
        metric = found.get("metric") or tolerance.get("metric")
        priority = manifest.get("priority")
        status = found.get("status")
        rows.append(
            {
                "id": found.get("id"),
                "priority": priority,
                "rank": priority if isinstance(priority, int) else UNRANKED,
                "text": manifest.get("text") or "",
                "kind": manifest.get("kind") or "",
                "status": T.status_text(status),
                "status_key": status or "",
                "reason": found.get("reason") or "",
                "error": T.measure_text(found.get("measured_error"), unit, metric),
                "tolerance": T.tolerance_text(
                    found.get("tolerance", tolerance.get("value")), unit, metric
                ),
                "basis": found.get("basis") or "",
                "function": found.get("function") or manifest.get("function") or "",
                "model": found.get("model") or "",
                "measured_at": T.time_text(found.get("measured_at")),
            }
        )
    rows.sort(key=lambda row: (row["rank"], str(row["id"])))
    return rows


def column_defs() -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name, header in T.QUESTION_COLUMNS.items():
        column: dict[str, Any] = {"field": name, "headerName": header, "tooltipField": name}
        if name == "status":
            # Coloured as well as told in a word and a sign.
            column["cellClassRules"] = {
                f"status-{status}": f"params.data.status_key == '{status}'"
                for status in T.STATUS_SIGNS
            }
        if name in ("text", "reason"):
            column["minWidth"] = 220
        columns.append(column)
    return columns


def layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id=ids.QUESTIONS_SHOWN),
            dcc.Store(id=ids.QUESTION_CHOSEN),
            html.Div(
                [
                    html.H3(T.QUESTIONS_HEADING),
                    html.Span(id=ids.QUESTIONS_STATUS, className="status"),
                ],
                className="questions-heading",
            ),
            html.Div(id=ids.QUESTIONS_FEEDBACK),
            dag.AgGrid(
                id=ids.QUESTIONS_GRID,
                rowData=[],
                columnDefs=column_defs(),
                getRowId="params.data.id",
                defaultColDef={
                    "resizable": True,
                    "sortable": True,
                    "cellDataType": False,
                    "minWidth": 100,
                    "flex": 1,
                },
                dashGridOptions={"tooltipShowDelay": 300, "domLayout": "autoHeight"},
                className="questions-grid",
                style={"width": "100%"},
            ),
            html.Div(
                [
                    html.Section(
                        [
                            html.H4(T.ASK_HEADING),
                            html.Div(
                                T.CHOOSE_QUESTION, id=ids.QUESTION_TITLE, className="question-title"
                            ),
                            html.Div(id=ids.QUESTION_FORM_FEEDBACK),
                            html.Div(id=ids.QUESTION_FORM, className="question-form"),
                            html.Div(
                                [
                                    field(
                                        T.AT_LABEL,
                                        dcc.Input(
                                            id=ids.QUESTION_AT,
                                            type="text",
                                            placeholder=T.AT_PLACEHOLDER,
                                            debounce=False,
                                        ),
                                    ),
                                    field(
                                        T.BRANCH_LABEL,
                                        dcc.Input(
                                            id=ids.QUESTION_BRANCH, type="text", debounce=False
                                        ),
                                    ),
                                ],
                                className="question-where",
                            ),
                            html.Button(
                                T.ASK, id=ids.ASK, n_clicks=0, disabled=True, className="button"
                            ),
                            html.Div(id=ids.ASK_FEEDBACK, className="ask-feedback"),
                        ],
                        className="ask-box",
                    ),
                    html.Section(
                        [
                            html.H4(T.ANSWER_HEADING),
                            html.Div(id=ids.ANSWER_FEEDBACK),
                            dcc.Loading(
                                html.Div(note(T.NOT_ASKED), id=ids.ANSWER, className="answer"),
                                type="dot",
                            ),
                        ],
                        className="answer-box",
                    ),
                ],
                className="ask-row",
            ),
        ],
        className="questions-tab",
    )


def argument_field(question: str, parameter: dict[str, Any]) -> html.Div:
    """One argument of the form: a field fit for its kind, labelled with its name, kind and
    whether it is required; a default is offered as the field's hint."""
    name, kind = str(parameter["name"]), parameter.get("kind")
    control_id = {"role": ids.QUESTION_ARG, "question": question, "name": name, "kind": kind or ""}
    hint = T.default_text(parameter.get("default"))
    if kind == "boolean":
        control: Any = dcc.Dropdown(
            id=control_id,
            options=[
                {"label": T.CHOICE_TRUE, "value": True},
                {"label": T.CHOICE_FALSE, "value": False},
            ],
            placeholder=hint or None,
            clearable=True,
        )
    elif kind == "choice" and parameter.get("choices"):
        control = dcc.Dropdown(
            id=control_id,
            options=[{"label": str(choice), "value": choice} for choice in parameter["choices"]],
            placeholder=hint or None,
            clearable=True,
        )
    else:
        if kind == "version":
            placeholder = T.AT_PLACEHOLDER
        elif kind in LIST_KINDS:
            placeholder = T.LIST_PLACEHOLDER
        else:
            placeholder = ""
        placeholder = " · ".join(part for part in (hint, placeholder) if part)
        control = dcc.Input(
            id=control_id,
            type="text",
            placeholder=placeholder,
            inputMode="decimal" if kind in NUMBER_KINDS else "text",
            debounce=False,
        )
    label_text = T.argument_label(name, kind, bool(parameter.get("required")))
    parts: list[Any] = [html.Label(label_text), control]
    if parameter.get("description"):
        parts.append(html.Div(parameter["description"], className="muted argument-help"))
    return html.Div(parts, className="field argument", **{"data-argument": name})


def form(question: str, document: dict[str, Any] | None) -> list[Any]:
    """The arguments a caller may give the function of a question."""
    if document is None:
        return [note(T.NO_FUNCTION)]
    free = [found for found in document.get("parameters") or [] if not found.get("fixed")]
    if free:
        return [argument_field(question, parameter) for parameter in free]
    if document.get("kind") == "registered":
        return [note(T.CODE_ARGUMENTS)]
    return [note(T.NO_ARGUMENTS)]


def bad_argument(name: str, kind: str | None, raw: Any) -> ApiProblem:
    return ApiProblem(
        code="ui.bad_argument",
        title=T.BAD_ARGUMENT,
        detail=T.bad_argument_text(name, kind, raw),
    )


def _number(text: str, integer: bool) -> int | float:
    value = float(text)
    if integer:
        if not value.is_integer():
            raise ValueError(text)
        return int(value)
    return int(value) if value.is_integer() and "." not in text else value


def argument_value(name: str, kind: str | None, raw: Any) -> Any:
    """What a field holds, as the API takes it; None when it is left empty, so that the
    function's default applies."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        return raw
    typed = raw.strip()
    try:
        if kind in LIST_KINDS:
            items = [part.strip() for part in typed.split(",") if part.strip()]
            if kind == "numbers":
                return [_number(item, integer=False) for item in items]
            return items
        if kind in NUMBER_KINDS:
            return _number(typed, integer=kind == "integer")
    except ValueError as exc:
        raise bad_argument(name, kind, raw) from exc
    return typed


def arguments(question: str, ids_seen: list[dict[str, Any]], values: list[Any]) -> dict[str, Any]:
    """The body of an ask: every field of the chosen question's form that is filled in."""
    body: dict[str, Any] = {}
    for control_id, raw in zip(ids_seen, values, strict=False):
        if control_id.get("question") != question:
            continue
        name, kind = str(control_id["name"]), control_id.get("kind") or None
        value = argument_value(name, kind, raw)
        if value is not None:
            body[name] = value
    return body


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.QUESTIONS_GRID, "rowData"),
        Output(ids.QUESTIONS_STATUS, "children"),
        Output(ids.QUESTIONS_SHOWN, "data"),
        Output(ids.QUESTIONS_FEEDBACK, "children"),
        Input(ids.TABS, "value"),
        Input(ids.VERSION, "data"),
        State(ids.QUESTIONS_SHOWN, "data"),
    )
    @guarded(3)
    def show_questions(tab: str | None, version: dict[str, Any] | None, shown: Any) -> Any:
        """The table, read when the tab is open and the selected version is not the one it
        was last read at."""
        if tab != TAB or not version:
            raise PreventUpdate
        where = at_version(version)
        key = f"{where['branch']}@{where['at']}"
        if shown == key:
            raise PreventUpdate
        started = time.perf_counter()
        questions = read_all(backend.client, "/questions", where)
        manifest = backend.client.get("/meta/manifest", params=where)
        stated = ((manifest.get("manifest") or {}).get("contract") or {}).get("questions") or {}
        rows = question_rows(questions, stated)
        log.info(
            "questions read",
            version=version["id"],
            branch=where["branch"],
            questions=len(rows),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        label = T.on_branch_text(where["branch"], version.get("scenario"))
        return rows, T.questions_status(len(rows), version, label), key

    @app.callback(
        Output(ids.QUESTION_CHOSEN, "data"),
        Output(ids.QUESTION_TITLE, "children"),
        Output(ids.QUESTION_FORM, "children"),
        Output(ids.ASK, "disabled"),
        Output(ids.ANSWER, "children", allow_duplicate=True),
        Output(ids.ASK_FEEDBACK, "children", allow_duplicate=True),
        Output(ids.QUESTION_FORM_FEEDBACK, "children"),
        Input(ids.QUESTIONS_GRID, "cellClicked"),
        State(ids.QUESTIONS_GRID, "rowData"),
        State(ids.QUESTION_CHOSEN, "data"),
        State(ids.VERSION, "data"),
        prevent_initial_call=True,
    )
    @guarded(6)
    def choose(
        clicked: dict[str, Any] | None,
        rows: list[dict[str, Any]] | None,
        chosen: dict[str, Any] | None,
        version: dict[str, Any] | None,
    ) -> Any:
        """The form of the question clicked: its function's arguments, read once per click
        on another question. The answer and refusal shown were the last question's, and go."""
        if not clicked or not version:
            raise PreventUpdate
        question = str(clicked.get("rowId") or "")
        row = next((found for found in rows or [] if str(found.get("id")) == question), None)
        if row is None:
            raise PreventUpdate
        if chosen and chosen.get("id") == question:
            raise PreventUpdate
        title = T.question_title(question, row.get("text"))
        function = row.get("function") or None
        picked = {"id": question, "function": function, "entry": None}
        cleared = (note(T.NOT_ASKED), None)
        if not function:
            return picked, title, form(question, None), False, *cleared
        try:
            document = backend.client.get(
                "/functions/{id}", path={"id": function}, params=at_version(version)
            )["data"]
        except ApiProblem as problem:
            # Shown where the form would be; Ask stays possible, and the API says why it
            # cannot answer when asked.
            log.warn(
                "the function of a question cannot be read; its form is left empty",
                question=question,
                function=function,
                code=problem.code,
            )
            return picked, title, [problem_box(problem)], False, *cleared
        picked["entry"] = document.get("entry")
        log.debug(
            "a question was chosen",
            question=question,
            function=function,
            arguments=[
                found["name"]
                for found in document.get("parameters") or []
                if not found.get("fixed")
            ],
        )
        return picked, title, form(question, document), False, *cleared

    @app.callback(
        Output(ids.QUESTION_AT, "value"),
        Output(ids.QUESTION_BRANCH, "value"),
        Input(ids.VERSION, "data"),
    )
    def fill_where(version: dict[str, Any] | None) -> Any:
        """The form asks at the session's version and branch unless told otherwise."""
        if not version:
            raise PreventUpdate
        return f"#{version['seq']}", branch_of(version)

    @app.callback(
        Output(ids.ANSWER, "children"),
        Output(ids.ASK_FEEDBACK, "children"),
        Input(ids.ASK, "n_clicks"),
        State(ids.QUESTION_CHOSEN, "data"),
        State({"role": ids.QUESTION_ARG, "question": ALL, "name": ALL, "kind": ALL}, "id"),
        State({"role": ids.QUESTION_ARG, "question": ALL, "name": ALL, "kind": ALL}, "value"),
        State(ids.QUESTION_AT, "value"),
        State(ids.QUESTION_BRANCH, "value"),
        State(ids.VERSION, "data"),
        running=[(Output(ids.ASK, "disabled"), True, False)],
        prevent_initial_call=True,
    )
    @guarded(1, fallback=[[]])
    def ask(
        clicks: int | None,
        chosen: dict[str, Any] | None,
        controls: list[dict[str, Any]],
        values: list[Any],
        at: str | None,
        branch: str | None,
        version: dict[str, Any] | None,
    ) -> Any:
        if not clicks or not chosen or ctx.triggered_id != ids.ASK:
            raise PreventUpdate
        question = str(chosen["id"])
        body = arguments(question, controls, values)
        params = {
            "at": (at or "").strip() or "head",
            "branch": (branch or "").strip() or LIVE_BRANCH,
        }
        document, key = answers.ask(
            backend, question, params, body, whole=chosen.get("entry") == "forecast"
        )
        answered_on = (document.get("version") or {}).get("branch")
        # The session's scenario names the branch only when the answer was read on it.
        same = answered_on == branch_of(version)
        scenario = (version or {}).get("scenario") if same else None
        return answers.view(document, key, T.on_branch_text(answered_on, scenario))


__all__ = ["layout", "register"]
