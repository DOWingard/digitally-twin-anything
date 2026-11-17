"""An answer to a question, as the questions tab shows it.

A value is shown with its unit and interval; a path or a list is told in
words; a table is a grid read a block at a time, each block asked of the API
again with the cursor the answer gave; a forecast (rows of series, time, mean
and the bounds of each interval) is also drawn, as lines with their intervals
as bands. The first page of a table is the answer to the click itself and is
held on the server, so the grid's first block does not run the function again.
The answer's version, whether it is provisional, its notes and its model are
shown above the value.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import dash_ag_grid as dag
import httpx
import plotly.graph_objects as go
from dash import MATCH, Dash, Input, Output, ctx, dcc, html
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .client import ApiProblem
from .feedback import block_response, note
from .paging import Posted
from .schema import PALETTE

log = Log("ui.answers")

ROUTE = "/questions/{id}/answer"
# Rows per block of an answer's grid; a first page is as long, so blocks start where
# the cursors of the API's pages do.
BLOCK_ROWS = 100
# A forecast is read whole in one run, up to this many rows, so that it can be drawn.
FORECAST_ROWS = 5_000
SERIES_DRAWN = 24
# A question's function has its own latency budget, a forecast up to two minutes; the
# API gives up at it, and the interface waits a little longer than the API.
ANSWER_TIMEOUT = httpx.Timeout(150.0, connect=3.0)
SERIES, TIME, MEAN = "unique_id", "ds", "mean"
LOW, HIGH = T.BOUND_PREFIXES


@dataclass(frozen=True)
class Asked:
    """A question asked from the page whose answer is a table: what was asked, and the
    first page of the rows."""

    question: str
    params: dict[str, str]
    body: dict[str, Any]
    rows: list[dict[str, Any]]
    next_cursor: str | None
    unit: str | None

    def posted(self) -> Posted:
        return Posted(body=self.body, rows_member="value", timeout=ANSWER_TIMEOUT)


def is_table(document: dict[str, Any]) -> bool:
    """The API pages an answer only when it is a table: its document then has a cursor
    member, null on the last page."""
    return "next_cursor" in document and isinstance(document.get("value"), list)


def is_forecast(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and {SERIES, TIME, MEAN} <= set(rows[0])


def ask(
    backend: Backend,
    question: str,
    params: dict[str, str],
    body: dict[str, Any],
    *,
    whole: bool,
) -> tuple[dict[str, Any], str]:
    """Ask a question once; its answer, and the key its rows are held under. `whole`
    reads a forecast's rows in one run, so that they can all be drawn."""
    started = time.perf_counter()
    limit = FORECAST_ROWS if whole else BLOCK_ROWS
    document = backend.client.post(
        ROUTE,
        path={"id": question},
        params={**params, "limit": limit},
        body=body,
        timeout=ANSWER_TIMEOUT,
    )
    key = secrets.token_hex(8)
    table = is_table(document)
    if table:
        asked = Asked(
            question=question,
            params=dict(params),
            body=dict(body),
            rows=list(document["value"]),
            next_cursor=document.get("next_cursor"),
            unit=document.get("unit"),
        )
        backend.answers.put(key, asked)
        if asked.next_cursor:
            backend.pages.seed(
                ROUTE,
                path={"id": question},
                params=asked.params,
                position=len(asked.rows),
                cursor=asked.next_cursor,
                generation=key,
                posted=asked.posted(),
            )
    log.info(
        "a question was asked from the page",
        question=question,
        arguments=sorted(body),
        table=table,
        rows=len(document["value"]) if table else None,
        more=bool(document.get("next_cursor")),
        provisional=document.get("provisional"),
        model=document.get("model"),
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return document, key


# -- the view -------------------------------------------------------------------------------


def view(document: dict[str, Any], key: str, branch_label: str | None) -> list[Any]:
    """Everything the page shows of an answer."""
    unit = document.get("unit")
    parts: list[Any] = [
        html.Div(T.answered_text(document, branch_label), className="answer-version"),
    ]
    if document.get("model"):
        parts.append(html.Div(T.model_text(document["model"]), className="answer-model"))
    if document.get("provisional"):
        parts.append(
            html.Div(
                T.provisional_text(list(document.get("reasons") or [])),
                className="notice provisional",
                role="status",
            )
        )
    if document.get("notes"):
        parts.append(
            html.Div(T.notes_text(list(document["notes"])), className="notice notes", role="status")
        )
    if document.get("wrote"):
        parts.append(note(T.wrote_text(document["wrote"], document.get("rows_written")), warn=True))
    value = document.get("value")
    if is_table(document):
        parts += table_view(document, key, unit)
    elif value is None:
        parts.append(note(T.NO_VALUE))
    elif isinstance(value, list):
        parts.append(list_view(value, unit))
    elif isinstance(value, dict):
        parts.append(mapping_view(value))
    else:
        parts.append(scalar_view(value, unit, document.get("interval")))
    return parts


def scalar_view(
    value: Any, unit: str | None, interval: dict[str, Any] | None, more: list[Any] | None = None
) -> html.Div:
    parts: list[Any] = [html.Span(T.number_text(value), className="answer-value")]
    if unit:
        parts += [" ", html.Span(unit, className="answer-unit")]
    if interval:
        parts.append(html.Div(T.interval_text(interval, unit), className="answer-interval"))
    if more:
        parts.append(html.Div(more, className="muted"))
    return html.Div(parts, className="answer-scalar")


def list_view(value: list[Any], unit: str | None) -> html.Ol:
    """A list told item by item; a path by the objects it passes."""
    items = []
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get("nodes"), list):
            items.append(html.Li(T.path_summary(entry, unit), className="answer-path"))
        else:
            items.append(html.Li(T.value_text(entry)))
    return html.Ol(items, className="answer-list")


