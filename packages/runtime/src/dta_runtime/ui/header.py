"""The header shown above every tab: which twin, which version, and whether to follow the head.

The selected version lives in the browser as its id, seq and description;
every read the tabs make passes that id as `at`. The page polls
`/meta/health` every few seconds. Following the head, it moves to each new
head; not following, it stays and counts the versions written since.
Choosing an older version by hand stops following, as scrolling up stops a
log from tailing.
"""

from __future__ import annotations

from typing import Any

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from . import ids
from .backend import Backend
from .feedback import guarded
from .text import plural, time_text

POLL_MS = 5_000
FOLLOW_ON = "follow"


def layout() -> html.Header:
    return html.Header(
        [
            dcc.Store(id=ids.HEALTH),
            dcc.Store(id=ids.VERSION),
            dcc.Interval(id=ids.POLL, interval=POLL_MS),
            html.Div(
                [
                    html.H1("Digital twin", id=ids.TWIN_NAME, className="twin-name"),
                    html.Span(id=ids.TWIN_TIER, className="badge"),
                    html.Span(id=ids.HEAD_LABEL, className="head-label"),
                ],
                className="header-row",
            ),
            html.Div(
                [
                    html.Label("Version", htmlFor=ids.SLIDER, className="slider-label"),
                    html.Div(
                        dcc.Slider(
                            id=ids.SLIDER,
                            min=0,
                            max=0,
                            step=1,
                            value=None,
                            marks=None,
                            updatemode="mouseup",
                        ),
                        className="slider-box",
                    ),
                    html.Div(id=ids.VERSION_DETAIL, className="version-detail"),
                ],
                className="header-row",
            ),
            html.Div(
                [
                    html.Button("Jump to head", id=ids.JUMP, n_clicks=0, className="button"),
                    dcc.Checklist(
                        id=ids.FOLLOW,
                        options=[{"label": " Follow head", "value": FOLLOW_ON}],
                        value=[FOLLOW_ON],
                        inline=True,
                        className="follow",
                    ),
                    html.Span(id=ids.NEWER, className="newer"),
                ],
                className="header-row",
            ),
            html.Div(id=ids.HEALTH_FEEDBACK),
            html.Div(id=ids.VERSION_FEEDBACK),
        ],
        className="header",
    )


def _head_seq(health: dict[str, Any] | None) -> int | None:
    if not health:
        return None
    return int(health["head"]["seq"])


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.HEALTH, "data"),
        Output(ids.HEALTH_FEEDBACK, "children"),
        Input(ids.POLL, "n_intervals"),
        State(ids.HEALTH, "data"),
    )
    @guarded(1)
    def poll_health(_: int | None, known: dict[str, Any] | None) -> Any:
        document = backend.client.get("/meta/health")
        health = {
            "twin_id": document.get("twin_id"),
            "name": document.get("name"),
            "tier": document.get("tier"),
            "head": document["head"],
            "versions": document.get("versions"),
            "fidelity": document.get("fidelity"),
        }
        # An unchanged answer is not sent on, so that nothing downstream reruns every poll.
        return no_update if health == known else health

    @app.callback(
        Output(ids.TWIN_NAME, "children"),
        Output(ids.TWIN_TIER, "children"),
        Output(ids.HEAD_LABEL, "children"),
        Input(ids.HEALTH, "data"),
    )
    def show_twin(health: dict[str, Any] | None) -> Any:
        if not health:
            return "Digital twin", "", ""
        head = health["head"]
        name = health.get("name") or health.get("twin_id") or "Digital twin"
        tier = f"tier: {health['tier']}" if health.get("tier") else ""
        versions = health.get("versions")
        counted = f" · {plural(versions, 'version')}" if isinstance(versions, int) else ""
        return name, tier, f"head #{head['seq']} · {time_text(head['t'])}{counted}"

    @app.callback(
        Output(ids.SLIDER, "max"),
        Output(ids.SLIDER, "marks"),
        Output(ids.SLIDER, "value"),
        Output(ids.FOLLOW, "value"),
        Input(ids.HEALTH, "data"),
        Input(ids.JUMP, "n_clicks"),
        Input(ids.FOLLOW, "value"),
        Input(ids.SLIDER, "value"),
        Input({"role": ids.COMMIT_LOG, "query": ALL}, "cellClicked"),
    )
    def steer(
        health: dict[str, Any] | None,
        _jumps: int,
        follow: list[str] | None,
        value: int | None,
        _clicks: list[Any],
    ) -> Any:
        head = _head_seq(health)
        if head is None:
            return no_update, no_update, no_update, no_update
        following = FOLLOW_ON in (follow or [])
        trigger = ctx.triggered_id
        marks = {0: "#0", head: f"#{head}"}
        target: int | None = None
        follow_out: Any = no_update
        if isinstance(trigger, dict) and trigger.get("role") == ids.COMMIT_LOG:
            clicked = ctx.triggered[0]["value"] if ctx.triggered else None
            seq = _clicked_seq(clicked)
            if seq is not None and 0 <= seq <= head:
                target = seq
        elif trigger == ids.JUMP or (trigger == ids.FOLLOW and following) or value is None:
            target = head
        elif trigger == ids.HEALTH and following:
            target = head
        if target is not None and target != head and following:
            follow_out = []
        elif trigger == ids.SLIDER and value is not None and value != head and following:
            follow_out = []
        value_out = target if target is not None and target != value else no_update
        return head, marks, value_out, follow_out

    @app.callback(
        Output(ids.VERSION, "data"),
        Output(ids.VERSION_DETAIL, "children"),
        Output(ids.VERSION_FEEDBACK, "children"),
        Input(ids.SLIDER, "value"),
        State(ids.VERSION, "data"),
    )
    @guarded(2)
    def select_version(seq: int | None, known: dict[str, Any] | None) -> Any:
        if seq is None:
            return no_update, no_update
        document = backend.client.get("/history/versions/{selector}", path={"selector": f"#{seq}"})
        found = document["data"]
        version = {
            "id": found["id"],
            "seq": found["seq"],
            "t": found["t"],
            "cause": found.get("cause"),
            "actor": found.get("actor"),
            "note": found.get("note"),
            "run": found.get("run"),
            "parents": found.get("parents") or [],
        }
        detail = _describe(version)
        if known and known.get("id") == version["id"]:
            return no_update, detail
        return version, detail

    @app.callback(
        Output(ids.NEWER, "children"),
        Input(ids.HEALTH, "data"),
        Input(ids.VERSION, "data"),
    )
    def count_newer(health: dict[str, Any] | None, version: dict[str, Any] | None) -> Any:
        head = _head_seq(health)
        if head is None or not version:
            return ""
        newer = head - int(version["seq"])
        if newer <= 0:
            return "at head"
        return f"{plural(newer, 'newer version')}"


def _clicked_seq(clicked: Any) -> int | None:
    """The seq of the commit log row a click landed on; rows are identified by their seq."""
    if not isinstance(clicked, dict):
        return None
    row = clicked.get("rowId")
    try:
        return int(row) if row is not None else None
    except (TypeError, ValueError):
        return None


def _describe(version: dict[str, Any]) -> list[Any]:
    parts: list[Any] = [
        html.Strong(f"#{version['seq']}"),
        f" {time_text(version['t'])}",
        f" · {version.get('cause') or ''}",
    ]
    if version.get("actor"):
        parts.append(f" by {version['actor']}")
    if version.get("run"):
        parts.append(f" · run {version['run']}")
    if version.get("note"):
        parts.append(html.Em(f" · {version['note']}"))
    parts.append(html.Code(version["id"], className="version-id"))
    return parts
