"""The actions tab: what the live twin does to the real system, as of the selected version.

Shown only for a live twin whose manifest declares actions. Read while the tab is
open, once per selected version: the action types and whether an executor is
configured (`/actions`); the records of the proposals, newest first, in an
infinite grid that reads a block of rows as it is scrolled to, filtered by status
and action (`/actions/records`); everything about the record chosen
(`/actions/records/{uid}`); the control loops and what each one's last run did
(`/control/loops`). The external call of a record is shown as the API tells it,
which holds no secret. Nothing here proposes, approves, enables or runs anything.
"""

from __future__ import annotations

import json
import time
from typing import Any

import dash_ag_grid as dag
from dash import (
    ALL,
    MATCH,
    ClientsideFunction,
    Dash,
    Input,
    Output,
    State,
    ctx,
    dcc,
    html,
    no_update,
)
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .feedback import block_response, guarded, note
from .parts import LIVE_BRANCH, branch_of, client_grid, field

log = Log("ui.actions")

TAB = "actions"
RECORDS_BLOCK_ROWS = 100


def type_row(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": action.get("id"),
        "control": action.get("control") or "",
        "target": action.get("target") or "",
        "autonomy": action.get("autonomy"),
        "binding": action.get("binding") or "",
        "bounds": T.bounds_text(action.get("bounds")),
        "settle": T.duration_text(action.get("settle")),
        "status": action.get("status"),
    }


def record_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": record.get("uid"),
        "proposed_at": T.time_text(record.get("proposed_at")),
        "action": record.get("action"),
        "object": record.get("object"),
        "params": T.params_text(record.get("params")),
        "source": record.get("source"),
        "fidelity": record.get("fidelity") or "",
        "status": T.record_status_text(record.get("status")),
        "status_key": record.get("status") or "",
        "reconciliation": record.get("reconciliation") or "",
        "effective_at": T.time_text(record.get("effective_at")),
    }


def loop_row(loop: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": loop.get("id"),
        "policy": loop.get("policy") or "",
        "interval": T.duration_text(loop.get("interval")),
        "enabled": T.ENABLED_WORDS[bool(loop.get("enabled"))],
        "last_run_at": T.time_text(loop.get("last_run_at")),
    }


def notes_text(notes: Any) -> str:
    if isinstance(notes, list):
        return "; ".join(T.value_text(item) for item in notes)
    return T.value_text(notes)


def last_runs(loops: list[dict[str, Any]]) -> Any:
    """What the last run of each loop that ran did."""
    ran = [loop for loop in loops if loop.get("last_run")]
    if not ran:
        return note(T.NO_LAST_RUN)
    rows = []
    for loop in ran:
        run = loop["last_run"]
        cells = [
            loop.get("id"),
            T.time_text(run.get("at")),
            run.get("status"),
            *(T.value_text(run.get(name)) for name in T.LAST_RUN_COLUMNS[3:9]),
            notes_text(run.get("notes")),
            T.value_text(run.get("error")),
        ]
        rows.append(html.Tr([html.Td(value) for value in cells], **{"data-loop": loop.get("id")}))
    return html.Table(
        [
            html.Thead(html.Tr([html.Th(label) for label in T.LAST_RUN_COLUMNS])),
            html.Tbody(rows),
        ],
        className="table loop-runs",
    )


def record_columns() -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name, header in T.RECORD_COLUMNS.items():
        column: dict[str, Any] = {"field": name, "headerName": header, "tooltipField": name}
        if name == "params":
            column["minWidth"] = 160
        if name == "proposed_at":
            column["minWidth"] = 190
        columns.append(column)
    return columns


def records_grid(query: dict[str, Any]) -> dag.AgGrid:
    return dag.AgGrid(
        id={"role": ids.RECORDS_GRID, "query": json.dumps(query, sort_keys=True)},
        rowModelType="infinite",
        columnDefs=record_columns(),
        defaultColDef={"resizable": True, "sortable": False, "cellDataType": False, "flex": 1},
        dashGridOptions={
            "cacheBlockSize": RECORDS_BLOCK_ROWS,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            "tooltipShowDelay": 300,
            "rowSelection": {
                "mode": "singleRow",
                "checkboxes": False,
                "enableClickSelection": True,
            },
        },
        getRowId="params.data.uid",
        rowClassRules={
            f"record-{status}": f"params.data && params.data.status_key === '{status}'"
            for status in ("rejected", "blocked", "failed")
        }
        | {"record-mismatch": "params.data && params.data.reconciliation === 'mismatch'"},
        className="records-grid",
        style={"height": "40vh", "width": "100%"},
    )


def part(title: str, body: Any, name: str) -> html.Div:
    return html.Div([html.H5(title), body], className="record-part", **{"data-part": name})


def lines(texts: list[str]) -> html.Ul:
    return html.Ul([html.Li(text) for text in texts])