def mapping_view(value: dict[str, Any]) -> html.Table:
    rows = [html.Tr([html.Th(name), html.Td(T.value_text(found))]) for name, found in value.items()]
    return html.Table(
        html.Tbody(rows),
        className="table answer-mapping",
    )


def table_view(document: dict[str, Any], key: str, unit: str | None) -> list[Any]:
    rows: list[dict[str, Any]] = document["value"]
    if not rows:
        return [note(T.NO_VALUE)]
    if len(rows) == 1 and not document.get("next_cursor") and "value" in rows[0]:
        # One number worked out over many objects: told as a value, with what it rests on.
        row = rows[0]
        more = [f"{name} {T.value_text(found)}" for name, found in row.items() if name != "value"]
        return [scalar_view(row["value"], unit, document.get("interval"), more)]
    parts: list[Any] = []
    if is_forecast(rows):
        parts += [html.H4(T.FORECAST_HEADING), *forecast_chart(rows, unit)]
    parts += [html.H4(T.ROWS_HEADING), answer_grid(key, list(rows[0]), unit)]
    return parts


def answer_cell(value: Any) -> Any:
    """A cell of an answer's grid: a list of plain values as a list, the rest as the
    sheet shows its cells."""
    if isinstance(value, list) and all(not isinstance(item, dict | list) for item in value):
        return ", ".join(T.value_text(item) for item in value)
    return T.cell(value)


def answer_grid(key: str, columns: list[str], unit: str | None) -> dag.AgGrid:
    return dag.AgGrid(
        id={"role": ids.ANSWER_GRID, "ask": key},
        rowModelType="infinite",
        columnDefs=[
            {"field": name, "headerName": T.column_header(name, unit), "tooltipField": name}
            for name in columns
        ],
        defaultColDef={
            "resizable": True,
            "sortable": False,
            "cellDataType": False,
            "minWidth": 110,
            "flex": 1,
        },
        dashGridOptions={
            "cacheBlockSize": BLOCK_ROWS,
            "maxConcurrentDatasourceRequests": 1,
            "infiniteInitialRowCount": 1,
            # Names of an answer's columns may hold dots; they are names, not paths.
            "suppressFieldDotNotation": True,
            "tooltipShowDelay": 300,
        },
        className="answer-grid",
        style={"height": "40vh", "width": "100%"},
    )


