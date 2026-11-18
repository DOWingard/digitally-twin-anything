"""Small pieces the tabs share: layout parts, and what a selected version names."""

from __future__ import annotations

from typing import Any

import dash_ag_grid as dag
from dash import html

# The branch of the live twin; any other branch holds a scenario.
LIVE_BRANCH = "main"


def field(label: str, control: Any) -> html.Div:
    """A filter control with its label above it."""
    return html.Div([html.Label(label, htmlFor=control.id), control], className="field")


def client_grid(
    grid_id: str,
    columns: list[dict[str, Any]],
    row_id: str,
    *,
    height: str | None = "32vh",
    class_name: str = "",
) -> dag.AgGrid:
    """A grid whose rows a callback hands over whole (`rowData`), each row named by
    `row_id` so that rows read again keep their place. Without `height` it grows with its
    rows: for listings the manifest bounds."""
    options: dict[str, Any] = {
        "tooltipShowDelay": 300,
        "rowSelection": {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True},
    }
    if height is None:
        options["domLayout"] = "autoHeight"
    return dag.AgGrid(
        id=grid_id,
        rowData=[],
        columnDefs=columns,
        getRowId=f"params.data.{row_id}",
        defaultColDef={
            "resizable": True,
            "sortable": True,
            "cellDataType": False,
            "minWidth": 90,
            "flex": 1,
        },
        dashGridOptions=options,
        className=class_name,
        style={"width": "100%"} if height is None else {"height": height, "width": "100%"},
    )


def columns_of(headers: dict[str, str], *, tooltips: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Grid columns from `field: header` pairs; the fields named in `tooltips` show their
    whole text on hover."""
    found: list[dict[str, Any]] = []
    for name, header in headers.items():
        column: dict[str, Any] = {"field": name, "headerName": header}
        if name in tooltips:
            column["tooltipField"] = name
        found.append(column)
    return found


def branch_of(version: dict[str, Any] | None) -> str:
    """The branch a selected version is on: every read at it names that branch too."""
    return str((version or {}).get("branch") or LIVE_BRANCH)


def at_version(version: dict[str, Any]) -> dict[str, str]:
    """The query that reads at a selected version: its id, on its branch."""
    return {"at": str(version["id"]), "branch": branch_of(version)}
