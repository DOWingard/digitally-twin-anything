"""The history tab: the commit log, what the selected version changed, sync, fidelity and logs.

Clicking a row of the commit log selects that version for the whole page,
as the header's slider does. The commit log ends at a head of the page's
own: it moves on when the selected version goes past it (following the head,
or jumping to it), and the grid then reads its rows again where it stands,
keeping its scroll position. A new head the page does not move to reads
nothing. The changes shown are those of the selected version against the one
before it. The sync section shows the twin at its head (see `sync`).
"""

from __future__ import annotations

import json
from typing import Any

import dash_ag_grid as dag
from dash import ALL, MATCH, ClientsideFunction, Dash, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids, sync
from .backend import Backend
from .feedback import block_response, guarded, note
from .parts import field
from .schema import Schema
from .text import plural, time_text, value_text, with_unit

log = Log("ui.history")

LOG_BLOCK_ROWS = 100
DIFF_ROWS = 100
LOG_LINES = 200


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
            dcc.Store(id=ids.LOG_HEAD),
            html.Div(id=ids.LOG_FEEDBACK),
            html.Div(id=ids.LOG_BOX),
            html.H2("Changes in the selected version"),
            html.Div(id=ids.DIFF_FEEDBACK),
            html.Div(id=ids.DIFF),
            sync.layout(),
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
        # Rows are identified by their seq, as text, so that a click names the version it
        # landed on. dash-ag-grid evaluates this as a restricted expression: a call such as
        # String(...) yields nothing, so the text is prepared on the server.
        getRowId="params.data.row_id",
        className="commit-log",
        style={"height": "32vh", "width": "100%"},
    )


def version_row(entry: dict[str, Any]) -> dict[str, Any]:
    extra = entry.get("extra") or {}
    return {
        "row_id": str(entry["seq"]),
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


def log_query(
    cause: str | None, actor: str | None, first: str | None, last: str | None
) -> dict[str, Any]:
    """The commit log's filters as the API takes them; an open end is the log's own head."""
    return {
        "from": (first or "").strip() or "#0",
        "to": (last or "").strip() or None,
        "cause": cause or None,
        "actor": (actor or "").strip() or None,
        "order": "desc",
    }


def next_log_head(
    health: dict[str, Any] | None, version: dict[str, Any] | None, known: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The head the commit log ends at, or None when it stays where it is.

    It moves to the twin's head when the selected version goes past it, which
    is what following the head and jumping to it do; a new head the page stays
    away from leaves it, and the grid, where they are.
    """
    if not health:
        return None
    head = {"id": health["head"]["id"], "seq": int(health["head"]["seq"])}
    if known == head:
        return None
    if known is None or head["seq"] < int(known["seq"]):
        return head
    if version and int(version["seq"]) > int(known["seq"]):
        return head
    return None


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
        Output(ids.LOG_HEAD, "data"),
        Input(ids.HEALTH, "data"),
        Input(ids.VERSION, "data"),
        State(ids.LOG_HEAD, "data"),
    )
    def log_head(
        health: dict[str, Any] | None,
        version: dict[str, Any] | None,
        known: dict[str, Any] | None,
    ) -> Any:
        found = next_log_head(health, version, known)
        if found is None:
            raise PreventUpdate
        log.debug("the commit log moves to a new head", head_seq=found["seq"])
        return found

    @app.callback(
        Output(ids.LOG_BOX, "children"),
        Output(ids.LOG_FEEDBACK, "children"),
        Input(ids.LOG_HEAD, "data"),
        Input(ids.LOG_CAUSE, "value"),
        Input(ids.LOG_ACTOR, "value"),
        Input(ids.LOG_FROM, "value"),
        Input(ids.LOG_TO, "value"),
        State({"role": ids.COMMIT_LOG, "query": ALL}, "id"),
    )
    @guarded(1)
    def commit_log(
        head: dict[str, Any] | None,
        cause: str | None,
        actor: str | None,
        first: str | None,
        last: str | None,
        present: list[dict[str, Any]] | None,
    ) -> Any:
        """The commit log's grid, made again when its filters change. When only its head
        moves, the grid stays and reads its rows again (in the browser, below)."""
        if not head:
            raise PreventUpdate
        query = log_query(cause, actor, first, last)
        if {"role": ids.COMMIT_LOG, "query": json.dumps(query, sort_keys=True)} in (present or []):
            raise PreventUpdate
        return log_grid(query)

    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="refreshGrids"),
        Output(ids.GRIDS_REFRESHED, "data", allow_duplicate=True),
        Input(ids.LOG_HEAD, "data"),
        State({"role": ids.COMMIT_LOG, "query": ALL}, "id"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output({"role": ids.COMMIT_LOG, "query": MATCH}, "getRowsResponse"),
        Input({"role": ids.COMMIT_LOG, "query": MATCH}, "getRowsRequest"),
        State(ids.LOG_HEAD, "data"),
    )
    def log_rows(request: dict[str, Any] | None, head: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not head or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + LOG_BLOCK_ROWS))

        def read() -> tuple[list[dict[str, Any]], int | None]:
            params = json.loads(grid_id["query"])
            # Without an end of its own, the log ends at its head, so that the rows of one
            # head are read with cursors issued for that head.
            params["to"] = params.get("to") or head["id"]
            block = backend.pages.block(
                "/history/versions", path=None, params=params, start=start, end=end
            )
            return [version_row(entry) for entry in block.rows], block.last_row

        return block_response(ids.LOG_FEEDBACK, "a block of the commit log", read, start)

    @app.callback(
        Output({"role": ids.COMMIT_LOG, "query": ALL}, "selectedRows"),
        Input(ids.VERSION, "data"),
        State({"role": ids.COMMIT_LOG, "query": ALL}, "id"),
    )
    def mark_selected(version: dict[str, Any] | None, present: list[Any] | None) -> Any:
        """The selected version's row is the commit log's selected row, once it is loaded."""
        if not version or not present:
            raise PreventUpdate
        return [{"ids": [str(version["seq"])]} for _ in present]

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
