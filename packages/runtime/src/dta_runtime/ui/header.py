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

from dataclasses import dataclass
from typing import Any

from dash import ALL, ClientsideFunction, Dash, Input, Output, State, ctx, dcc, html, no_update
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .feedback import guarded
from .parts import LIVE_BRANCH, branch_of
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
            dcc.Store(id=ids.BRANCHES),
            dcc.Store(id=ids.BRANCH),
            dcc.Store(id=ids.LIVE_KEPT),
            dcc.Interval(id=ids.POLL, interval=POLL_MS),
            html.Div(
                [
                    html.H1("Digital twin", id=ids.TWIN_NAME, className="twin-name"),
                    html.Span(id=ids.TWIN_TIER, className="badge"),
                    html.Span(id=ids.HEAD_LABEL, className="head-label"),
                ],
                className="header-row",
            ),
            html.Div(id=ids.SCENARIO_NOTICE, className="scenario-notice", role="status"),
            html.Div(id=ids.LIVE_LINE, className="header-row live-line"),
            html.Div(
                [
                    html.Label(
                        T.BRANCH_SELECT_LABEL, htmlFor=ids.BRANCH_SELECT, className="slider-label"
                    ),
                    dcc.Dropdown(
                        id=ids.BRANCH_SELECT,
                        options=[{"label": T.LIVE_OPTION, "value": LIVE_BRANCH}],
                        value=LIVE_BRANCH,
                        clearable=False,
                        searchable=False,
                        className="branch-select",
                    ),
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
                        options=follow_options(following_allowed=True),
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
            html.Div(id=ids.BRANCH_FEEDBACK),
        ],
        id=ids.HEADER,
        className="header",
    )


def follow_options(*, following_allowed: bool) -> list[dict[str, Any]]:
    """A scenario's head does not move while it is shown: following is off and cannot be
    turned on."""
    return [{"label": T.FOLLOW_LABEL, "value": FOLLOW_ON, "disabled": not following_allowed}]


@dataclass(frozen=True)
class Span:
    """The versions the slider runs over: from 0 to the live twin's head, or from a
    scenario's base to its head."""

    branch: str
    low: int
    head: int


