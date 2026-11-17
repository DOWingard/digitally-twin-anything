"""The scenarios tab: every scenario of the twin, and one compared with the live twin or
with another scenario of the same base.

The tab has its own place rather than a section of the history tab: a
comparison is about a scenario's horizon, not about the version the session
has selected, and it is read only when asked for. The list is read when the
tab is open and whenever the live twin's head moves (a scenario's record is a
version of the live twin), deleted and failed ones included. Nothing is
created, run or deleted from here.

A comparison is read whole (`/scenarios/{id}/compare`, page after page) on a
click and held on the server. Each target is drawn on a time axis, one pair
of lines per object, the scenario's solid with dots and the other side's
dashed with crosses, so the two are told apart without colour; at most
`DRAWN_MAX` objects at once, those with the largest absolute difference
first, and the operator may choose others. A side with no value at a time
leaves a gap in its line: a null is never drawn as zero. Every row is in the
grid below, sorted on the server.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass
from typing import Any

import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import MATCH, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .client import ApiProblem
from .feedback import block_response, guarded, note
from .paging import read_all
from .parts import LIVE_BRANCH, field
from .schema import NUMERIC_KINDS, PALETTE, Schema

log = Log("ui.scenarios")

TAB = "scenarios"
DRAWN_MAX = 12
# A comparison's rows are read whole, up to this many, so that they can be drawn.
ROWS_MAX = 200_000
PAGE_ROWS = 10_000
GRID_BLOCK = 100
SCENARIOS_MAX = 500
COMPARABLE = "done"


@dataclass(frozen=True)
class Compared:
    """A comparison read for the page: the scenario, the other side, and every row."""

    name: str
    against: str
    against_name: str
    targets: tuple[str, ...]
    units: dict[str, str | None]
    rows: list[dict[str, Any]]
    cut: bool


# -- what is offered -------------------------------------------------------------------------


def scenario_row(scenario: dict[str, Any]) -> dict[str, Any]:
    status = scenario.get("status") or ""
    return {
        "id": scenario.get("id"),
        "name": scenario.get("name") or scenario.get("id"),
        "base_t": T.minute_text(scenario.get("base_t")),
        "horizon": T.duration_text(scenario.get("horizon")),
        "status": T.scenario_status_text(status),
        "status_key": status,
        "interventions": T.interventions_text(scenario.get("interventions")),
        "models": ", ".join(scenario.get("models") or []),
    }


def numeric_targets(schema: Schema) -> list[str]:
    """Every numeric `Type.property` of the twin's own objects and links."""
    found = []
    for name in schema.sheet_types():
        if schema.is_meta(name):
            continue
        for prop in schema.properties(name):
            if prop.get("kind") in NUMERIC_KINDS:
                found.append(f"{name}.{prop['name']}")
    return found


def target_options(predicted: list[str], numeric: list[str]) -> list[dict[str, str]]:
    """What a scenario's models predict first; any other numeric property after."""
    return [{"label": T.target_option(name, True), "value": name} for name in predicted] + [
        {"label": T.target_option(name, False), "value": name}
        for name in numeric
        if name not in predicted
    ]


