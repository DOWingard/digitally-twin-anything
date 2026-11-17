"""The branch selector: the live twin, or one of its scenarios, for every tab to read.

The scenarios offered are those whose branch exists (`/scenarios` beside
`/history/branches`), newest base first. They are read when the page loads
and when the history tab's data is read again: on its Refresh, and at each
new head of the live twin for a session that follows it (a scenario's record
is a version of the live twin, so making, running or deleting one moves its
head). A session that stays on a version reads nothing more than before.

The chosen branch is held with what the slider needs: a scenario's versions
run from its base to its head. Choosing the live twin again is handled by the
header, which restores the version and the following the session had.
"""

from __future__ import annotations

import time
from typing import Any

from dash import Dash, Input, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate
from dta_core.log import Log

from . import ids
from . import text as T
from .backend import Backend
from .feedback import guarded, note
from .header import FOLLOW_ON
from .paging import read_all
from .parts import LIVE_BRANCH

log = Log("ui.branches")

SCENARIOS_MAX = 500


def live_option() -> dict[str, str]:
    return {"label": T.LIVE_OPTION, "value": LIVE_BRANCH}


def known_scenarios(
    scenarios: list[dict[str, Any]], branches: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The scenarios whose branch exists, by branch name, newest base first."""
    heads = {str(found.get("name")): found.get("head") or {} for found in branches}
    found: list[dict[str, Any]] = []
    for scenario in scenarios:
        branch = str(scenario.get("branch") or "")
        if not branch or branch == LIVE_BRANCH or branch not in heads:
            continue
        head = heads[branch]
        found.append(
            {
                "branch": branch,
                "id": scenario.get("id"),
                "name": scenario.get("name") or scenario.get("id"),
                "base_t": scenario.get("base_t"),
                "base_version": scenario.get("base_version"),
                "status": scenario.get("status"),
                "head_id": head.get("id"),
                "head_seq": head.get("seq"),
            }
        )
    # Newest base first; scenarios of one base by name.
    found.sort(key=lambda entry: str(entry["name"]))
    found.sort(key=lambda entry: str(entry["base_t"] or ""), reverse=True)
    return {entry["branch"]: entry for entry in found}


def options(known: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [live_option()] + [
        {
            "label": T.scenario_option(entry["name"], entry["base_t"], entry["status"]),
            "value": branch,
        }
        for branch, entry in known.items()
    ]


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.BRANCHES, "data"),
        Output(ids.BRANCH_SELECT, "options"),
        Output(ids.BRANCH_FEEDBACK, "children"),
        Input(ids.HEALTH, "data"),
        Input(ids.LOGS_REFRESH, "n_clicks"),
        State(ids.BRANCHES, "data"),
        State(ids.FOLLOW, "value"),
        State(ids.BRANCH, "data"),
    )
    @guarded(2)
    def list_branches(
        health: dict[str, Any] | None,
        _refresh: int | None,
        known: Any,
        follow: list[str] | None,
        chosen: dict[str, Any] | None,
    ) -> Any:
        """The scenarios offered: read when the page loads and when the history tab's data
        is read again, which is on its Refresh and, for a session following the live twin,
        at each new head. A session that stays on a version reads nothing more."""
        if ctx.triggered_id == ids.HEALTH:
            following = FOLLOW_ON in (follow or [])
            on_live = (chosen or {}).get("branch", LIVE_BRANCH) == LIVE_BRANCH
            if not health or (known is not None and not (following and on_live)):
                raise PreventUpdate
        started = time.perf_counter()
        scenarios = read_all(backend.client, "/scenarios", {}, cap=SCENARIOS_MAX)
        branches = backend.client.get("/history/branches").get("data") or []
        found = known_scenarios(scenarios, branches)
        log.info(
            "scenarios read for the branch selector",
            scenarios=len(scenarios),
            offered=len(found),
            branches=len(branches),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        if found == known:
            return no_update, no_update
        return found, options(found)

    @app.callback(
        Output(ids.BRANCH, "data"),
        Output(ids.BRANCH_SELECT, "value"),
        Output(ids.BRANCH_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.BRANCH_SELECT, "value"),
        Input(ids.BRANCHES, "data"),
        State(ids.BRANCH, "data"),
        prevent_initial_call="initial_duplicate",
    )
    @guarded(2)
    def choose_branch(
        value: str | None, known: dict[str, Any] | None, current: dict[str, Any] | None
    ) -> Any:
        """The branch the tabs read; a scenario with its base and head, its base read once
        per choice."""
        name = value or LIVE_BRANCH
        if name == LIVE_BRANCH:
            chosen: dict[str, Any] = {"branch": LIVE_BRANCH}
            if chosen == current:
                raise PreventUpdate
            log.info("the live twin is chosen")
            return chosen, no_update
        entry = (known or {}).get(name)
        if entry is None:
            if known is None:
                raise PreventUpdate
            # Deleted since it was chosen, or never had a branch: the live twin again.
            log.warn("a chosen scenario has no branch any more", branch=name)
            gone = str((current or {}).get("scenario") or name)
            return {"branch": LIVE_BRANCH}, LIVE_BRANCH, note(T.scenario_gone(gone), warn=True)
        same = current and current.get("branch") == name
        if same and current.get("base_version") == entry["base_version"]:
            base_seq = current.get("base_seq")
        else:
            document = backend.client.get(
                "/history/versions/{selector}",
                path={"selector": str(entry["base_version"])},
                params={"branch": name},
            )
            base_seq = int(document["data"]["seq"])
        chosen = {
            "branch": name,
            "scenario": entry["name"],
            "id": entry["id"],
            "status": entry["status"],
            "base_t": entry["base_t"],
            "base_version": entry["base_version"],
            "base_seq": base_seq,
            "head_id": entry["head_id"],
            "head_seq": entry["head_seq"],
        }
        if chosen == current:
            raise PreventUpdate
        log.info(
            "a scenario is chosen",
            branch=name,
            scenario=entry["id"],
            base_seq=base_seq,
            head_seq=entry["head_seq"],
        )
        return chosen, no_update

    @app.callback(
        Output(ids.SCENARIO_NOTICE, "children"),
        Output(ids.HEADER, "className"),
        Input(ids.BRANCH, "data"),
    )
    def notice(chosen: dict[str, Any] | None) -> Any:
        """A scenario is said in words at the top of the page, and the header is marked."""
        if not chosen or chosen.get("branch", LIVE_BRANCH) == LIVE_BRANCH:
            return [], "header"
        return [
            html.Strong(T.SCENARIO_BADGE, className="scenario-badge"),
            " ",
            html.Span(
                T.scenario_notice(
                    str(chosen.get("scenario")),
                    chosen.get("base_t"),
                    chosen.get("base_seq"),
                    chosen.get("head_seq"),
                )
            ),
        ], "header scenario-shown"


__all__ = ["known_scenarios", "register"]
