"""The history tab: the commit log, what the selected version changed, sync runs, fidelity and logs.

Clicking a row of the commit log selects that version for the whole page,
as the header's slider does. The changes shown are those of the selected
version against the one before it. Sync runs are the twin's own
`TwinSyncRun` records as of the selected version.
"""

from __future__ import annotations

import json
from typing import Any

import dash_ag_grid as dag
from dash import MATCH, Dash, Input, Output, State, ctx, dcc, html, set_props
from dash.exceptions import PreventUpdate
from dta_core.log import Log, new_trace_id, trace

from . import ids
from .backend import Backend
from .client import ApiProblem
from .feedback import guarded, internal_problem, note, problem_box
from .schema import Schema
from .text import cell, plural, time_text, value_text, with_unit

log = Log("ui.history")

SYNC_RUN_TYPE = "TwinSyncRun"
LOG_BLOCK_ROWS = 100
DIFF_ROWS = 100
RUNS_SHOWN = 50
LOG_LINES = 200
# The twin's own record of a run: what matters most to an operator comes first.
RUN_COLUMNS_FIRST = (
    "status",
    "kind",
    "started_at",
    "finished_at",
    "rows_read",
    "rows_written",
    "rows_rejected",
    "versions_written",
    "error",
)


def field(label: str, control: Any) -> html.Div:
    return html.Div([html.Label(label, htmlFor=control.id), control], className="field")


def layout() -> html.Div:
    return html.Div(
        [
            html.Div(
                ["Fidelity: ", html.Strong(id=ids.FIDELITY, children="unknown")],
                className="fidelity",
            ),
            html.H2("Commit log"),
            html.Div(
                [
                    field("Cause", dcc.Dropdown(id=ids.LOG_CAUSE, options=[], placeholder="any")),
                    field(
                        "Actor",
                        dcc.Input(id=ids.LOG_ACTOR, type="text", debounce=True, placeholder="any"),
                    ),
                    field(
                        "From (selector or time)",
                        dcc.Input(id=ids.LOG_FROM, type="text", debounce=True, placeholder="#0"),
                    ),
                    field(
                        "To (selector or time)",
                        dcc.Input(id=ids.LOG_TO, type="text", debounce=True, placeholder="head"),
                    ),
                ],
                className="filters",
            ),
            html.Div(id=ids.LOG_FEEDBACK),
            html.Div(id=ids.LOG_BOX),
            html.H2("Changes in the selected version"),
            html.Div(id=ids.DIFF_FEEDBACK),
            html.Div(id=ids.DIFF),
            html.H2("Sync runs"),
            html.Div(id=ids.RUNS_FEEDBACK),
            html.Div(id=ids.RUNS),
            html.H2("Logs"),
            html.Div(
                [
                    field(
                        "Level or more severe",
                        dcc.Dropdown(id=ids.LOGS_LEVEL, options=[], placeholder="any"),
                    ),
                    field(
                        "Context starts with",
                        dcc.Input(
                            id=ids.LOGS_CONTEXT, type="text", debounce=True, placeholder="any"
                        ),
                    ),
                    html.Div(
                        html.Button("Refresh", id=ids.LOGS_REFRESH, n_clicks=0, className="button"),
                        className="field",
                    ),
                ],
                className="filters",
            ),
            html.Div(id=ids.LOGS_FEEDBACK),
            dag.AgGrid(
                id=ids.LOGS_GRID,
                rowData=[],
                columnDefs=[
                    {"field": "timestamp", "width": 230},
                    {"field": "level", "width": 90},
                    {"field": "service", "width": 110},
                    {"field": "context", "width": 160},
                    {"field": "message", "flex": 2, "minWidth": 260},
                    {"field": "trace_id", "width": 290},
                    {"field": "error", "flex": 1, "minWidth": 160},
                    {"field": "data", "flex": 1, "minWidth": 200},
                ],
                defaultColDef={"resizable": True, "sortable": True, "cellDataType": False},
                dashGridOptions={"tooltipShowDelay": 300, "enableCellTextSelection": True},
                style={"height": "40vh", "width": "100%"},
            ),
        ],
        className="history-tab",
    )


