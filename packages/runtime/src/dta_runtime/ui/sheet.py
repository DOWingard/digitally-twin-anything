"""The spreadsheet tab: a sheet per object type and per link type, read a block at a time.

The grid uses AG Grid's infinite row model. Each block it asks for becomes
one listing request with the version, sort and filters translated into the
API's `at`, `sort` and `where`; the API pages, sorts and filters, and only
the block asked for reaches the browser. The grid is made for a sheet and its
columns, which its id carries; when another version is selected and the
columns stay the same, the grid stays, with the operator's sort, filters and
scroll position, and is asked to read its rows again at that version.
"""

from __future__ import annotations

import hashlib
import json
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
from .backend import Backend
from .client import ApiProblem
from .feedback import block_response, guarded
from .parts import at_version, branch_of
from .schema import NUMERIC_KINDS, Schema, filterable
from .text import cell, on_branch_text

log = Log("ui.sheet")

BLOCK_ROWS = 100
TEXT_OPTIONS = ["contains", "equals", "notEqual", "startsWith", "endsWith", "blank", "notBlank"]
NUMBER_OPTIONS = [
    "equals",
    "notEqual",
    "lessThan",
    "lessThanOrEqual",
    "greaterThan",
    "greaterThanOrEqual",
    "inRange",
    "blank",
    "notBlank",
]
DATE_OPTIONS = ["equals", "notEqual", "lessThan", "greaterThan", "inRange", "blank", "notBlank"]
COMPARISONS = {
    "equals": "eq",
    "notEqual": "ne",
    "lessThan": "lt",
    "lessThanOrEqual": "le",
    "greaterThan": "gt",
    "greaterThanOrEqual": "ge",
}


def unsupported(detail: str) -> ApiProblem:
    return ApiProblem(code="ui.unsupported_filter", title="Filter not supported", detail=detail)


def _date_value(text: Any, kind: str) -> str:
    """AG Grid's `YYYY-MM-DD hh:mm:ss` as the API reads it; times are taken as UTC, as shown."""
    if not isinstance(text, str) or len(text) < 10:
        raise unsupported(f"{text!r} is not a date")
    if kind == "date":
        return text[:10]
    return text.strip().replace(" ", "T") + "Z"


def _value(value: Any, kind: str, filter_type: str) -> str:
    if filter_type == "date":
        return _date_value(value, kind)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and kind == "integer":
        return str(int(value))
    return str(value)


def where_clauses(filter_model: dict[str, Any] | None, kinds: dict[str, str]) -> list[str]:
    """The API's `where` clauses for an AG Grid filter model.

    A filter the API cannot express (such as "does not contain") is refused
    with a problem naming it, rather than applied in the browser to one block.
    """
    clauses: list[str] = []
    for column, entry in (filter_model or {}).items():
        if column not in kinds:
            raise unsupported(f"no column {column!r} to filter on")
        if "conditions" in entry or "operator" in entry:
            raise unsupported("only one condition per column can be applied")
        kind = kinds[column]
        operation = entry.get("type")
        filter_type = entry.get("filterType", "text")
        first = entry.get("dateFrom") if filter_type == "date" else entry.get("filter")
        if operation == "blank":
            clauses.append(f"{column}:null")
        elif operation == "notBlank":
            clauses.append(f"{column}:notnull")
        elif operation == "inRange":
            last = entry.get("dateTo") if filter_type == "date" else entry.get("filterTo")
            if first is None or last is None:
                raise unsupported(f"a range on {column} needs both ends")
            clauses.append(f"{column}:ge:{_value(first, kind, filter_type)}")
            clauses.append(f"{column}:le:{_value(last, kind, filter_type)}")
        elif operation in COMPARISONS:
            if first is None:
                continue
            clauses.append(f"{column}:{COMPARISONS[operation]}:{_value(first, kind, filter_type)}")
        elif operation in ("contains", "startsWith", "endsWith"):
            if first in (None, ""):
                continue
            text = str(first)
            pattern = {
                "contains": f"%{text}%",
                "startsWith": f"{text}%",
                "endsWith": f"%{text}",
            }[operation]
            clauses.append(f"{column}:like:{pattern}")
        else:
            raise unsupported(f"the filter {operation!r} on {column} is not supported by the API")
    return clauses


