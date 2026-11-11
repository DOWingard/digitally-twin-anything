"""The header shown above every tab: which twin, which version, and whether to follow the head.

The selected version lives in the browser as its id, seq and description;
every read the tabs make passes that id as `at`. The page polls
`/meta/health` every few seconds. Following the head, it moves to each new
head; not following, it stays and counts the versions written since.
Choosing an older version by hand stops following, as scrolling up stops a
log from tailing.

One poll feeds two stores. The first holds what changes only with the head
or the twin, so that nothing which depends on the version runs on a poll that
brought no new head. The second holds what a live twin changes on every poll:
its clock, how old its data is against what its contract allows, its last
run and the sync job in progress. A version is read only once the slider has
stayed still for a moment, so stepping through versions reads only the one
the operator stops at.
"""

from __future__ import annotations

from typing import Any

from dash import ALL, ClientsideFunction, Dash, Input, Output, State, ctx, dcc, html, no_update
from dta_core.log import Log

from . import ids
from .backend import Backend
from .feedback import guarded
from .text import duration_text, minute_text, plural, seconds_text, time_text

log = Log("ui.header")

POLL_MS = 5_000
# How long the slider must stay still before its version is read; the same value as
# SETTLE_MS in assets/clientside.js.
SETTLE_MS = 300
FOLLOW_ON = "follow"
ACTIVE_JOB_STATES = ("queued", "running")


