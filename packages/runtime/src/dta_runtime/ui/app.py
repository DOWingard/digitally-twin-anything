"""The browser interface of a twin: a Dash application that reads the twin only through its API.

Every API call is made on the server, inside a callback, with the token from
the settings; the browser receives the data and never the token. All
scripts, styles and fonts are served by the application itself, so the
interface works on a stack without internet access. It answers only requests
addressed to it under a loopback name (`guard`).
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

from dash import Dash, dcc, html
from dta_core.log import Log
from flask import jsonify

from ..settings import UiSettings
from . import graph, guard, header, history, ids, sheet, sync
from .backend import Backend

log = Log("ui")

ASSETS = Path(__file__).parent / "assets"


def layout() -> html.Div:
    return html.Div(
        [
            header.layout(),
            dcc.Tabs(
                id=ids.TABS,
                value="graph",
                children=[
                    dcc.Tab(label="Graph", value="graph", children=graph.layout()),
                    dcc.Tab(label="Spreadsheet", value="sheet", children=sheet.layout()),
                    dcc.Tab(label="History", value="history", children=history.layout()),
                ],
                className="tabs",
            ),
        ],
        className="page",
    )


def create_app(settings: UiSettings) -> Dash:
    """The interface for the twin whose API `settings.api_url` names.

    Nothing is read from the API here: the application starts, and answers
    its health check, whether or not the API is up.
    """
    started = time.perf_counter()
    backend = Backend(settings)
    app = Dash(
        __name__,
        title="Digital twin",
        update_title=None,
        assets_folder=str(ASSETS),
        serve_locally=True,
        suppress_callback_exceptions=True,
    )
    guard.install(app.server)
    app.layout = layout()
    header.register(app, backend)
    graph.register(app, backend)
    sheet.register(app, backend)
    history.register(app, backend)
    sync.register(app, backend)

    @app.server.route("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        return jsonify({"status": "ok"})

    address = urlsplit(settings.api_url)
    log.info(
        "UI created",
        api=f"{address.scheme}://{address.hostname}:{address.port or ''}",
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return app