def sort_parameter(sort_model: list[dict[str, Any]] | None) -> str | None:
    if not sort_model:
        return None
    first = sort_model[0]
    return ("-" if first.get("sort") == "desc" else "") + str(first["colId"])


def column_defs(schema: Schema, type_name: str) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {
            "field": "uid",
            "headerName": "uid",
            "pinned": "left",
            "filter": "agTextColumnFilter",
            "filterParams": {"filterOptions": TEXT_OPTIONS, "maxNumConditions": 1},
        }
    ]
    for prop in schema.properties(type_name):
        unit = prop.get("unit")
        column: dict[str, Any] = {
            "field": prop["name"],
            "headerName": f"{prop['name']} [{unit}]" if unit else prop["name"],
            "headerTooltip": prop.get("description") or prop["name"],
        }
        kind = prop["kind"]
        if not filterable(prop):
            column.update({"filter": False, "sortable": False})
        elif kind in NUMERIC_KINDS:
            column.update(
                {
                    "filter": "agNumberColumnFilter",
                    "filterParams": {"filterOptions": NUMBER_OPTIONS, "maxNumConditions": 1},
                    "type": "rightAligned",
                }
            )
        elif kind in ("datetime", "date"):
            column.update(
                {
                    "filter": "agDateColumnFilter",
                    "filterParams": {
                        "filterOptions": DATE_OPTIONS,
                        "maxNumConditions": 1,
                        "includeTime": kind == "datetime",
                    },
                }
            )
        else:
            column.update(
                {
                    "filter": "agTextColumnFilter",
                    "filterParams": {"filterOptions": TEXT_OPTIONS, "maxNumConditions": 1},
                }
            )
        columns.append(column)
    return columns


