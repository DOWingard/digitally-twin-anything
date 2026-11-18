"""Which tabs a twin has: a live twin the fidelity tab, a live twin whose manifest declares
actions the actions tab too; a static twin neither.

What decides it is read from the manifest at the head once per manifest (its hash,
as the health poll gives it), never on a poll that brought the same manifest. A
static twin is told by its health alone and costs no read. Once a twin with a
fidelity section is known, a hidden chart is drawn so that the chart library is
loaded before the fidelity tab is opened rather than when it is.
"""

from __future__ import annotations

import time
from typing import Any

from dash import Dash, Input, Output, State, dcc, no_update
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from .backend import Backend
from .feedback import guarded

log = Log("ui.features")

GRAPH_TAB = "graph"
FIDELITY_TAB = "fidelity"
ACTIONS_TAB = "actions"
HIDDEN = {"display": "none"}
SHOWN: dict[str, str] = {}


def features_of(health: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """What the tabs need of a live twin's manifest."""
    fidelity = manifest.get("fidelity") or {}
    return {
        "live": True,
        "hash": health.get("manifest"),
        "fidelity": bool(fidelity.get("interval") and fidelity.get("window")),
        "actions": bool(manifest.get("actions")),
    }


def tab_styles(features: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    live = bool(features and features.get("live"))
    actions = live and bool((features or {}).get("actions"))
    return (SHOWN if live else HIDDEN), (SHOWN if actions else HIDDEN)


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.FEATURES, "data"),
        Output(ids.FEATURES_FEEDBACK, "children"),
        Input(ids.FIDELITY_HEALTH, "data"),
        State(ids.FEATURES, "data"),
    )
    @guarded(1)
    def read_features(health: dict[str, Any] | None, known: dict[str, Any] | None) -> Any:
        if not health:
            raise PreventUpdate
        if not health.get("live"):
            if known == {"live": False}:
                raise PreventUpdate
            log.info("the twin is static: it has neither a fidelity tab nor an actions tab")
            return {"live": False}
        if known and known.get("live") and known.get("hash") == health.get("manifest"):
            raise PreventUpdate
        started = time.perf_counter()
        document = backend.client.get("/meta/manifest", params={"at": health.get("head")})
        found = features_of(health, document.get("manifest") or {})
        log.info(
            "the twin's tabs were read from its manifest",
            manifest=health.get("manifest"),
            fidelity=found["fidelity"],
            actions=found["actions"],
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return found

    @app.callback(
        Output(ids.FIDELITY_TAB, "style"),
        Output(ids.ACTIONS_TAB, "style"),
        Output(ids.TABS, "value"),
        Output(ids.CHART_PRELOAD, "children"),
        Input(ids.FEATURES, "data"),
        State(ids.TABS, "value"),
        State(ids.CHART_PRELOAD, "children"),
        prevent_initial_call=True,
    )
    def show_tabs(features: dict[str, Any] | None, tab: str | None, preloaded: Any) -> Any:
        fidelity_style, actions_style = tab_styles(features)
        # A tab the twin no longer has is left for the graph.
        gone = (tab == FIDELITY_TAB and fidelity_style is HIDDEN) or (
            tab == ACTIONS_TAB and actions_style is HIDDEN
        )
        preload: Any = no_update
        if (features or {}).get("fidelity") and not preloaded:
            preload = dcc.Graph(
                figure={"data": [], "layout": {}},
                config={"staticPlot": True},
                style={"display": "none"},
            )
        return fidelity_style, actions_style, GRAPH_TAB if gone else no_update, preload


__all__ = ["features_of", "register", "tab_styles"]
