"""The sync section of the history tab: the twin's sources, its runs, the rows a run set
aside, and a button that starts a run now.

It shows the twin at its head, whatever version is selected: what syncing
knows of the sources is the present, not a version's. It reads only while the
history tab is open, and then only when a run finished or a sync job started
or ended since it last read (the sync key the header's poll keeps), never on a
poll that brought nothing new. The runs grid stays when it reads again, so a
new run appears without the grid losing its place. A static twin has nothing
to sync, and the section says only that.

A run asked for here is followed with the page's own poll: while it, or any
sync job, is queued or running the button is disabled; once it has ended its
outcome is told in words, read from its job.
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
    set_props,
)
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from .backend import Backend
from .client import ApiProblem
from .feedback import block_response, guarded, note
from .parts import field
from .text import cell, minute_text, plural, seconds_text, time_text

log = Log("ui.sync")

HISTORY_TAB = "history"
RUNS_BLOCK_ROWS = 50
REJECTS_BLOCK_ROWS = 100
# The most lines `/sync/runs/{uid}/rejects` gives in one page.
REJECTS_PAGE_MAX = 5_000
ACTIVE = ("queued", "running")
MARKED_STATUSES = ("failing", "disabled")
NO_RUN_CHOSEN = "Select a run in the grid above to see the rows it set aside."
STATIC = "This twin is static: it has no sources to keep up with, and nothing to sync."
JOB_GONE = "job.not_found"
GONE = "The outcome of the run asked for is not known: the API no longer keeps its job."


def layout() -> html.Section:
    return html.Section(
        [
            html.H2("Sync, as of the head"),
            dcc.Store(id=ids.SYNC_SHOWN),
            dcc.Store(id=ids.RUN_JOB),
            dcc.Store(id=ids.REJECTS_RUN),
            html.Div(id=ids.SYNC_FEEDBACK),
            html.Div(id=ids.SYNC_STATIC),
            html.Div(
                [
                    html.Div(
                        [
                            html.Button("Run now", id=ids.RUN_NOW, n_clicks=0, className="button"),
                            html.Span(id=ids.RUN_STATUS, className="run-status"),
                        ],
                        className="run-row",
                    ),
                    html.Div(id=ids.RUN_FEEDBACK),
                    html.H3("Sources"),
                    html.Div(id=ids.SOURCES),
                    html.H3("Runs"),
                    html.Div(
                        [
                            field(
                                "Status",
                                dcc.Dropdown(id=ids.RUNS_STATUS, options=[], placeholder="any"),
                            ),
                            field(
                                "Kind",
                                dcc.Dropdown(id=ids.RUNS_KIND, options=[], placeholder="any"),
                            ),
                        ],
                        className="filters",
                    ),
                    html.Div(id=ids.RUNS_FEEDBACK),
                    html.Div(id=ids.RUNS),
                    html.H3("Rows set aside"),
                    html.Div(
                        [
                            field(
                                "Rule",
                                dcc.Input(
                                    id=ids.REJECTS_RULE,
                                    type="text",
                                    debounce=True,
                                    placeholder="any",
                                ),
                            ),
                            field(
                                "Mapping",
                                dcc.Dropdown(id=ids.REJECTS_MAPPING, options=[], placeholder="any"),
                            ),
                        ],
                        className="filters",
                    ),
                    html.Div(id=ids.REJECTS_FEEDBACK),
                    html.Div(id=ids.REJECTS_TOTAL, className="muted"),
                    html.Div(note(NO_RUN_CHOSEN), id=ids.REJECTS),
                ],
                id=ids.SYNC_BODY,
                hidden=True,
            ),
        ],
        id=ids.SYNC_SECTION,
        className="sync-section",
    )


# -- sources ----------------------------------------------------------------------------


def dataset_text(dataset: dict[str, Any]) -> str:
    """How far one dataset of a source has been read."""
    name = dataset.get("name") or "?"
    if dataset.get("cursor") is not None:
        column = dataset.get("cursor_column") or "cursor"
        cursor = dataset["cursor"]
        shown = time_text(cursor) if isinstance(cursor, str) and "T" in cursor else cursor
        text = f"{name}: read to {column} {shown}"
    else:
        text = f"{name}: read whole"
    if dataset.get("rows_read") is not None:
        text += f", {plural(int(dataset['rows_read']), 'row')}"
    if dataset.get("last_read_at"):
        text += f", last read {minute_text(dataset['last_read_at'])}"
    return text


def sources_table(sources: list[dict[str, Any]]) -> Any:
    if not sources:
        return note("The manifest names no source.")
    head = ("source", "connector", "status", "newest data", "age", "datasets", "last error")
    rows = []
    for source in sources:
        status = source.get("status") or "not read yet"
        marked = source.get("status") in MARKED_STATUSES
        age = source.get("freshness_s")
        datasets = source.get("datasets") or []
        rows.append(
            html.Tr(
                [
                    html.Td(html.Code(source.get("id"))),
                    html.Td(source.get("connector") or ""),
                    html.Td(
                        html.Strong(status) if marked else status,
                        className=f"source-status status-{source.get('status') or 'none'}",
                    ),
                    html.Td(time_text(source.get("watermark")) or "nothing yet"),
                    html.Td(seconds_text(age) if age is not None else ""),
                    html.Td(
                        html.Ul([html.Li(dataset_text(found)) for found in datasets])
                        if datasets
                        else "none read yet"
                    ),
                    html.Td(source.get("last_error") or "", className="source-error"),
                ],
                className="source-row marked" if marked else "source-row",
                **{"data-source": str(source.get("id"))},
            )
        )
    return html.Table(
        [html.Thead(html.Tr([html.Th(label) for label in head])), html.Tbody(rows)],
        className="table sources",
    )


# -- runs and the rows they set aside ------------------------------------------------------

RUN_COLUMNS = [
    {"field": "status", "width": 110},
    {"field": "kind", "width": 110},
    {"field": "started_at", "headerName": "started", "width": 200},
    {"field": "finished_at", "headerName": "finished", "width": 200},
    {"field": "clock_at", "headerName": "clock time", "width": 200},
    {"field": "versions_written", "headerName": "versions written", "width": 140},
    {"field": "rows_read", "headerName": "rows read", "width": 110},
    {"field": "rows_written", "headerName": "written", "width": 100},
    {"field": "rows_rejected", "headerName": "set aside", "width": 100},
    {"field": "rows_late", "headerName": "late", "width": 80},
    {"field": "error", "flex": 1, "minWidth": 220, "tooltipField": "error"},
    {"field": "uid", "width": 320},
]
REJECT_COLUMNS = [
    {"field": "source", "width": 120},
    {"field": "mapping", "width": 170},
    {"field": "type", "width": 120},
    {"field": "uid", "width": 180},
    {"field": "property", "width": 130},
    {"field": "rule", "width": 100},
    {"field": "message", "flex": 1, "minWidth": 240, "tooltipField": "message"},
    {"field": "t", "headerName": "time", "width": 200},
]


def run_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": record.get("uid"),
        "status": record.get("status"),
        "kind": record.get("kind"),
        "started_at": time_text(record.get("started_at")),
        "finished_at": time_text(record.get("finished_at")),
        "clock_at": time_text(record.get("clock_at")),
        "versions_written": record.get("versions_written"),
        "rows_read": record.get("rows_read"),
        "rows_written": record.get("rows_written"),
        "rows_rejected": record.get("rows_rejected"),
        "rows_late": record.get("rows_late"),
        "error": cell(record.get("error")),
    }


def reject_row(line: dict[str, Any]) -> dict[str, Any]:
    row = {column["field"]: cell(line.get(column["field"])) for column in REJECT_COLUMNS}
    row["t"] = time_text(line.get("t"))
    return row


def runs_grid(query: dict[str, Any]) -> dag.AgGrid:
    return dag.AgGrid(
        id={"role": ids.RUNS_GRID, "query": json.dumps(query, sort_keys=True)},
        rowModelType="infinite",
        columnDefs=RUN_COLUMNS,
        defaultColDef={"resizable": True, "sortable": False, "cellDataType": False},
        dashGridOptions={
            "cacheBlockSize": RUNS_BLOCK_ROWS,
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
            "run-partial": "params.data && params.data.status === 'partial'",
            "run-failed": "params.data && params.data.status === 'failed'",
        },
        className="runs-grid",
        style={"height": "32vh", "width": "100%"},
    )


def rejects_grid(query: dict[str, Any]) -> dag.AgGrid:
    return dag.AgGrid(
        id={"role": ids.REJECTS_GRID, "query": json.dumps(query, sort_keys=True)},
        rowModelType="infinite",
        columnDefs=REJECT_COLUMNS,
        defaultColDef={"resizable": True, "sortable": False, "cellDataType": False},
        dashGridOptions={
            "cacheBlockSize": REJECTS_BLOCK_ROWS,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            "tooltipShowDelay": 300,
        },
        className="rejects-grid",
        style={"height": "32vh", "width": "100%"},
    )


def total_text(uid: str, total: Any, filtered: bool) -> str:
    if not isinstance(total, int):
        return f"Rows set aside by run {uid}"
    counted = plural(total, "row")
    return f"{counted} set aside by run {uid}" + (" match the filters" if filtered else "")


# -- a run asked for here ------------------------------------------------------------------


def active_job(live: dict[str, Any] | None) -> dict[str, Any] | None:
    """The sync job queued or running, as the last poll saw it."""
    sync = (live or {}).get("sync") or {}
    job = sync.get("job") or None
    return job if job and job.get("state") in ACTIVE else None


def run_outcome(job: dict[str, Any]) -> dict[str, str]:
    """How a finished sync job ended, in words: what it wrote, which sources failed and why,
    or why it failed. A job that succeeded can hold a partial run: some sources failed and
    the others were synced."""
    result = job.get("result") or {}
    if job.get("state") == "failed":
        error = job.get("error") or result.get("error") or {}
        reason = f"{error.get('code', 'unknown')}: {error.get('message', 'no reason given')}"
        return {"status": "failed", "text": f"The run failed: {reason}"}
    written = (
        f"{plural(int(result.get('versions_written') or 0), 'version')} and "
        f"{plural(int(result.get('rows_written') or 0), 'row')} written"
    )
    rejected = int(result.get("rows_rejected") or 0)
    if rejected:
        written += f", {plural(rejected, 'row')} set aside"
    if result.get("status") == "partial":
        failing = []
        for name, source in sorted((result.get("sources") or {}).items()):
            error = source.get("error")
            if source.get("status") == "ok" and not error:
                continue
            reason = f"{error.get('code')}: {error.get('message')}" if error else source["status"]
            failing.append(f"{name} ({reason})")
        return {
            "status": "partial",
            "text": f"The run was partial: {written}. Failing: " + "; ".join(failing),
        }
    return {"status": "succeeded", "text": f"The run succeeded: {written}."}


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.SYNC_STATIC, "children"),
        Output(ids.SYNC_BODY, "hidden"),
        Output(ids.SOURCES, "children"),
        Output(ids.REJECTS_MAPPING, "options"),
        Output(ids.RUNS_STATUS, "options"),
        Output(ids.RUNS_KIND, "options"),
        Output(ids.SYNC_SHOWN, "data"),
        Output(ids.SYNC_FEEDBACK, "children"),
        Input(ids.SYNC_KEY, "data"),
        Input(ids.TABS, "value"),
        State(ids.SYNC_SHOWN, "data"),
        State(ids.RUNS_STATUS, "options"),
    )
    @guarded(7)
    def refresh(
        key: dict[str, Any] | None,
        tab: str | None,
        shown: dict[str, Any] | None,
        statuses: list[Any] | None,
    ) -> Any:
        """Read the sources again when a run finished or a job started or ended since the
        section last read them, and only while it can be seen."""
        if not key or tab != HISTORY_TAB or key == shown:
            raise PreventUpdate
        if not key.get("live"):
            log.debug("the twin is static: the sync section has nothing to read")
            return note(STATIC), True, None, [], no_update, no_update, key
        started = time.perf_counter()
        document = backend.client.get("/sync/sources")
        sources = document.get("data") or []
        mappings = [
            f"{source['id']}.{mapping['id']}"
            for source in sources
            for mapping in source.get("mappings") or []
        ]
        status_options: Any = no_update
        kind_options: Any = no_update
        if not statuses:
            status_options = backend.enum("/sync/runs", "status")
            kind_options = backend.enum("/sync/runs", "kind")
        log.info(
            "sync section read",
            sources=len(sources),
            failing=sum(1 for source in sources if source.get("status") in MARKED_STATUSES),
            run=key.get("run"),
            job=key.get("job"),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return (
            None,
            False,
            sources_table(sources),
            mappings,
            status_options,
            kind_options,
            key,
        )

    @app.callback(
        Output(ids.RUNS, "children"),
        Output(ids.RUNS_FEEDBACK, "children"),
        Input(ids.SYNC_SHOWN, "data"),
        Input(ids.RUNS_STATUS, "value"),
        Input(ids.RUNS_KIND, "value"),
        State({"role": ids.RUNS_GRID, "query": ALL}, "id"),
    )
    @guarded(1)
    def runs(
        shown: dict[str, Any] | None,
        status: str | None,
        kind: str | None,
        present: list[dict[str, Any]] | None,
    ) -> Any:
        """The runs grid, made again when its filters change; a new run is read into the
        grid that stays."""
        if not shown:
            raise PreventUpdate
        if not shown.get("live"):
            return None
        query = {"status": status or None, "kind": kind or None}
        if {"role": ids.RUNS_GRID, "query": json.dumps(query, sort_keys=True)} in (present or []):
            raise PreventUpdate
        return runs_grid(query)

    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="refreshGrids"),
        Output(ids.GRIDS_REFRESHED, "data", allow_duplicate=True),
        Input(ids.SYNC_SHOWN, "data"),
        State({"role": ids.RUNS_GRID, "query": ALL}, "id"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output({"role": ids.RUNS_GRID, "query": MATCH}, "getRowsResponse"),
        Input({"role": ids.RUNS_GRID, "query": MATCH}, "getRowsRequest"),
        State(ids.SYNC_SHOWN, "data"),
    )
    def run_rows(request: dict[str, Any] | None, shown: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not shown or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + RUNS_BLOCK_ROWS))
        params = json.loads(grid_id["query"])

        def read() -> tuple[list[dict[str, Any]], int | None]:
            # Cursors are kept per state of the section: once a run is added, the listing
            # is read again from its first page.
            block = backend.pages.block(
                "/sync/runs",
                path=None,
                params=params,
                start=start,
                end=end,
                generation=json.dumps(shown, sort_keys=True),
            )
            return [run_row(record) for record in block.rows], block.last_row

        return block_response(ids.RUNS_FEEDBACK, "a block of the sync runs", read, start)

    @app.callback(
        Output(ids.REJECTS_RUN, "data"),
        Output(ids.REJECTS_FEEDBACK, "children"),
        Input({"role": ids.RUNS_GRID, "query": ALL}, "cellClicked"),
        State(ids.REJECTS_RUN, "data"),
        prevent_initial_call=True,
    )
    @guarded(1)
    def choose_run(_: list[Any], chosen: dict[str, Any] | None) -> Any:
        clicked = ctx.triggered[0]["value"] if ctx.triggered else None
        uid = clicked.get("rowId") if isinstance(clicked, dict) else None
        if not uid or (chosen and chosen.get("uid") == uid):
            raise PreventUpdate
        found = backend.client.get("/sync/runs/{uid}", path={"uid": str(uid)})["data"]
        log.debug("a run was chosen", rows_rejected=found.get("rows_rejected"))
        return {
            "uid": str(uid),
            "rows_rejected": int(found.get("rows_rejected") or 0),
            "kept": bool(found.get("rejects_location")),
        }

    @app.callback(
        Output(ids.REJECTS, "children"),
        Output(ids.REJECTS_TOTAL, "children"),
        Output(ids.REJECTS_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.REJECTS_RUN, "data"),
        Input(ids.REJECTS_RULE, "value"),
        Input(ids.REJECTS_MAPPING, "value"),
        prevent_initial_call=True,
    )
    @guarded(2)
    def rejects(run: dict[str, Any] | None, rule: str | None, mapping: str | None) -> Any:
        if not run:
            return note(NO_RUN_CHOSEN), ""
        uid = run["uid"]
        if not run.get("rows_rejected") and not run.get("kept"):
            return note(f"Run {uid} set no row aside."), ""
        query = {"uid": uid, "rule": (rule or "").strip() or None, "mapping": mapping or None}
        return rejects_grid(query), total_text(uid, None, False)

    @app.callback(
        Output({"role": ids.REJECTS_GRID, "query": MATCH}, "getRowsResponse"),
        Input({"role": ids.REJECTS_GRID, "query": MATCH}, "getRowsRequest"),
    )
    def reject_rows(request: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + REJECTS_BLOCK_ROWS))
        query = json.loads(grid_id["query"])
        uid = str(query["uid"])
        params = {"rule": query.get("rule"), "mapping": query.get("mapping")}

        def read() -> tuple[list[dict[str, Any]], int | None]:
            block = backend.pages.block(
                "/sync/runs/{uid}/rejects",
                path={"uid": uid},
                params=params,
                start=start,
                end=end,
                page_max=REJECTS_PAGE_MAX,
            )
            total = block.extra.get("total")
            filtered = any(value is not None for value in params.values())
            set_props(ids.REJECTS_TOTAL, {"children": total_text(uid, total, filtered)})
            count = total if isinstance(total, int) else block.last_row
            return [reject_row(line) for line in block.rows], count

        return block_response(
            ids.REJECTS_FEEDBACK, "a block of the rows set aside", read, start, run=uid
        )

    @app.callback(
        Output(ids.RUN_JOB, "data"),
        Output(ids.RUN_FEEDBACK, "children"),
        Input(ids.RUN_NOW, "n_clicks"),
        prevent_initial_call=True,
    )
    @guarded(1)
    def run_now(clicks: int | None) -> Any:
        """Ask for a run of every source. Asked for while one is in progress, the API gives
        back the job in progress: the button never starts a second run."""
        if not clicks:
            raise PreventUpdate
        started = time.perf_counter()
        job = backend.client.post("/sync/runs", body={})["job"]
        log.info(
            "a sync run was asked for from the page",
            job=job["id"],
            state=job["state"],
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return {"id": job["id"], "state": job["state"], "outcome": None}

    @app.callback(
        Output(ids.RUN_NOW, "disabled"),
        Output(ids.RUN_STATUS, "children"),
        Input(ids.LIVE, "data"),
        Input(ids.RUN_JOB, "data"),
    )
    def show_run(live: dict[str, Any] | None, job: dict[str, Any] | None) -> Any:
        """Disabled while a sync job is queued or running, or while the run asked for here
        has not been seen to end; then its outcome."""
        if active_job(live) is not None or (job and job.get("outcome") is None):
            return True, html.Span("run in progress", className="run-progress")
        if job and job.get("outcome"):
            outcome = job["outcome"]
            return False, html.Span(
                outcome["text"], className=f"run-outcome outcome-{outcome['status']}"
            )
        return False, ""

    @app.callback(
        Output(ids.RUN_JOB, "data", allow_duplicate=True),
        Output(ids.RUN_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.POLL, "n_intervals"),
        Input(ids.TABS, "value"),
        Input(ids.RUN_JOB, "data"),
        State(ids.LIVE, "data"),
        prevent_initial_call=True,
    )
    @guarded(1)
    def follow_run(
        _: int | None, tab: str | None, job: dict[str, Any] | None, live: dict[str, Any] | None
    ) -> Any:
        """Read the job asked for here, on the page's poll, until it has ended."""
        if not job:
            raise PreventUpdate
        active = active_job(live)
        if job.get("outcome") is not None:
            if active is not None and active.get("id") != job["id"]:
                # A newer run is in progress: the outcome shown is no longer the latest.
                return None
            raise PreventUpdate
        if tab != HISTORY_TAB:
            raise PreventUpdate
        try:
            found = backend.client.get("/lifecycle/jobs/{id}", path={"id": job["id"]})["data"]
        except ApiProblem as problem:
            if problem.code != JOB_GONE:
                raise
            # Jobs live in the API's memory: one that restarted no longer knows this job.
            log.warn("the job of a run asked for from the page is no longer kept", job=job["id"])
            return {**job, "state": "unknown", "outcome": {"status": "unknown", "text": GONE}}
        if found["state"] in ACTIVE:
            if found["state"] == job.get("state"):
                raise PreventUpdate
            return {**job, "state": found["state"]}
        outcome = run_outcome(found)
        log.info(
            "a sync run asked for from the page has ended",
            job=job["id"],
            state=found["state"],
            outcome=outcome["status"],
            duration_ms=found.get("duration_ms"),
        )
        return {**job, "state": found["state"], "outcome": outcome}


__all__ = ["layout", "register"]