def columns_digest(columns: list[dict[str, Any]]) -> str:
    """A short name for a sheet's columns: the grid is made again only when they change."""
    text = json.dumps(columns, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def grid_id(schema: Schema, type_name: str) -> dict[str, str]:
    return {
        "role": ids.SHEET_GRID,
        "sheet": type_name,
        "columns": columns_digest(column_defs(schema, type_name)),
    }


def grid(schema: Schema, type_name: str) -> dag.AgGrid:
    return dag.AgGrid(
        id=grid_id(schema, type_name),
        rowModelType="infinite",
        columnDefs=column_defs(schema, type_name),
        defaultColDef={
            "sortable": True,
            "resizable": True,
            "floatingFilter": True,
            # Values are shown as the API returns them; no type guessing from one block.
            "cellDataType": False,
            "minWidth": 90,
        },
        dashGridOptions={
            "cacheBlockSize": BLOCK_ROWS,
            "maxBlocksInCache": 20,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            "rowBuffer": 10,
            "suppressMultiSort": True,
            "tooltipShowDelay": 300,
        },
        className="sheet-grid",
        style={"height": "62vh", "width": "100%"},
    )


def layout() -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    html.Label("Sheet", htmlFor=ids.SHEET_TYPE),
                    dcc.Dropdown(id=ids.SHEET_TYPE, options=[], clearable=False),
                    html.Span(id=ids.SHEET_STATUS, className="status"),
                ],
                className="sheet-controls",
            ),
            html.Div(id=ids.SHEET_FEEDBACK),
            dcc.Store(id=ids.SHEET_REFRESH),
            html.Div(id=ids.SHEET_BOX),
        ],
        className="sheet-tab",
    )


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.SHEET_TYPE, "options"),
        Output(ids.SHEET_TYPE, "value"),
        Output(ids.SHEET_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.VERSION, "data"),
        State(ids.SHEET_TYPE, "value"),
        prevent_initial_call="initial_duplicate",
    )
    @guarded(2)
    def sheet_choices(version: dict[str, Any] | None, chosen: str | None) -> Any:
        if not version:
            raise PreventUpdate
        schema = backend.schema_at(version)
        options = [
            {"label": schema.type_label(name), "value": name} for name in schema.sheet_types()
        ]
        value = chosen if schema.has(chosen) else schema.default_node_type()
        return options, value if value != chosen else no_update

    @app.callback(
        Output(ids.SHEET_BOX, "children"),
        Output(ids.SHEET_STATUS, "children"),
        Output(ids.SHEET_REFRESH, "data"),
        Output(ids.SHEET_FEEDBACK, "children"),
        Input(ids.SHEET_TYPE, "value"),
        Input(ids.VERSION, "data"),
        State({"role": ids.SHEET_GRID, "sheet": ALL, "columns": ALL}, "id"),
    )
    @guarded(3)
    def make_sheet(
        type_name: str | None, version: dict[str, Any] | None, present: list[dict[str, Any]]
    ) -> Any:
        """The sheet's grid, made again only when the sheet or its columns change; a grid
        that stays is asked to read its rows again at the selected version."""
        if not version or not type_name:
            raise PreventUpdate
        schema = backend.schema_at(version)
        if not schema.has(type_name):
            raise PreventUpdate
        where = on_branch_text(branch_of(version), version.get("scenario"))
        status = (
            f"{type_name} at #{version['seq']}{f' of {where}' if where else ''}: sorted, "
            f"filtered and paged by the API, {BLOCK_ROWS} rows at a time"
        )
        if grid_id(schema, type_name) in (present or []):
            log.debug("the sheet's grid stays; its rows are read again", sheet=type_name)
            return no_update, status, f"{branch_of(version)}@{version['id']}"
        log.debug("the sheet's grid is made", sheet=type_name, version=version["id"])
        return grid(schema, type_name), status, no_update

    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="refreshGrids"),
        Output(ids.GRIDS_REFRESHED, "data", allow_duplicate=True),
        Input(ids.SHEET_REFRESH, "data"),
        State({"role": ids.SHEET_GRID, "sheet": ALL, "columns": ALL}, "id"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output({"role": ids.SHEET_GRID, "sheet": MATCH, "columns": MATCH}, "getRowsResponse"),
        Input({"role": ids.SHEET_GRID, "sheet": MATCH, "columns": MATCH}, "getRowsRequest"),
        State(ids.VERSION, "data"),
    )
    def sheet_rows(request: dict[str, Any] | None, version: dict[str, Any] | None) -> Any:
        if not request or not version:
            raise PreventUpdate
        grid_id = ctx.triggered_id
        if not isinstance(grid_id, dict):
            raise PreventUpdate
        start, end = int(request.get("startRow", 0)), int(request.get("endRow", BLOCK_ROWS))
        return block_response(
            ids.SHEET_FEEDBACK,
            "a block of a sheet",
            lambda: read_block(backend, grid_id, version, request, start, end),
            start,
            sheet=grid_id.get("sheet"),
        )


def read_block(
    backend: Backend,
    grid_id: dict[str, Any],
    version: dict[str, Any],
    request: dict[str, Any],
    start: int,
    end: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """A block of the sheet a grid shows, at the selected version."""
    type_name = str(grid_id["sheet"])
    schema = backend.schema_at(version)
    if not schema.has(type_name):
        raise ApiProblem(
            code="ui.unknown_sheet",
            title="No such sheet at this version",
            detail=f"{type_name} is not a type at this version",
        )
    if grid_id.get("columns") != columns_digest(column_defs(schema, type_name)):
        # The columns changed with the version: this grid is being replaced by one with
        # the new columns, which reads its own rows.
        log.debug("a block asked for by a grid being replaced", sheet=type_name)
        raise PreventUpdate
    kinds = {"uid": "string"} | {
        prop["name"]: prop["kind"] for prop in schema.properties(type_name)
    }
    params: dict[str, Any] = {
        **at_version(version),
        "where": where_clauses(request.get("filterModel"), kinds) or None,
        "sort": sort_parameter(request.get("sortModel")),
    }
    block = backend.pages.block(
        "/objects/{type}", path={"type": type_name}, params=params, start=start, end=end
    )
    rows = [{name: cell(value) for name, value in row.items()} for row in block.rows]
    log.debug(
        "sheet block served",
        sheet=type_name,
        start=start,
        rows=len(rows),
        filters=len(params["where"] or []),
        sorted=params["sort"] is not None,
    )
    return rows, block.last_row
