"""The fidelity tab against a real API over a real store: the summary, the charts, the
incidents with their steps, the signals, the checks and the notifications are what the
API says, read at the selected version; a failed read is said in place."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance
from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from selenium.webdriver.common.by import By
from ui_page import (
    acting_controls,
    chart_id,
    choose_filter,
    click_row,
    click_tab,
    figure_data,
    figure_layout,
    open_page,
    problem_text,
    text_of,
    trace,
    visible_tabs,
    wait_for,
    wait_grid,
)
from ui_testkit import FIDELITY_TAB_LIMIT_S, FIDELITY_TAB_S, TIMED_RUNS
from ui_trust import CROWDED_INCIDENTS, CROWDED_QUESTIONS, SCORED, Served, changes_asked

DAY = timedelta(days=1)


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def plotted(text: str) -> str:
    return parse(text).strftime("%Y-%m-%d %H:%M:%S")


def open_fidelity(browser: Any, served: Served) -> None:
    open_page(browser, served.ui_url)
    wait_for(browser, lambda: "Fidelity" in visible_tabs(browser), "the fidelity tab")
    click_tab(browser, "Fidelity")


def summary_field(browser: Any, name: str) -> str:
    found = browser.find_elements(By.CSS_SELECTOR, f"#{ids.FIDELITY_SUMMARY} [data-field={name}]")
    return found[0].text if found else ""


def head_t(served: Served) -> datetime:
    return parse(served.get("/meta/health")["head"]["t"])


def errors_since(served: Served, question: str, since: datetime | None) -> list[dict[str, Any]]:
    params = {"from": iso(since)} if since else {}
    return served.every(f"/fidelity/questions/{question}/errors", **params)


def incident_rows(found: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            incident["kind"],
            incident["subject"],
            T.incident_status_text(incident["status"]),
            incident["rung"],
            incident["attempt"],
            incident["cycle"],
            T.time_text(incident["detail"].get("next_cycle_at")),
            T.time_text(incident["opened_at"]),
            T.time_text(incident["resolved_at"]),
        )
        for incident in found
    ]


def shown_incidents(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            row["kind"],
            row["subject"],
            row["status"],
            row["rung"],
            row["attempt"],
            row["cycle"],
            row["next_cycle_at"],
            row["opened_at"],
            row["resolved_at"],
        )
        for row in rows
    ]


def steps_shown(browser: Any) -> list[list[str]]:
    rows = browser.find_elements(
        By.CSS_SELECTOR, f"#{ids.INCIDENT_DETAIL} .incident-steps tbody tr"
    )
    return [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in rows]


def test_the_summary_and_every_grid_are_what_the_api_says(fidelity_world, browser):
    served = fidelity_world.served
    open_fidelity(browser, served)
    overview = served.get("/fidelity")
    every = served.every("/fidelity/incidents")
    wait_for(browser, lambda: summary_field(browser, "state"), "the summary")
    assert summary_field(browser, "state") == T.state_text("correcting") == "◐ correcting"
    assert summary_field(browser, "since") == T.time_text(overview["since"])
    assert summary_field(browser, "check") == (
        f"{overview['check']['uid']} at {T.time_text(overview['check']['at'])}"
    )
    counts = Counter(found["status"] for found in every)
    assert counts == {"open": 1, "remodel_requested": 1, "resolved": 1}
    assert summary_field(browser, "incidents") == "1 open, 1 remodel_requested, 1 resolved"
    assert summary_field(browser, "signals") == "1: Gauge.flow of gauge:G2 (stuck)"

    # Incidents, newest first, as the API lists them.
    shown = wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: len(rows) == 3, "the incidents")
    assert shown_incidents(shown) == incident_rows(every)
    assert [row["status_key"] for row in shown] == [found["status"] for found in every]

    signals = served.get("/fidelity/signals")["data"]
    shown = wait_grid(browser, ids.SIGNALS_GRID, lambda rows: len(rows) == 2, "the signals")
    expected = []
    for signal in signals:
        estimate = signal["detail"].get("estimate") or {}
        suspect = signal["status"] == "suspect"
        reading = T.value_text(signal["reading"])
        expected.append(
            {
                "object": signal["object"],
                "property": signal["property"],
                "status": signal["status"],
                "reason": signal["reason"],
                "since": T.time_text(signal["since"]),
                "reading": f"{reading} ({T.time_text(signal['reading_at'])})",
                "inferred": (
                    f"{T.number_text(estimate['value'])} ({estimate['model']})" if suspect else ""
                ),
                "clear_checks": signal["clear_checks"],
            }
        )
    assert [{name: row[name] for name in expected[0]} for row in shown] == expected
    # The suspect flow of G2 is in force as the flow model estimated it; the level of G1
    # is sound again.
    assert shown[0]["inferred"] == "5 (twin_model:flow/naive)"
    assert shown[1]["inferred"] == ""

    checks = served.get("/fidelity/checks", limit=50)["data"]
    assert 15 <= len(checks) <= 50
    shown = wait_grid(browser, ids.CHECKS_GRID, lambda rows: len(rows) == len(checks), "checks")
    assert [(row["at"], row["state"], row["duration"]) for row in shown] == [
        (
            T.time_text(check["at"]),
            T.state_text(check["state"]),
            T.milliseconds_text(check["duration_ms"]),
        )
        for check in checks
    ]

    listing = served.get("/lifecycle/notifications")
    told = listing["data"]
    assert told and {found["type"] for found in told} >= {
        "twin.fidelity.incident",
        "twin.fidelity.remodel_requested",
    }
    shown = wait_grid(
        browser, ids.NOTIFICATIONS_GRID, lambda rows: len(rows) == len(told), "notifications"
    )
    assert [
        (row["type"], row["subject"], row["time"], row["status"], row["tries"]) for row in shown
    ] == [
        (
            found["type"],
            found["subject"],
            T.time_text(found["time"]),
            found["status"],
            found["tries"],
        )
        for found in told
    ]
    assert text_of(browser, ids.NOTIFICATIONS_STATUS).startswith(f"{len(told)} notifications")
    # Nothing on the tab can change the twin, and the page asked for nothing but reads.
    assert acting_controls(browser, "fidelity-tab") == []
    assert changes_asked(served.api_requests.from_page(0)) == []


def test_each_chart_draws_the_error_series_the_tolerance_and_the_corrections(
    fidelity_world, browser
):
    served = fidelity_world.served
    open_fidelity(browser, served)
    since = head_t(served) - 30 * DAY
    checks = sorted(
        served.every("/fidelity/checks", **{"from": iso(since)}), key=lambda check: check["at"]
    )
    manifest = served.get("/meta/manifest")["manifest"]
    breach = served.every("/fidelity/incidents", kind="breach")
    for question, unit in (("level", "m"), ("flow", "m3/h")):
        errors = errors_since(served, question, since)
        assert len(errors) >= 15
        traces = wait_for(
            browser,
            lambda question=question, errors=errors: (
                (found := figure_data(browser, chart_id(question)))
                and trace(found, "errors")
                and len(trace(found, "errors")["x"]) == len(errors)
                and found
            ),
            f"the chart of {question}",
        )
        points = trace(traces, "errors")
        assert points["x"] == [plotted(row["scored_at"]) for row in errors]
        assert points["y"] == [row["error"] for row in errors]
        assert points["marker"]["symbol"] == [
            "circle" if row["in_window"] else "circle-open" for row in errors
        ]
        line = trace(traces, "window")
        windowed = [
            check
            for check in checks
            if ((check["questions"].get(question) or {}).get("window") or {}).get("error")
            is not None
        ]
        assert line["x"] == [plotted(check["at"]) for check in windowed]
        assert line["y"] == [check["questions"][question]["window"]["error"] for check in windowed]
        tolerance = trace(traces, "tolerance")
        assert tolerance["y"] == [0.5, 0.5]
        assert tolerance["name"] == f"ε = 0.5 {unit}"
        layout = figure_layout(browser, chart_id(question))
        assert layout["yaxis"]["title"]["text"] == f"error, mae [{unit}]"
        latest = parse(checks[-1]["at"])
        window = timedelta(hours=6)
        assert manifest["fidelity"]["window"] == "PT6H"
        (shaded,) = [shape for shape in layout.get("shapes", []) if shape.get("type") == "rect"]
        assert (shaded["x0"], shaded["x1"]) == (
            (latest - window).strftime("%Y-%m-%d %H:%M:%S"),
            latest.strftime("%Y-%m-%d %H:%M:%S"),
        )
        corrections = [
            step["at"]
            for incident in breach
            if incident["subject"] == question
            for step in incident["steps"]
            if step["action"] == "result"
        ]
        marks = trace(traces, "corrections")
        if corrections:
            assert marks["x"] == [plotted(moment) for moment in sorted(corrections)]
        else:
            assert marks is None
    # The level's breach was corrected twice, by recalibration and by reselection.
    level = figure_data(browser, chart_id("level"))
    assert len(trace(level, "corrections")["x"]) == 2


def test_an_incident_shows_its_steps_and_what_a_remodel_request_waits_for(fidelity_world, browser):
    served = fidelity_world.served
    open_fidelity(browser, served)
    (breach,) = served.every("/fidelity/incidents", kind="breach")
    found = served.get(f"/fidelity/incidents/{breach['uid']}")["data"]
    assert found["status"] == "remodel_requested"
    wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: len(rows) == 3, "the incidents")
    click_row(browser, ids.INCIDENTS_GRID, breach["uid"], "kind")
    wait_for(
        browser,
        lambda: text_of(browser, ids.INCIDENT_DETAIL).startswith("breach incident of level"),
        "the breach's steps",
    )
    detail = text_of(browser, ids.INCIDENT_DETAIL)
    until = parse(found["requested_at"]) + timedelta(
        seconds=found["detail"]["remodel"]["timeout_s"]
    )
    assert f"re-model the model specification level until {T.time_text(iso(until))}" in detail
    assert "twin_model:level/naive" in detail

    steps = steps_shown(browser)
    assert [row[1] for row in steps] == [step["action"] for step in found["steps"]]
    assert [row[0] for row in steps] == [T.time_text(step["at"]) for step in found["steps"]]
    opened = found["steps"][0]["error"]
    assert steps[0][4:6] == ["", T.measure_text(opened, "m")]
    assert steps[1][2:6] == [
        "recalibrate, attempt 0, cycle 1",
        "asked for",
        T.measure_text(opened, "m"),
        "",
    ]
    assert steps[2][2:4] == ["recalibrate", "not accepted"]
    assert steps[3][2] == "reselect, attempt 0, cycle 1"
    assert steps[4][2:4] == ["reselect", "not accepted"]
    assert steps[5][2:4] == ["asked the managing agent to re-model", "waits at most 2 h"]
    # The cycle of each step and when the next cycle is due, as the record says.
    assert [row[6:8] for row in steps] == [
        [
            "" if step.get("cycle") is None else str(step["cycle"]),
            T.time_text(step.get("next_cycle_at")),
        ]
        for step in found["steps"]
    ]

    resolved = served.every("/fidelity/incidents", status="resolved")[0]
    click_row(browser, ids.INCIDENTS_GRID, resolved["uid"], "kind")
    wait_for(
        browser,
        lambda: "resolved 2026-01-05 07:00:00 UTC" in text_of(browser, ids.INCIDENT_DETAIL),
        "the resolved incident",
    )
    assert [row[1] for row in steps_shown(browser)] == ["flagged", "cleared"]
    assert steps_shown(browser)[1][3] == "cleared by check"


def test_the_incidents_filter_by_status(fidelity_world, browser):
    served = fidelity_world.served
    open_fidelity(browser, served)
    wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: len(rows) == 3, "every incident")
    for status, count in (("resolved", 1), ("unresolved", 2), ("remodel_requested", 1)):
        choose_filter(browser, ids.INCIDENTS_STATUS, status)
        expected = served.every("/fidelity/incidents", status=status)
        assert len(expected) == count
        shown = wait_grid(
            browser,
            ids.INCIDENTS_GRID,
            lambda rows, expected=expected: (
                [row["uid"] for row in rows] == [found["uid"] for found in expected]
            ),
            f"the {status} incidents",
        )
        assert shown_incidents(shown) == incident_rows(expected)


def test_the_range_reads_further_back_than_30_days(crowded, browser):
    open_fidelity(browser, crowded)
    t = head_t(crowded)
    for choice, days in ((None, 30), ("everything", None), ("last 7 days", 7)):
        if choice:
            choose_filter(browser, ids.FIDELITY_RANGE, choice)
        since = t - days * DAY if days else None
        for question in CROWDED_QUESTIONS:
            errors = errors_since(crowded, question, since)
            traces = wait_for(
                browser,
                lambda question=question, errors=errors: (
                    (found := figure_data(browser, chart_id(question)))
                    and trace(found, "errors")
                    and len(trace(found, "errors")["x"]) == len(errors)
                    and found
                ),
                f"{question} over {choice or 'the default range'}",
            )
            assert trace(traces, "errors")["y"] == [row["error"] for row in errors]
        counted = len(errors_since(crowded, "level", since))
        if days == 30:
            assert SCORED <= counted < len(errors_since(crowded, "level", None))
        if days == 7:
            assert 0 < counted < SCORED


def test_the_fidelity_tab_is_drawn_in_time(crowded, browser):
    since = head_t(crowded) - 30 * DAY
    expected = {
        question: len(errors_since(crowded, question, since)) for question in CROWDED_QUESTIONS
    }
    assert all(count >= SCORED for count in expected.values())
    incidents = len(crowded.every("/fidelity/incidents"))
    assert incidents == CROWDED_INCIDENTS + 1

    def drawn() -> bool:
        if not summary_field(browser, "state") or not text_of(browser, ids.NOTIFICATIONS_STATUS):
            return False
        for question, count in expected.items():
            found = figure_data(browser, chart_id(question))
            points = trace(found, "errors") if found else None
            if not points or len(points["x"]) != count:
                return False
        shown = wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: True, "incidents")
        checks = wait_grid(browser, ids.CHECKS_GRID, lambda rows: True, "checks")
        return len(shown) == incidents and len(checks) == 50

    for _ in range(TIMED_RUNS):
        open_page(browser, crowded.ui_url)
        wait_for(browser, lambda: "Fidelity" in visible_tabs(browser), "the fidelity tab")
        started = time.perf_counter()
        click_tab(browser, "Fidelity")
        wait_for(browser, drawn, "the whole fidelity tab", timeout=30.0)
        FIDELITY_TAB_S.append(time.perf_counter() - started)
    assert max(FIDELITY_TAB_S[-TIMED_RUNS:]) <= FIDELITY_TAB_LIMIT_S, FIDELITY_TAB_S


def test_an_incident_gone_at_a_newer_version_is_said_in_place(make_badge, browser):
    world = make_badge()
    world.healthy()
    world.correcting()
    open_fidelity(browser, world.served)
    (reconcile,) = world.served.every("/fidelity/incidents", kind="reconcile")
    wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: len(rows) == 1, "the incident")
    click_row(browser, ids.INCIDENTS_GRID, reconcile["uid"], "kind")
    wait_for(
        browser,
        lambda: text_of(browser, ids.INCIDENT_DETAIL).startswith("reconcile incident of"),
        "the incident's steps",
    )
    store = world.plant.twin.store
    later = store.head().t + timedelta(minutes=1)
    store.commit(
        ChangeSet(
            deletes={"TwinIncident": [reconcile["uid"]]},
            provenance=Provenance(source="test", kind=FactKind.inferred, t_valid=later),
        ),
        CommitMeta(cause="registration", actor="test", t=later),
    )
    # The page follows the head: the chosen incident is read again there, and is gone.
    problem = wait_for(
        browser, lambda: problem_text(browser, ids.INCIDENT_FEEDBACK), "the incident's 404", 20.0
    )
    assert "fidelity.not_found" in problem
    wait_grid(browser, ids.INCIDENTS_GRID, lambda rows: rows == [], "the incident gone")
    # The rest of the tab is drawn at the new version.
    assert summary_field(browser, "state")
    assert not problem_text(browser, ids.FIDELITY_FEEDBACK)
