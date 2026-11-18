"""The state badge in the header, and the line said under it while the twin is failed.

The badge is drawn from what the header's poll of `/meta/health` already
brought: the fidelity state in a word and a sign, since when, and the incidents
open and failed. It costs no request of its own. A static twin has no fidelity,
and no badge.

The health document counts the failed incidents but does not name them; the
line said while the twin is failed names their subjects, read once each time
the failure changes (another start, another count), never on a poll that
brought nothing new.
"""

from __future__ import annotations

import time
from typing import Any

from dash import Dash, Input, Output, State, html
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .feedback import guarded
from .paging import read_all

log = Log("ui.badge")

FAILED = "failed"
# Shown when the failed incidents cannot be read: the warning stands without their subjects.
WARNING_ONLY = [html.Strong(T.FAILED_WARNING)]


def fidelity_of(document: dict[str, Any]) -> dict[str, Any]:
    """The fidelity part of a health document, with whether the twin is live (a static
    twin has no sync section) and what names its manifest."""
    detail = document.get("fidelity_detail") or {}
    incidents = detail.get("incidents") or {}
    return {
        "live": bool(document.get("sync")),
        "state": document.get("fidelity") or "unknown",
        "since": detail.get("since"),
        "check": detail.get("check"),
        "open": int(incidents.get("open") or 0),
        "failed": int(incidents.get("failed") or 0),
        "suspect_signals": detail.get("suspect_signals"),
        "manifest": (document.get("manifest") or {}).get("hash"),
        "head": (document.get("head") or {}).get("id"),
    }


def badge(health: dict[str, Any] | None) -> tuple[Any, str, bool]:
    """The badge's words, its class and whether it is hidden."""
    if not health or not health.get("live"):
        return [], "state-badge", True
    state = str(health.get("state") or "unknown")
    words = [
        html.Span(T.BADGE_LABEL, className="state-label"),
        " ",
        html.Strong(T.state_text(state), className="state-word"),
        T.badge_details(health.get("since"), health["open"], health["failed"]),
    ]
    return words, f"state-badge state-{state}", False


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.STATE_BADGE, "children"),
        Output(ids.STATE_BADGE, "className"),
        Output(ids.STATE_BADGE, "hidden"),
        Input(ids.FIDELITY_HEALTH, "data"),
    )
    def show_badge(health: dict[str, Any] | None) -> Any:
        return badge(health)

    @app.callback(
        Output(ids.FAILED_LINE, "children"),
        Output(ids.FAILED_SHOWN, "data"),
        Output(ids.FAILED_FEEDBACK, "children"),
        Input(ids.FIDELITY_HEALTH, "data"),
        State(ids.FAILED_SHOWN, "data"),
    )
    @guarded(2, fallback=(WARNING_ONLY, None))
    def failed_line(health: dict[str, Any] | None, shown: dict[str, Any] | None) -> Any:
        """The warning and the subjects of the failed incidents, read once per failure."""
        failed = bool(health and health.get("live") and health.get("state") == FAILED)
        if not failed:
            if shown is None:
                raise PreventUpdate
            log.info("the twin is no longer failed: the failed line goes")
            return [], None
        assert health is not None
        key = {"since": health.get("since"), "failed": health["failed"]}
        if key == shown:
            raise PreventUpdate
        started = time.perf_counter()
        params = {"status": FAILED, "at": health.get("head")}
        found = read_all(backend.client, "/fidelity/incidents", params, cap=50, page=50)
        log.info(
            "the twin is failed: its failed incidents were read for the line under the header",
            incidents=len(found),
            since=health.get("since"),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        line = [*WARNING_ONLY, T.failed_subjects_text(found)]
        return line, key


__all__ = ["badge", "fidelity_of", "register"]