def log_grid(query: dict[str, Any]) -> dag.AgGrid:
    return dag.AgGrid(
        id={"role": ids.COMMIT_LOG, "query": json.dumps(query, sort_keys=True)},
        rowModelType="infinite",
        columnDefs=[
            {"field": "seq", "width": 90},
            {"field": "t", "headerName": "time", "width": 210},
            {"field": "cause", "width": 110},
            {"field": "actor", "width": 140},
            {"field": "note", "flex": 1, "minWidth": 160},
            {"field": "run", "width": 120},
            {"field": "backfill", "width": 100},
            {"field": "written_at", "headerName": "written at", "width": 250},
            {"field": "id", "width": 290},
        ],
        defaultColDef={"resizable": True, "sortable": False, "cellDataType": False},
        dashGridOptions={
            "cacheBlockSize": LOG_BLOCK_ROWS,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            "rowSelection": {
                "mode": "singleRow",
                "checkboxes": False,
                "enableClickSelection": True,
            },
        },
        # Rows are identified by seq, so a click names the version it landed on.
        getRowId="String(params.data.seq)",
        className="commit-log",
        style={"height": "32vh", "width": "100%"},
    )


def version_row(entry: dict[str, Any]) -> dict[str, Any]:
    extra = entry.get("extra") or {}
    return {
        "seq": entry["seq"],
        "t": time_text(entry.get("t")),
        "cause": entry.get("cause"),
        "actor": entry.get("actor"),
        "note": entry.get("note"),
        "run": entry.get("run"),
        "backfill": "yes" if extra.get("backfill") is True else "",
        "written_at": time_text(entry.get("written_at")),
        "id": entry["id"],
    }


def _values_text(
    schema: Schema, type_name: str, values: dict[str, Any], names: list[str]
) -> list[str]:
    return [
        f"{name}: {with_unit(value_text(values.get(name)), schema.unit(type_name, name))}"
        for name in names
    ]


def change_text(schema: Schema, type_name: str, row: dict[str, Any]) -> str:
    """What changed in one row of a diff: before and after for a modified object,
    the values of an added one, the last values of a removed one."""
    before: dict[str, Any] = row.get("_before") or {}
    names = [name for name in row if name not in ("_change", "uid", "_before")]
    change = row.get("_change")
    if change == "modified":
        parts = [
            f"{name}: {value_text(before.get(name))} → "
            f"{with_unit(value_text(row.get(name)), schema.unit(type_name, name))}"
            for name in names
            if before.get(name) != row.get(name)
        ]
    elif change == "removed":
        parts = _values_text(
            schema, type_name, before, [name for name in names if before.get(name) is not None]
        )
    else:
        parts = _values_text(
            schema, type_name, row, [name for name in names if row.get(name) is not None]
        )
    return "; ".join(parts)


def diff_view(
    schema: Schema, document: dict[str, Any], version: dict[str, Any], base: str
) -> list[Any]:
    summary = document.get("summary", [])
    if not summary:
        return [note(f"Version #{version['seq']} changed no object.")]
    counts = {entry["type"]: entry for entry in summary}
    parts: list[Any] = [
        html.Div(
            f"#{version['seq']} against {base}: "
            + ", ".join(
                f"{entry['type']} +{entry['added']} ~{entry['modified']} -{entry['removed']}"
                for entry in summary
            ),
            className="muted",
        ),
        html.Table(
            [
                html.Thead(
                    html.Tr([html.Th(label) for label in ("type", "added", "modified", "removed")])
                ),
                html.Tbody(
                    [
                        html.Tr(
                            [
                                html.Td(entry["type"]),
                                html.Td(entry["added"]),
                                html.Td(entry["modified"]),
                                html.Td(entry["removed"]),
                            ]
                        )
                        for entry in summary
                    ]
                ),
            ],
            className="table diff-summary",
        ),
    ]
    truncated = document.get("truncated") or {}
    for type_name, rows in (document.get("rows") or {}).items():
        total = sum(
            counts.get(type_name, {}).get(key, 0) for key in ("added", "modified", "removed")
        )
        heading = f"{type_name}: {plural(total, 'changed object')}"
        if truncated.get(type_name):
            heading += f", the first {len(rows)} shown"
        parts.append(html.H3(heading))
        parts.append(
            html.Table(
                [
                    html.Thead(html.Tr([html.Th("change"), html.Th("uid"), html.Th("values")])),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(
                                        row.get("_change"), className=f"change {row.get('_change')}"
                                    ),
                                    html.Td(html.Code(row.get("uid"))),
                                    html.Td(change_text(schema, type_name, row)),
                                ]
                            )
                            for row in rows
                        ]
                    ),
                ],
                className="table diff-rows",
                **{"data-type": type_name},
            )
        )
    return parts