def layout() -> html.Header:
    return html.Header(
        [
            dcc.Store(id=ids.HEALTH),
            dcc.Store(id=ids.LIVE),
            dcc.Store(id=ids.SYNC_KEY),
            dcc.Store(id=ids.VERSION),
            dcc.Store(id=ids.SETTLED),
            dcc.Store(id=ids.GRIDS_REFRESHED),
            dcc.Interval(id=ids.POLL, interval=POLL_MS),
            html.Div(
                [
                    html.H1("Digital twin", id=ids.TWIN_NAME, className="twin-name"),
                    html.Span(id=ids.TWIN_TIER, className="badge"),
                    html.Span(id=ids.HEAD_LABEL, className="head-label"),
                ],
                className="header-row",
            ),
            html.Div(id=ids.LIVE_LINE, className="header-row live-line"),
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
        Output(ids.LIVE, "data"),
        Output(ids.SYNC_KEY, "data"),
        Output(ids.HEALTH_FEEDBACK, "children"),
        Input(ids.POLL, "n_intervals"),
        State(ids.HEALTH, "data"),
        State(ids.LIVE, "data"),
        State(ids.SYNC_KEY, "data"),
    )
    @guarded(3)
    def poll_health(
        _: int | None,
        known: dict[str, Any] | None,
        known_live: dict[str, Any] | None,
        known_key: dict[str, Any] | None,
    ) -> Any:
        document = backend.client.get("/meta/health")
        health = {
            "twin_id": document.get("twin_id"),
            "name": document.get("name"),
            "tier": document.get("tier"),
            "head": document["head"],
            "versions": document.get("versions"),
            "fidelity": document.get("fidelity"),
        }
        live = {"clock": document.get("clock"), "sync": document.get("sync")}
        key = sync_key(live["sync"])
        if key != known_key:
            log.info("the twin's sync state changed", **key)
        # An unchanged answer is not sent on, so that nothing downstream reruns every poll.
        return (
            no_update if health == known else health,
            no_update if live == known_live else live,
            no_update if key == known_key else key,
        )

    @app.callback(Output(ids.LIVE_LINE, "children"), Input(ids.LIVE, "data"))
    def show_live(live: dict[str, Any] | None) -> Any:
        return live_line(live)

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
        Output(ids.SETTLED, "data"),
        Input(ids.HEALTH, "data"),
        Input(ids.JUMP, "n_clicks"),
        Input(ids.FOLLOW, "value"),
        Input(ids.SLIDER, "value"),
        Input({"role": ids.COMMIT_LOG, "query": ALL}, "cellClicked"),
        State(ids.SETTLED, "data"),
    )
    def steer(
        health: dict[str, Any] | None,
        _jumps: int,
        follow: list[str] | None,
        value: int | None,
        _clicks: list[Any],
        settled: int | None,
    ) -> Any:
        """Where the slider goes. A move the page makes (following, a jump, a click in the
        commit log) is settled at once; a move the operator makes with the slider itself
        is settled in the browser once the slider stays still."""
        head = _head_seq(health)
        if head is None:
            return no_update, no_update, no_update, no_update, no_update
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
        settled_out = target if target is not None and target != settled else no_update
        log.debug(
            "version steered",
            trigger=str(trigger),
            clicked=str(ctx.triggered[0]["value"])[:200] if ctx.triggered else None,
            head=head,
            value=value,
            target=target,
            following=following,
        )
        return head, marks, value_out, follow_out, settled_out

    # The slider's own moves are handed on once it has stayed still for SETTLE_MS.
    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="settleVersion"),
        Output(ids.SETTLED, "data", allow_duplicate=True),
        Input(ids.SLIDER, "value"),
        State(ids.SETTLED, "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(ids.VERSION, "data"),
        Output(ids.VERSION_DETAIL, "children"),
        Output(ids.VERSION_FEEDBACK, "children"),
        Input(ids.SETTLED, "data"),
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


def sync_key(sync: dict[str, Any] | None) -> dict[str, Any]:
    """What the sync section shows depends on: it changes when a run finishes or a job
    starts or ends, never with the clock alone."""
    if not sync:
        return {"live": False}
    last = sync.get("last_run") or {}
    job = sync.get("job") or {}
    return {
        "live": True,
        "run": last.get("uid"),
        "run_status": last.get("status"),
        "job": job.get("id"),
        "job_state": job.get("state"),
    }


def clock_kind_text(kind: str | None) -> str:
    """`source:telemetry` -> `source telemetry`."""
    return (kind or "unknown").replace(":", " ", 1)


def live_line(live: dict[str, Any] | None) -> list[Any]:
    """The twin's time, how old its data is against its limit, its last run and the
    run in progress; nothing for a twin that is not live."""
    if not live or not live.get("sync"):
        return []
    clock = live.get("clock") or {}
    sync = live["sync"]
    kind = f"(clock: {clock_kind_text(clock.get('kind'))})"
    parts: list[Any] = []
    if clock.get("available") and clock.get("now"):
        parts.append(
            html.Span(f"twin time {minute_text(clock['now'])} {kind}", className="live-clock")
        )
    else:
        parts.append(html.Span(f"clock unavailable {kind}", className="live-clock unavailable"))
    age = _age(sync)
    if age is not None:
        parts.append(age)
    parts.append(_last_run(sync.get("last_run")))
    job = sync.get("job") or {}
    if job.get("state") in ACTIVE_JOB_STATES:
        parts.append(html.Span("run in progress", className="live-job"))
    line: list[Any] = []
    for part in parts:
        if line:
            line.append(html.Span(" · ", className="live-sep"))
        line.append(part)
    return line


def _age(sync: dict[str, Any]) -> html.Span | None:
    """`data 12 min old (limit 1 h)`, marked stale with the questions that are not fresh;
    None when no question of the contract sets a limit."""
    staleness = sync.get("staleness")
    if not staleness:
        return None
    limit = duration_text(staleness)
    age = sync.get("freshness_s")
    if age is None:
        return html.Span(f"data age unknown (limit {limit})", className="live-age")
    text = f"data {seconds_text(age)} old (limit {limit})"
    if sync.get("fresh") is not False:
        return html.Span(text, className="live-age")
    behind = [
        f"{found['id']} ({seconds_text(found['freshness_s'])} old, limit "
        f"{duration_text(found.get('staleness'))}, waits for {found.get('source') or 'a source'})"
        for found in sync.get("questions") or []
        if found.get("fresh") is False and found.get("freshness_s") is not None
    ]
    parts: list[Any] = [text, " ", html.Strong("stale", className="stale-mark")]
    if behind:
        parts.append(": not fresh " + "; ".join(behind))
    return html.Span(parts, className="live-age stale")


def _last_run(last: dict[str, Any] | None) -> html.Span:
    """`last run succeeded at …`, at the time of the twin's clock when it ran; partial
    and failed runs are marked."""
    if not last:
        return html.Span("no run yet", className="live-run")
    status = last.get("status") or "unknown"
    when = minute_text(last.get("clock_at") or last.get("finished_at"))
    text = f"last run {status}" + (f" at {when}" if when else "")
    return html.Span(text, className=f"live-run run-{status}")


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