def span_of(health: dict[str, Any] | None, branch: dict[str, Any] | None) -> Span | None:
    name = (branch or {}).get("branch") or LIVE_BRANCH
    if name == LIVE_BRANCH:
        head = _head_seq(health)
        return None if head is None else Span(LIVE_BRANCH, 0, head)
    low, head = (branch or {}).get("base_seq"), (branch or {}).get("head_seq")
    if low is None or head is None:
        return None
    return Span(name, int(low), int(head))


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
        Output(ids.SLIDER, "min"),
        Output(ids.SLIDER, "max"),
        Output(ids.SLIDER, "marks"),
        Output(ids.SLIDER, "value"),
        Output(ids.FOLLOW, "value"),
        Output(ids.FOLLOW, "options"),
        Output(ids.SETTLED, "data"),
        Output(ids.LIVE_KEPT, "data"),
        Input(ids.HEALTH, "data"),
        Input(ids.JUMP, "n_clicks"),
        Input(ids.FOLLOW, "value"),
        Input(ids.SLIDER, "value"),
        Input({"role": ids.COMMIT_LOG, "query": ALL}, "cellClicked"),
        Input(ids.BRANCH, "data"),
        State(ids.SETTLED, "data"),
        State(ids.LIVE_KEPT, "data"),
    )
    def steer(
        health: dict[str, Any] | None,
        _jumps: int,
        follow: list[str] | None,
        value: int | None,
        _clicks: list[Any],
        branch: dict[str, Any] | None,
        settled: dict[str, Any] | None,
        kept: dict[str, Any] | None,
    ) -> Any:
        """Where the slider goes. A move the page makes (following, a jump, a click in the
        commit log, a change of branch) is settled at once; a move the operator makes with
        the slider itself is settled in the browser once the slider stays still.

        Leaving the live twin for a scenario keeps the version and the following the
        session had, and goes to the scenario's head; coming back restores them."""
        span = span_of(health, branch)
        if span is None:
            return (no_update,) * 8
        following = FOLLOW_ON in (follow or [])
        trigger = ctx.triggered_id
        on_scenario = span.branch != LIVE_BRANCH
        was = (settled or {}).get("branch") or LIVE_BRANCH
        switching = trigger == ids.BRANCH and settled is not None and was != span.branch
        target: int | None = None
        follow_out: Any = no_update
        kept_out: Any = no_update
        if switching and on_scenario:
            if was == LIVE_BRANCH:
                kept_out = {"seq": settled["seq"], "follow": following}  # type: ignore[index]
            target = span.head
            follow_out = [] if following else no_update
        elif switching:
            restored = kept or {}
            seq = restored.get("seq")
            if restored.get("follow") or not isinstance(seq, int) or not 0 <= seq <= span.head:
                target = span.head
            else:
                target = seq
            if restored.get("follow") and not following:
                follow_out = [FOLLOW_ON]
        elif isinstance(trigger, dict) and trigger.get("role") == ids.COMMIT_LOG:
            clicked = ctx.triggered[0]["value"] if ctx.triggered else None
            seq = _clicked_seq(clicked)
            if seq is not None and span.low <= seq <= span.head:
                target = seq
        elif trigger == ids.JUMP or value is None:
            target = span.head
        elif trigger in (ids.FOLLOW, ids.HEALTH) and following and not on_scenario:
            target = span.head
        if not switching and following and target is not None and target != span.head:
            follow_out = []
        elif trigger == ids.SLIDER and value is not None and value != span.head and following:
            follow_out = []
        value_out = target if target is not None and target != value else no_update
        settle = {"seq": target, "branch": span.branch} if target is not None else None
        settled_out = settle if settle is not None and settle != settled else no_update
        log.debug(
            "version steered",
            trigger=str(trigger),
            clicked=str(ctx.triggered[0]["value"])[:200] if ctx.triggered else None,
            branch=span.branch,
            low=span.low,
            head=span.head,
            value=value,
            target=target,
            following=following,
            switching=switching,
        )
        return (
            span.low,
            span.head,
            {span.low: f"#{span.low}", span.head: f"#{span.head}"},
            value_out,
            follow_out,
            follow_options(following_allowed=not on_scenario),
            settled_out,
            kept_out,
        )

    # The slider's own moves are handed on once it has stayed still for SETTLE_MS.
    app.clientside_callback(
        ClientsideFunction(namespace="dta", function_name="settleVersion"),
        Output(ids.SETTLED, "data", allow_duplicate=True),
        Input(ids.SLIDER, "value"),
        State(ids.SETTLED, "data"),
        State(ids.BRANCH, "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(ids.VERSION, "data"),
        Output(ids.VERSION_DETAIL, "children"),
        Output(ids.VERSION_FEEDBACK, "children"),
        Input(ids.SETTLED, "data"),
        State(ids.VERSION, "data"),
        State(ids.BRANCH, "data"),
    )
    @guarded(2)
    def select_version(
        settled: dict[str, Any] | None,
        known: dict[str, Any] | None,
        branch: dict[str, Any] | None,
    ) -> Any:
        """The version every tab reads, on the branch it was settled on."""
        if not settled or settled.get("seq") is None:
            return no_update, no_update
        name = str(settled.get("branch") or LIVE_BRANCH)
        document = backend.client.get(
            "/history/versions/{selector}",
            path={"selector": f"#{settled['seq']}"},
            params={"branch": name},
        )
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
            "branch": name,
        }
        if name != LIVE_BRANCH and (branch or {}).get("branch") == name:
            version["scenario"] = branch.get("scenario")  # type: ignore[union-attr]
        detail = _describe(version)
        if known and known.get("id") == version["id"] and branch_of(known) == name:
            return no_update, detail
        return version, detail

    @app.callback(
        Output(ids.NEWER, "children"),
        Input(ids.HEALTH, "data"),
        Input(ids.VERSION, "data"),
        Input(ids.BRANCH, "data"),
    )
    def count_newer(
        health: dict[str, Any] | None,
        version: dict[str, Any] | None,
        branch: dict[str, Any] | None,
    ) -> Any:
        """Versions after the selected one on its branch: the live twin's, or the scenario's."""
        span = span_of(health, branch)
        if span is None or not version or branch_of(version) != span.branch:
            return ""
        newer = span.head - int(version["seq"])
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