def runs_view(schema: Schema, document: dict[str, Any], version: dict[str, Any]) -> list[Any]:
    rows = document.get("data", [])
    if not rows:
        return [note(f"No sync run is recorded as of #{version['seq']}.")]
    properties = {prop["name"]: prop for prop in schema.properties(SYNC_RUN_TYPE)}
    first = [name for name in RUN_COLUMNS_FIRST if name in properties]
    names = ["uid", *first, *(name for name in properties if name not in first)]

    def shown(name: str, value: Any) -> Any:
        if name in properties and properties[name]["kind"] == "datetime":
            return time_text(value)
        return cell(value)

    heading = f"{plural(len(rows), 'sync run')} as of #{version['seq']}, latest first"
    if document.get("next_cursor"):
        heading = f"The {len(rows)} latest sync runs as of #{version['seq']}"
    return [
        html.Div(heading, className="muted"),
        dag.AgGrid(
            id=ids.RUNS_GRID,
            rowData=[{name: shown(name, row.get(name)) for name in names} for row in rows],
            columnDefs=[{"field": name, "headerName": name} for name in names],
            defaultColDef={"resizable": True, "sortable": True, "cellDataType": False},
            # Few rows and columns: all are drawn, so every value is in the page.
            dashGridOptions={"suppressColumnVirtualisation": True},
            style={"height": f"{min(60 + 42 * len(rows), 360)}px", "width": "100%"},
        ),
    ]