def forecast_chart(rows: list[dict[str, Any]], unit: str | None) -> list[Any]:
    """The forecast drawn: per series a line of its mean, its intervals as bands around it,
    the widest the palest."""
    series: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        series.setdefault(str(row[SERIES]), []).append(row)
    levels = sorted(
        {name.removeprefix(LOW) for name in rows[0] if name.startswith(LOW)},
        key=lambda found: -float(found) if found.replace(".", "", 1).isdigit() else 0.0,
    )
    figure = go.Figure()
    drawn = sorted(series)[:SERIES_DRAWN]
    for index, uid in enumerate(drawn):
        colour = PALETTE[index % len(PALETTE)]
        found = sorted(series[uid], key=lambda row: str(row[TIME]))
        times = [row[TIME] for row in found]
        for depth, level in enumerate(levels):
            low = [row.get(f"{LOW}{level}") for row in found]
            high = [row.get(f"{HIGH}{level}") for row in found]
            figure.add_trace(
                go.Scatter(
                    x=times,
                    y=high,
                    mode="lines",
                    line={"width": 0, "color": colour},
                    legendgroup=uid,
                    showlegend=False,
                    hoverinfo="skip",
                    name=T.band_name(uid, level),
                )
            )
            figure.add_trace(
                go.Scatter(
                    x=times,
                    y=low,
                    mode="lines",
                    line={"width": 0, "color": colour},
                    fill="tonexty",
                    fillcolor=_tint(colour, 0.12 + 0.1 * depth),
                    legendgroup=uid,
                    showlegend=False,
                    name=T.band_name(uid, level),
                )
            )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=[row.get(MEAN) for row in found],
                mode="lines+markers",
                line={"color": colour, "width": 2},
                legendgroup=uid,
                name=T.mean_name(uid),
            )
        )
    figure.update_layout(
        template="simple_white",
        margin={"l": 50, "r": 10, "t": 10, "b": 40},
        height=360,
        yaxis_title=unit or "",
        legend={"orientation": "v"},
    )
    parts: list[Any] = [
        dcc.Graph(
            id=ids.ANSWER_CHART,
            figure=figure,
            config={"displaylogo": False, "responsive": True},
            className="answer-chart",
        )
    ]
    if len(series) > len(drawn):
        parts.append(note(T.series_cap_text(len(drawn), len(series))))
    return parts


def _tint(colour: str, alpha: float) -> str:
    """`#0072B2` at a transparency, for a band."""
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha:.2f})"


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output({"role": ids.ANSWER_GRID, "ask": MATCH}, "getRowsResponse"),
        Input({"role": ids.ANSWER_GRID, "ask": MATCH}, "getRowsRequest"),
    )
    def answer_rows(request: dict[str, Any] | None) -> Any:
        grid_id = ctx.triggered_id
        if not request or not isinstance(grid_id, dict):
            raise PreventUpdate
        key = str(grid_id["ask"])
        start = int(request.get("startRow", 0))
        end = int(request.get("endRow", start + BLOCK_ROWS))

        def read() -> tuple[list[dict[str, Any]], int | None]:
            asked = backend.answers.get(key)
            if asked is None:
                log.warn("the rows of an answer were asked for after they were let go")
                raise ApiProblem(code="ui.answer_gone", title=T.ANSWER_GONE, detail=T.ANSWER_GONE)
            held = len(asked.rows)
            if end <= held or asked.next_cursor is None:
                rows = asked.rows[start:end]
                return [_cells(row) for row in rows], held if asked.next_cursor is None else None
            block = backend.pages.block(
                ROUTE,
                path={"id": asked.question},
                params=asked.params,
                start=start,
                end=end,
                page_max=BLOCK_ROWS,
                generation=key,
                posted=asked.posted(),
            )
            return [_cells(row) for row in block.rows], block.last_row

        return block_response(ids.ANSWER_FEEDBACK, "a block of an answer", read, start)


def _cells(row: dict[str, Any]) -> dict[str, Any]:
    return {name: answer_cell(value) for name, value in row.items()}


__all__ = ["ask", "is_table", "register", "view"]