def record_detail(record: dict[str, Any]) -> html.Div:
    """Everything the API tells of a record: the proposal, why it was refused, the
    decision, the external call and what reconciliation observed."""
    reasons = record.get("reasons") or []
    approval = record.get("approval")
    execution = record.get("execution")
    observed = record.get("observed")
    return html.Div(
        [
            part(T.RECORD_PARTS["proposal"], lines(T.proposal_lines(record)), "proposal"),
            part(
                T.RECORD_PARTS["reasons"],
                lines([T.reason_text(found) for found in reasons]) if reasons else T.NO_REASONS,
                "reasons",
            ),
            part(
                T.RECORD_PARTS["approval"],
                T.approval_text(approval) if approval else T.NO_APPROVAL,
                "approval",
            ),
            part(
                T.RECORD_PARTS["execution"],
                lines(T.execution_lines(execution)) if execution else T.NO_EXECUTION,
                "execution",
            ),
            part(
                T.RECORD_PARTS["observed"],
                lines(T.observed_lines(observed, record.get("reconciled_at")))
                if observed
                else T.NO_OBSERVED,
                "observed",
            ),
        ],
        className="record-detail-block",
        **{"data-uid": record.get("uid")},
    )


def layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id=ids.ACTIONS_SHOWN),
            dcc.Store(id=ids.RECORD_CHOSEN),
            html.H3(T.ACTIONS_HEADING),
            html.Div(id=ids.ACTIONS_NOTE),
            html.H4(T.TYPES_HEADING),
            html.Div(id=ids.EXECUTOR_STATUS, className="muted"),
            html.Div(id=ids.ACTION_TYPES_FEEDBACK),
            client_grid(
                ids.ACTION_TYPES_GRID,
                [
                    {"field": name, "headerName": header, "tooltipField": name}
                    for name, header in T.TYPE_COLUMNS.items()
                ],
                "id",
                height=None,
                class_name="action-types-grid",
            ),
            html.H4(T.RECORDS_HEADING),
            html.Div(
                [
                    field(
                        T.STATUS_FILTER_LABEL,
                        dcc.Dropdown(
                            id=ids.RECORDS_STATUS,
                            options=[],
                            placeholder=T.FILTER_ANY,
                            searchable=False,
                        ),
                    ),
                    field(
                        T.ACTION_FILTER_LABEL,
                        dcc.Dropdown(
                            id=ids.RECORDS_ACTION,
                            options=[],
                            placeholder=T.FILTER_ANY,
                            searchable=False,
                        ),
                    ),
                ],
                className="filters",
            ),
            html.Div(id=ids.RECORDS_FEEDBACK),
            html.Div(id=ids.RECORDS),
            html.Div(id=ids.RECORD_FEEDBACK),
            html.Div(note(T.CHOOSE_RECORD), id=ids.RECORD_DETAIL, className="record-detail"),
            html.H4(T.LOOPS_HEADING),
            html.Div(id=ids.LOOPS_FEEDBACK),
            client_grid(
                ids.LOOPS_GRID,
                [
                    {"field": name, "headerName": header, "tooltipField": name}
                    for name, header in T.LOOP_COLUMNS.items()
                ],
                "id",
                height=None,
                class_name="loops-grid",
            ),
            html.H5(T.LAST_RUNS_HEADING),
            html.Div(id=ids.LOOP_RUNS),
        ],
        className="actions-tab",
    )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.ACTIONS_SHOWN, "data"),
        Input(ids.TABS, "value"),
        Input(ids.VERSION, "data"),
        Input(ids.FEATURES, "data"),
        State(ids.ACTIONS_SHOWN, "data"),
    )
    def gate(
        tab: str | None,
        version: dict[str, Any] | None,
        features: dict[str, Any] | None,
        shown: dict[str, Any] | None,
    ) -> Any:
        """What the tab shows: the selected version, while the tab is open."""
        if tab != TAB or not version or not (features or {}).get("actions"):
            raise PreventUpdate
        key = {
            "at": str(version["id"]),
            "seq": version.get("seq"),
            "t": version.get("t"),
            "branch": branch_of(version),
        }
        if key == shown:
            raise PreventUpdate
        log.debug("the actions tab shows another version", at=key["at"], branch=key["branch"])
        return key

    @app.callback(
        Output(ids.ACTIONS_NOTE, "children"),
        Input(ids.ACTIONS_SHOWN, "data"),
    )
    def scenario_note(shown: dict[str, Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        return None if shown["branch"] == LIVE_BRANCH else note(T.LIVE_ONLY, warn=True)

    @app.callback(
        Output(ids.ACTION_TYPES_GRID, "rowData"),
        Output(ids.EXECUTOR_STATUS, "children"),
        Output(ids.RECORDS_ACTION, "options"),
        Output(ids.RECORDS_STATUS, "options"),
        Output(ids.ACTION_TYPES_FEEDBACK, "children"),
        Input(ids.ACTIONS_SHOWN, "data"),
        State(ids.RECORDS_STATUS, "options"),
    )
    @guarded(4, fallback=([], "", no_update, no_update))
    def types(shown: dict[str, Any] | None, statuses: list[Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        if shown["branch"] != LIVE_BRANCH:
            return [], "", no_update, no_update
        started = time.perf_counter()
        document = backend.client.get("/actions", params={"at": shown["at"]})
        found = document.get("data") or []
        offered = no_update if statuses else backend.enum("/actions/records", "status")
        log.info(
            "action types read",
            at=shown["at"],
            types=len(found),
            executor=bool(document.get("executor_configured")),
            duration_ms=_ms(started),
        )
        return (
            [type_row(action) for action in found],
            T.executor_text(bool(document.get("executor_configured"))),
            [str(action.get("id")) for action in found],
            offered,
        )

    @app.callback(
        Output(ids.RECORDS, "children"),
        Output(ids.RECORDS_FEEDBACK, "children"),
        Input(ids.ACTIONS_SHOWN, "data"),
        Input(ids.RECORDS_STATUS, "value"),
        Input(ids.RECORDS_ACTION, "value"),
        State({"role": ids.RECORDS_GRID, "query": ALL}, "id"),
    )
    @guarded(1)
    def records(
        shown: dict[str, Any] | None,
        status: str | None,
        action: str | None,
        present: list[dict[str, Any]] | None,
    ) -> Any:
        """The records grid, made again when its filters change; a new version is read into
        the grid that stays."""
        if not shown:
            raise PreventUpdate
        if shown["branch"] != LIVE_BRANCH:
            return None
        query = {"status": status or None, "action": action or None}
        if {"role": ids.RECORDS_GRID, "query": json.dumps(query, sort_keys=True)} in (
            present or []
        ):
            raise PreventUpdate
        log.debug("the records grid is made", status=status, action=action)
        return records_grid(query)

    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="refreshGrids"),
        Output(ids.GRIDS_REFRESHED, "data", allow_duplicate=True),
        Input(ids.ACTIONS_SHOWN, "data"),
        State({"role": ids.RECORDS_GRID, "query": ALL}, "id"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output({"role": ids.RECORDS_GRID, "query": MATCH}, "getRowsResponse"),
        Input({"role": ids.RECORDS_GRID, "query": MATCH}, "getRowsRequest"),
        State(ids.ACTIONS_SHOWN, "data"),
    )
    def record_rows(request: dict[str, Any] | None, shown: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not shown or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + RECORDS_BLOCK_ROWS))
        params = {**json.loads(grid_id["query"]), "at": shown["at"]}

        def read() -> tuple[list[dict[str, Any]], int | None]:
            block = backend.pages.block(
                "/actions/records",
                path=None,
                params=params,
                start=start,
                end=end,
                generation=shown["at"],
            )
            return [record_row(record) for record in block.rows], block.last_row

        return block_response(
            ids.RECORDS_FEEDBACK, "a block of the action records", read, start, at=shown["at"]
        )

    @app.callback(
        Output(ids.RECORD_CHOSEN, "data"),
        Input({"role": ids.RECORDS_GRID, "query": ALL}, "cellClicked"),
        State(ids.RECORD_CHOSEN, "data"),
        prevent_initial_call=True,
    )
    def choose_record(_: list[Any], chosen: str | None) -> Any:
        clicked = ctx.triggered[0]["value"] if ctx.triggered else None
        uid = clicked.get("rowId") if isinstance(clicked, dict) else None
        if not uid or uid == chosen:
            raise PreventUpdate
        log.debug("a record was chosen")
        return str(uid)

    @app.callback(
        Output(ids.RECORD_DETAIL, "children"),
        Output(ids.RECORD_FEEDBACK, "children"),
        Input(ids.RECORD_CHOSEN, "data"),
        Input(ids.ACTIONS_SHOWN, "data"),
    )
    @guarded(1, fallback=(None,))
    def show_record(chosen: str | None, shown: dict[str, Any] | None) -> Any:
        """Everything about the record chosen, read at the version shown."""
        if not shown or shown["branch"] != LIVE_BRANCH:
            raise PreventUpdate
        if not chosen:
            if ctx.triggered_id == ids.RECORD_CHOSEN:
                raise PreventUpdate
            return note(T.CHOOSE_RECORD)
        started = time.perf_counter()
        found = backend.client.get(
            "/actions/records/{uid}", path={"uid": chosen}, params={"at": shown["at"]}
        )["data"]
        log.info(
            "a record was read",
            status=found.get("status"),
            reconciliation=found.get("reconciliation"),
            duration_ms=_ms(started),
        )
        return record_detail(found)

    @app.callback(
        Output(ids.LOOPS_GRID, "rowData"),
        Output(ids.LOOP_RUNS, "children"),
        Output(ids.LOOPS_FEEDBACK, "children"),
        Input(ids.ACTIONS_SHOWN, "data"),
    )
    @guarded(2, fallback=([], None))
    def loops(shown: dict[str, Any] | None) -> Any:
        if not shown:
            raise PreventUpdate
        if shown["branch"] != LIVE_BRANCH:
            return [], None
        started = time.perf_counter()
        found = backend.client.get("/control/loops", params={"at": shown["at"]})["data"]
        log.info("control loops read", at=shown["at"], loops=len(found), duration_ms=_ms(started))
        if not found:
            return [], note(T.NO_LOOPS)
        return [loop_row(loop) for loop in found], last_runs(found)


__all__ = ["TAB", "layout", "register"]