def register(app: Dash, backend: Backend) -> None:
    @app.callback(Output(ids.FIDELITY, "children"), Input(ids.HEALTH, "data"))
    def fidelity(health: dict[str, Any] | None) -> Any:
        if not health:
            return "unknown"
        return health.get("fidelity") or "unknown"

    @app.callback(
        Output(ids.LOG_CAUSE, "options"),
        Output(ids.LOGS_LEVEL, "options"),
        Output(ids.LOG_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.HEALTH, "data"),
        State(ids.LOG_CAUSE, "options"),
        prevent_initial_call="initial_duplicate",
    )
    @guarded(2)
    def filter_choices(health: dict[str, Any] | None, known: list[Any] | None) -> Any:
        if not health or known:
            raise PreventUpdate
        causes = backend.enum("/history/versions", "cause")
        levels = backend.enum("/lifecycle/logs", "level")
        return causes, levels

    @app.callback(
        Output(ids.LOG_BOX, "children"),
        Output(ids.LOG_FEEDBACK, "children"),
        Input(ids.HEALTH, "data"),
        Input(ids.LOG_CAUSE, "value"),
        Input(ids.LOG_ACTOR, "value"),
        Input(ids.LOG_FROM, "value"),
        Input(ids.LOG_TO, "value"),
    )
    @guarded(1)
    def commit_log(
        health: dict[str, Any] | None,
        cause: str | None,
        actor: str | None,
        first: str | None,
        last: str | None,
    ) -> Any:
        if not health:
            raise PreventUpdate
        query = {
            "from": (first or "").strip() or "#0",
            # The log ends at the head the page knows, so that it matches the slider.
            "to": (last or "").strip() or health["head"]["id"],
            "cause": cause or None,
            "actor": (actor or "").strip() or None,
            "order": "desc",
        }
        return log_grid(query)

    @app.callback(
        Output({"role": ids.COMMIT_LOG, "query": MATCH}, "getRowsResponse"),
        Input({"role": ids.COMMIT_LOG, "query": MATCH}, "getRowsRequest"),
    )
    def log_rows(request: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", LOG_BLOCK_ROWS))
        with trace(new_trace_id()) as trace_id:
            try:
                params = json.loads(grid_id["query"])
                block = backend.pages.block(
                    "/history/versions", path=None, params=params, start=start, end=end
                )
            except ApiProblem as problem:
                log.warn("the commit log cannot be shown", code=problem.code, start=start)
                set_props(ids.LOG_FEEDBACK, {"children": problem_box(problem)})
                return {"rowData": [], "rowCount": start}
            except Exception as exc:
                log.error("the commit log failed", exc=exc, start=start)
                set_props(ids.LOG_FEEDBACK, {"children": problem_box(internal_problem(trace_id))})
                return {"rowData": [], "rowCount": start}
        set_props(ids.LOG_FEEDBACK, {"children": None})
        return {"rowData": [version_row(entry) for entry in block.rows], "rowCount": block.last_row}

    @app.callback(
        Output(ids.DIFF, "children"),
        Output(ids.DIFF_FEEDBACK, "children"),
        Input(ids.VERSION, "data"),
    )
    @guarded(1, fallback=(None,))
    def changes(version: dict[str, Any] | None) -> Any:
        if not version:
            raise PreventUpdate
        if int(version["seq"]) == 0:
            return note("#0 is the first version: nothing comes before it.")
        parents = version.get("parents") or []
        base = parents[0] if parents else f"#{int(version['seq']) - 1}"
        document = backend.client.get(
            "/history/diff",
            params={"from": base, "to": version["id"], "detail": "rows", "limit": DIFF_ROWS},
        )
        schema = backend.schema(version["id"])
        label = f"#{document.get('from', {}).get('seq', int(version['seq']) - 1)}"
        return diff_view(schema, document, version, label)

    @app.callback(
        Output(ids.RUNS, "children"),
        Output(ids.RUNS_FEEDBACK, "children"),
        Input(ids.VERSION, "data"),
    )
    @guarded(1, fallback=(None,))
    def sync_runs(version: dict[str, Any] | None) -> Any:
        if not version:
            raise PreventUpdate
        schema = backend.schema(version["id"])
        if not schema.has(SYNC_RUN_TYPE):
            return note("This twin keeps no sync runs.")
        sort = "-started_at" if schema.property(SYNC_RUN_TYPE, "started_at") else None
        document = backend.client.get(
            "/objects/{type}",
            path={"type": SYNC_RUN_TYPE},
            params={"at": version["id"], "sort": sort, "limit": RUNS_SHOWN},
        )
        return runs_view(schema, document, version)

    @app.callback(
        Output(ids.LOGS_GRID, "rowData"),
        Output(ids.LOGS_FEEDBACK, "children"),
        Input(ids.LOGS_LEVEL, "value"),
        Input(ids.LOGS_CONTEXT, "value"),
        Input(ids.LOGS_REFRESH, "n_clicks"),
    )
    @guarded(1, fallback=([],))
    def logs(level: str | None, context: str | None, _: int) -> Any:
        document = backend.client.get(
            "/lifecycle/logs",
            params={
                "level": level or None,
                "context": (context or "").strip() or None,
                "limit": LOG_LINES,
            },
        )
        return [
            {
                "timestamp": line.get("timestamp"),
                "level": line.get("level"),
                "service": line.get("service"),
                "context": line.get("context"),
                "message": line.get("message"),
                "trace_id": line.get("trace_id"),
                "error": line.get("error"),
                "data": value_text(line.get("data")) if line.get("data") else "",
            }
            for line in document.get("data", [])
        ]


__all__ = ["layout", "register"]