def against_options(
    chosen: dict[str, Any], scenarios: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """The live twin, and the other scenarios of the same base whose branch is there."""
    options = [{"label": T.LIVE_OPTION, "value": LIVE_BRANCH}]
    for other in scenarios:
        if other.get("id") == chosen.get("id") or other.get("status") == "deleted":
            continue
        if other.get("base_version") != chosen.get("base_version"):
            continue
        options.append({"label": T.against_option(other), "value": str(other["id"])})
    return options


# -- drawing ---------------------------------------------------------------------------------


def largest_differences(rows: list[dict[str, Any]], target: str) -> list[str]:
    """The objects of a target, the largest absolute difference first; an object with no
    difference at any time last, then in uid order."""
    largest: dict[str, float] = {}
    for row in rows:
        if row.get("target") != target:
            continue
        uid = str(row.get("uid"))
        difference = row.get("difference")
        size = abs(difference) if isinstance(difference, int | float) else -1.0
        if isinstance(size, float) and math.isnan(size):
            size = -1.0
        largest[uid] = max(largest.get(uid, -1.0), size)
    return sorted(largest, key=lambda uid: (-largest[uid], uid))


def figure(compared: Compared, target: str, uids: list[str]) -> go.Figure:
    """A target over time: per object the scenario's line and the other side's."""
    drawn = go.Figure()
    series: dict[str, list[dict[str, Any]]] = {uid: [] for uid in uids}
    for row in compared.rows:
        if row.get("target") == target and str(row.get("uid")) in series:
            series[str(row["uid"])].append(row)
    for index, uid in enumerate(uids):
        colour = PALETTE[index % len(PALETTE)]
        found = sorted(series[uid], key=lambda row: str(row.get("t")))
        times = [row.get("t") for row in found]
        for side, name, dash, symbol in (
            ("scenario", compared.name, "solid", "circle"),
            ("against", compared.against_name, "dash", "x"),
        ):
            drawn.add_trace(
                go.Scatter(
                    x=times,
                    # A missing value stays None: the line stops there.
                    y=[row.get(side) for row in found],
                    mode="lines+markers",
                    connectgaps=False,
                    line={"color": colour, "dash": dash, "width": 2},
                    marker={"symbol": symbol, "size": 6, "color": colour},
                    legendgroup=uid,
                    name=T.trace_name(uid, name),
                    meta=side,
                )
            )
    drawn.update_layout(
        template="simple_white",
        margin={"l": 55, "r": 10, "t": 30, "b": 40},
        height=380,
        title={"text": target, "x": 0.01},
        yaxis_title=compared.units.get(target) or "",
    )
    return drawn


def chart_section(compared: Compared, key: str, target: str) -> html.Div:
    order = largest_differences(compared.rows, target)
    chosen = order[:DRAWN_MAX]
    pick = dcc.Dropdown(
        id={"role": ids.COMPARE_PICK, "target": target, "key": key},
        options=[{"label": uid, "value": uid} for uid in order],
        value=chosen,
        multi=True,
        clearable=False,
    )
    return html.Div(
        [
            dcc.Graph(
                id={"role": ids.COMPARE_CHART, "target": target, "key": key},
                figure=figure(compared, target, chosen),
                config={"displaylogo": False, "responsive": True},
                className="compare-chart",
            ),
            html.Div(
                [html.Label(T.pick_label(target, DRAWN_MAX)), pick],
                className="field compare-pick",
            ),
            html.Div(id={"role": ids.COMPARE_NOTE, "target": target, "key": key}),
        ],
        className="compare-target",
        **{"data-target": target},
    )


def compare_grid(key: str, units: dict[str, str | None]) -> dag.AgGrid:
    columns = []
    for name, header in T.COMPARE_COLUMNS.items():
        column: dict[str, Any] = {"field": name, "headerName": header, "sortable": True}
        if name in ("scenario", "against", "difference"):
            column["type"] = "rightAligned"
        columns.append(column)
    return dag.AgGrid(
        id={"role": ids.COMPARE_GRID, "key": key},
        rowModelType="infinite",
        columnDefs=columns,
        defaultColDef={"resizable": True, "cellDataType": False, "minWidth": 100, "flex": 1},
        dashGridOptions={
            "cacheBlockSize": GRID_BLOCK,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            "suppressMultiSort": True,
        },
        className="compare-grid",
        style={"height": "45vh", "width": "100%"},
    )


def sorted_rows(rows: list[dict[str, Any]], sort_model: list[dict[str, Any]] | None) -> list[Any]:
    """The rows in the grid's order; values that are missing last, whichever the direction."""
    if not sort_model:
        return rows
    column = str(sort_model[0].get("colId"))
    descending = sort_model[0].get("sort") == "desc"
    present = [row for row in rows if row.get(column) is not None]
    missing = [row for row in rows if row.get(column) is None]
    present.sort(key=lambda row: row[column], reverse=descending)
    return present + missing


# -- layout and callbacks ---------------------------------------------------------------------


def layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id=ids.SCENARIO_LIST_SHOWN),
            dcc.Store(id=ids.SCENARIO_PICKED),
            html.Div(
                [
                    html.H3(T.SCENARIOS_HEADING),
                    html.Span(id=ids.SCENARIO_LIST_STATUS, className="status"),
                ],
                className="questions-heading",
            ),
            html.Div(id=ids.SCENARIO_LIST_FEEDBACK),
            dag.AgGrid(
                id=ids.SCENARIO_LIST,
                rowData=[],
                columnDefs=[
                    {
                        "field": name,
                        "headerName": header,
                        "tooltipField": name,
                        **(
                            {
                                "cellClassRules": {
                                    f"scenario-{status}": f"params.data.status_key == '{status}'"
                                    for status in T.SCENARIO_SIGNS
                                }
                            }
                            if name == "status"
                            else {}
                        ),
                    }
                    for name, header in T.SCENARIO_COLUMNS.items()
                ],
                getRowId="params.data.id",
                defaultColDef={"resizable": True, "sortable": True, "flex": 1, "minWidth": 110},
                dashGridOptions={"tooltipShowDelay": 300, "domLayout": "autoHeight"},
                className="scenario-list",
                style={"width": "100%"},
            ),
            html.Section(
                [
                    html.H4(T.CHOOSE_SCENARIO, id=ids.SCENARIO_TITLE),
                    html.Div(
                        [
                            field(
                                T.TARGETS_LABEL,
                                dcc.Dropdown(id=ids.COMPARE_TARGETS, options=[], multi=True),
                            ),
                            field(
                                T.AGAINST_LABEL,
                                dcc.Dropdown(
                                    id=ids.COMPARE_AGAINST,
                                    options=[{"label": T.LIVE_OPTION, "value": LIVE_BRANCH}],
                                    value=LIVE_BRANCH,
                                    clearable=False,
                                ),
                            ),
                            html.Button(
                                T.COMPARE,
                                id=ids.COMPARE,
                                n_clicks=0,
                                disabled=True,
                                className="button",
                            ),
                        ],
                        className="compare-controls",
                    ),
                    html.Div(id=ids.COMPARE_FEEDBACK),
                    # Drawn hidden when the tab opens, so that the chart library is loaded
                    # before a comparison is asked for rather than after.
                    dcc.Graph(
                        id=ids.COMPARE_PRELOAD,
                        figure={"data": [], "layout": {}},
                        config={"staticPlot": True},
                        style={"display": "none"},
                    ),
                    dcc.Loading(html.Div(id=ids.COMPARISON, className="comparison"), type="dot"),
                    html.Div(id=ids.COMPARE_GRID_FEEDBACK),
                ],
                className="compare-box",
            ),
        ],
        className="scenarios-tab",
    )


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.SCENARIO_LIST, "rowData"),
        Output(ids.SCENARIO_LIST_STATUS, "children"),
        Output(ids.SCENARIO_LIST_SHOWN, "data"),
        Output(ids.SCENARIO_LIST_FEEDBACK, "children"),
        Input(ids.TABS, "value"),
        Input(ids.HEALTH, "data"),
        State(ids.SCENARIO_LIST_SHOWN, "data"),
    )
    @guarded(3)
    def list_scenarios(tab: str | None, health: dict[str, Any] | None, shown: Any) -> Any:
        """Every scenario, deleted and failed ones too, as of the live twin's head; read when
        the tab is open and the head has moved since."""
        if tab != TAB or not health:
            raise PreventUpdate
        head = str(health["head"]["id"])
        if shown == head:
            raise PreventUpdate
        started = time.perf_counter()
        scenarios = read_all(
            backend.client, "/scenarios", {"include_deleted": "true"}, cap=SCENARIOS_MAX
        )
        rows = [scenario_row(found) for found in scenarios]
        rows.sort(key=lambda row: str(row["name"]))
        rows.sort(key=lambda row: str(row["base_t"]), reverse=True)
        log.info(
            "scenarios listed",
            scenarios=len(rows),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return rows, T.scenarios_status(len(rows), int(health["head"]["seq"])), head

    @app.callback(
        Output(ids.SCENARIO_PICKED, "data"),
        Output(ids.SCENARIO_TITLE, "children"),
        Output(ids.COMPARE_TARGETS, "options"),
        Output(ids.COMPARE_TARGETS, "value"),
        Output(ids.COMPARE_AGAINST, "options"),
        Output(ids.COMPARE_AGAINST, "value"),
        Output(ids.COMPARE, "disabled"),
        Output(ids.COMPARISON, "children", allow_duplicate=True),
        Output(ids.COMPARE_FEEDBACK, "children"),
        Input(ids.SCENARIO_LIST, "cellClicked"),
        State(ids.VERSION, "data"),
        prevent_initial_call=True,
    )
    @guarded(8)
    def pick(clicked: dict[str, Any] | None, version: dict[str, Any] | None) -> Any:
        """The scenario clicked, with the targets its models predict and what it can be
        compared against; one that has nothing to compare says so in place of a chart."""
        if not clicked or not version:
            raise PreventUpdate
        scenario_id = str(clicked.get("rowId") or "")
        scenarios = read_all(
            backend.client, "/scenarios", {"include_deleted": "true"}, cap=SCENARIOS_MAX
        )
        chosen = next((found for found in scenarios if str(found.get("id")) == scenario_id), None)
        if chosen is None:
            # Listed a moment ago and gone since: the list is read again on the next head.
            log.warn("a scenario clicked in the list is not listed any more", scenario=scenario_id)
            raise PreventUpdate
        models = read_all(backend.client, "/models", {}, cap=SCENARIOS_MAX)
        targets = {str(found.get("uid")): found.get("target") for found in models}
        predicted = list(
            dict.fromkeys(
                str(targets[uid]) for uid in chosen.get("models") or [] if targets.get(uid)
            )
        )
        numeric = numeric_targets(backend.schema_at(version))
        picked = {
            "id": chosen["id"],
            "name": chosen.get("name") or chosen["id"],
            "status": chosen.get("status"),
            "base_version": chosen.get("base_version"),
        }
        comparable = chosen.get("status") == COMPARABLE
        log.info(
            "a scenario is chosen to compare",
            scenario=chosen["id"],
            status=chosen.get("status"),
            predicted=predicted,
        )
        shown = [] if comparable else [note(T.not_comparable_text(chosen), warn=True)]
        return (
            picked,
            T.scenario_heading(chosen),
            target_options(predicted, numeric),
            predicted,
            against_options(chosen, scenarios),
            LIVE_BRANCH,
            not comparable,
            shown,
        )

    @app.callback(
        Output(ids.COMPARISON, "children"),
        Output(ids.COMPARE_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.COMPARE, "n_clicks"),
        State(ids.SCENARIO_PICKED, "data"),
        State(ids.COMPARE_TARGETS, "value"),
        State(ids.COMPARE_AGAINST, "value"),
        State(ids.COMPARE_AGAINST, "options"),
        State(ids.VERSION, "data"),
        running=[(Output(ids.COMPARE, "disabled"), True, False)],
        prevent_initial_call=True,
    )
    @guarded(1)
    def compare(
        clicks: int | None,
        picked: dict[str, Any] | None,
        targets: list[str] | None,
        against: str | None,
        against_choices: list[dict[str, Any]] | None,
        version: dict[str, Any] | None,
    ) -> Any:
        if not clicks or not picked or not version or ctx.triggered_id != ids.COMPARE:
            raise PreventUpdate
        if not targets:
            return [note(T.NO_TARGETS, warn=True)]
        other = against or LIVE_BRANCH
        started = time.perf_counter()
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        cut = False
        while True:
            page = backend.client.get(
                "/scenarios/{id}/compare",
                path={"id": str(picked["id"])},
                params={
                    "targets": ",".join(targets),
                    "against": other,
                    "limit": PAGE_ROWS,
                    "cursor": cursor,
                },
            )
            rows.extend(page.get("data") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
            if len(rows) >= ROWS_MAX:
                cut = True
                log.warn("a comparison was cut short", scenario=picked["id"], rows=len(rows))
                break
        schema = backend.schema_at(version)
        units = {}
        for target in targets:
            type_name, _, prop = target.partition(".")
            units[target] = schema.unit(type_name, prop) if schema.has(type_name) else None
        against_name = next(
            (str(found["label"]) for found in against_choices or [] if found["value"] == other),
            other,
        )
        compared = Compared(
            name=str(picked["name"]),
            against=other,
            against_name=against_name,
            targets=tuple(targets),
            units=units,
            rows=rows,
            cut=cut,
        )
        key = secrets.token_hex(8)
        backend.comparisons.put(key, compared)
        log.info(
            "a comparison was read",
            scenario=picked["id"],
            against=other,
            targets=targets,
            rows=len(rows),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        if not rows:
            return [note(T.NO_ROWS)]
        parts: list[Any] = [
            html.Div(T.compared_text(compared.name, against_name, len(rows), len(targets)))
        ]
        if cut:
            parts.append(note(T.compare_cut_text(len(rows)), warn=True))
        parts += [chart_section(compared, key, target) for target in targets]
        parts.append(compare_grid(key, units))
        return parts

    @app.callback(
        Output({"role": ids.COMPARE_CHART, "target": MATCH, "key": MATCH}, "figure"),
        Output({"role": ids.COMPARE_NOTE, "target": MATCH, "key": MATCH}, "children"),
        Input({"role": ids.COMPARE_PICK, "target": MATCH, "key": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def repick(chosen: list[str] | None) -> Any:
        """Other objects drawn, at most DRAWN_MAX of them."""
        grid_id = ctx.triggered_id
        if not isinstance(grid_id, dict):
            raise PreventUpdate
        compared = backend.comparisons.get(str(grid_id["key"]))
        if compared is None:
            log.warn("objects were chosen for a comparison that was let go")
            return no_update, note(T.COMPARED_GONE, warn=True)
        uids = list(chosen or [])
        shown = None
        if len(uids) > DRAWN_MAX:
            # An object added is added last: it is drawn, and the earliest chosen are not.
            uids = uids[-DRAWN_MAX:]
            shown = note(T.picked_cap_text(DRAWN_MAX), warn=True)
        log.debug("objects of a comparison chosen", target=grid_id["target"], objects=len(uids))
        return figure(compared, str(grid_id["target"]), uids), shown

    @app.callback(
        Output({"role": ids.COMPARE_GRID, "key": MATCH}, "getRowsResponse"),
        Input({"role": ids.COMPARE_GRID, "key": MATCH}, "getRowsRequest"),
    )
    def compare_rows(request: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not isinstance(grid_id, dict):
            raise PreventUpdate
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + GRID_BLOCK))

        def read() -> tuple[list[dict[str, Any]], int | None]:
            compared = backend.comparisons.get(str(grid_id["key"]))
            if compared is None:
                raise ApiProblem(
                    code="ui.comparison_gone", title=T.COMPARED_GONE, detail=T.COMPARED_GONE
                )
            ordered = sorted_rows(compared.rows, request.get("sortModel"))
            return ordered[start:end], len(ordered)

        return block_response(ids.COMPARE_GRID_FEEDBACK, "a block of a comparison", read, start)


__all__ = ["layout", "register"]
