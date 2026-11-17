"""The scenarios tab against a computed twin with scenarios run through a real worker: the
list says what the API says, and a comparison draws and lists what
`/scenarios/{id}/compare` gives, a missing value as a gap and never as zero."""

from __future__ import annotations

import time
from typing import Any

from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from dta_runtime.ui.scenarios import DRAWN_MAX, largest_differences
from selenium.webdriver.common.by import By
from ui_compute import HORIZON_STEPS, TANKS
from ui_page import (
    choose_dropdown,
    choose_scenario,
    click_tab,
    compare_key,
    compare_traces,
    grid_cells,
    open_page,
    pattern_id,
    pattern_selector,
    scenario_row,
    sort_by,
    text_of,
    wait_for,
)
from ui_testkit import COMPARE_LIMIT_S, COMPARE_S, TIMED_RUNS

TARGETS = ["Tank.level", "Junction.demand"]


def open_scenarios(browser: Any, world: Any) -> None:
    open_page(browser, world.ui_url)
    click_tab(browser, T.SCENARIOS_TAB)
    wait_for(
        browser,
        lambda: scenario_row(browser, world.scenarios["closed"]["id"]),
        "the scenarios list",
        timeout=30.0,
    )


def press_compare(browser: Any) -> None:
    button = browser.find_element(By.ID, ids.COMPARE)
    wait_for(browser, button.is_enabled, "Compare to be enabled")
    button.click()


def drawn(browser: Any, targets: list[str], before: str | None) -> bool:
    """Whether a new comparison is drawn: every target's chart and the grid's first rows."""
    key = compare_key(browser)
    return (
        key is not None
        and key != before
        and all(compare_traces(browser, target) for target in targets)
        and bool(grid_cells(browser, ids.COMPARISON, "uid"))
    )


def assert_drawn_as(browser: Any, rows: list[dict[str, Any]], target: str, sides: dict) -> None:
    """A target's chart draws the objects with the largest differences, each side's values
    as the API gave them, a missing value as a gap."""
    traces = compare_traces(browser, target)
    chosen = largest_differences(rows, target)[:DRAWN_MAX]
    names = [trace["name"] for trace in traces]
    assert names == [T.trace_name(uid, sides[side]) for uid in chosen for side in sides]
    for trace in traces:
        uid = trace["name"].split(" · ", 1)[0]
        side = trace["meta"]
        found = sorted(
            (row for row in rows if row["target"] == target and row["uid"] == uid),
            key=lambda row: row["t"],
        )
        assert trace["x"] == [row["t"] for row in found]
        assert trace["y"] == [row[side] for row in found]
        assert trace["connectgaps"] is False
    scenario_line = next(trace for trace in traces if trace["meta"] == "scenario")
    against_line = next(trace for trace in traces if trace["meta"] == "against")
    # Told apart without colour: a solid line with dots, a dashed one with crosses.
    assert scenario_line["line"]["dash"] == "solid" and against_line["line"]["dash"] == "dash"
    assert scenario_line["marker"]["symbol"] != against_line["marker"]["symbol"]


def test_the_list_says_what_the_api_says(computed, browser):
    open_scenarios(browser, computed)
    listed = computed.get("/scenarios", include_deleted="true", limit=100)["data"]
    assert len(listed) == len(computed.scenarios) == 5
    for scenario in listed:
        shown = wait_for(
            browser,
            lambda scenario=scenario: scenario_row(browser, scenario["id"]),
            scenario["name"],
        )
        assert shown["name"] == scenario["name"]
        assert shown["base_t"] == T.minute_text(scenario["base_t"])
        assert shown["horizon"] == T.duration_text(scenario["horizon"])
        assert shown["status"] == T.scenario_status_text(scenario["status"])
        assert f"scenario-{scenario['status']}" in shown["status_class"]
        assert shown["interventions"] == T.interventions_text(scenario["interventions"])
        assert shown["models"] == ", ".join(scenario["models"])
    closed = computed.scenarios["closed"]
    assert (
        "Valve.status of link:V1 set to closed from 2026-01-05 12:15 UTC"
        in (scenario_row(browser, closed["id"])["interventions"])
    )
    assert scenario_row(browser, computed.scenarios["as_is"]["id"])["interventions"] == (
        T.NO_INTERVENTION
    )


def test_a_comparison_draws_and_lists_what_the_api_gives(computed, browser):
    closed = computed.scenarios["closed"]
    rows = computed.compare("closed", TARGETS)
    assert len(rows) == len(TARGETS) * TANKS * (HORIZON_STEPS + 1)
    open_scenarios(browser, computed)
    choose_scenario(browser, closed["id"], closed["name"])
    assert text_of(browser, ids.SCENARIO_TITLE) == T.scenario_heading(closed)
    choose_dropdown(browser, ids.COMPARE_TARGETS, "Junction.demand", typed="Junction.dem")

    before: str | None = None
    for _ in range(TIMED_RUNS):
        started = time.monotonic()
        press_compare(browser)
        wait_for(
            browser,
            lambda before=before: drawn(browser, TARGETS, before),
            "the comparison",
            timeout=30.0,
        )
        COMPARE_S.append(time.monotonic() - started)
        before = compare_key(browser)

    sides = {"scenario": closed["name"], "against": T.LIVE_OPTION}
    for target in TARGETS:
        assert_drawn_as(browser, rows, target, sides)
    # The live twin has no value after the base: its lines stop there, with no zero.
    against = [
        trace for trace in compare_traces(browser, "Tank.level") if trace["meta"] == "against"
    ]
    assert all(trace["y"][0] is not None for trace in against)
    assert all(value is None for trace in against for value in trace["y"][1:])
    assert text_of(browser, ids.COMPARISON).startswith(
        T.compared_text(closed["name"], T.LIVE_OPTION, len(rows), len(TARGETS))
    )

    # The grid lists the rows in the API's order, then sorted on the server.
    shown = grid_cells(browser, ids.COMPARISON, "uid")
    assert shown == [row["uid"] for row in rows][: len(shown)]
    times = grid_cells(browser, ids.COMPARISON, "t")
    assert times == [row["t"] for row in rows][: len(times)]
    sort_by(browser, ids.COMPARISON, "difference")
    sort_by(browser, ids.COMPARISON, "difference")
    largest = max(row["difference"] for row in rows if row["difference"] is not None)
    wait_for(
        browser,
        lambda: (
            (cells := grid_cells(browser, ids.COMPARISON, "difference"))
            and float(cells[0]) == largest
        ),
        "the largest difference first",
    )
    slow = [seconds for seconds in COMPARE_S if seconds > COMPARE_LIMIT_S]
    assert not slow, f"comparison over {COMPARE_LIMIT_S} s: {COMPARE_S}"


def test_a_scenario_is_compared_with_another_of_its_base(computed, browser):
    closed, as_is = computed.scenarios["closed"], computed.scenarios["as_is"]
    rows = computed.compare("closed", ["Tank.level"], against=as_is["id"])
    assert all(row["against"] is not None for row in rows)
    open_scenarios(browser, computed)
    choose_scenario(browser, closed["id"], closed["name"])
    choose_dropdown(browser, ids.COMPARE_AGAINST, T.against_option(as_is))
    press_compare(browser)
    wait_for(browser, lambda: drawn(browser, ["Tank.level"], None), "the comparison")
    assert_drawn_as(
        browser,
        rows,
        "Tank.level",
        {"scenario": closed["name"], "against": T.against_option(as_is)},
    )
    # With V1 to V14 closed one after another, the tanks fed through them differ most.
    drawn_uids = [
        trace["name"].split(" · ")[0]
        for trace in compare_traces(browser, "Tank.level")
        if trace["meta"] == "scenario"
    ]
    assert len(drawn_uids) == DRAWN_MAX

    # An object not drawn is chosen: it is drawn, and the earliest chosen one is not.
    others = largest_differences(rows, "Tank.level")[DRAWN_MAX:]
    assert pattern_id(browser, "compare-pick", "Tank.level") is not None
    pick = pattern_selector("compare-pick", "Tank.level")
    choose_dropdown(browser, pick, others[-1], typed=others[-1])
    wait_for(
        browser,
        lambda: (
            f"{others[-1]} · {closed['name']}"
            in [trace["name"] for trace in compare_traces(browser, "Tank.level")]
        ),
        "the object chosen to be drawn",
    )
    names = [trace["name"] for trace in compare_traces(browser, "Tank.level")]
    assert len(names) == 2 * DRAWN_MAX
    assert T.trace_name(drawn_uids[0], closed["name"]) not in names
    assert T.picked_cap_text(DRAWN_MAX) in text_of(browser, ids.COMPARISON)


def test_a_scenario_with_nothing_to_compare_says_so(computed, browser):
    open_scenarios(browser, computed)
    mark = computed.api_requests.mark()
    for key in ("not_run", "failed", "dropped"):
        scenario = computed.scenarios[key]
        choose_scenario(browser, scenario["id"], scenario["name"])
        wait_for(
            browser,
            lambda scenario=scenario: (
                text_of(browser, ids.COMPARISON) == T.not_comparable_text(scenario)
            ),
            f"what {scenario['name']} has to compare",
        )
        assert not browser.find_element(By.ID, ids.COMPARE).is_enabled()
        assert compare_key(browser) is None
    assert "failed" in T.not_comparable_text(computed.scenarios["failed"])
    assert computed.scenarios["failed"]["error"] in T.not_comparable_text(
        computed.scenarios["failed"]
    )
    compared = [
        seen for seen in computed.api_requests.from_page(mark) if seen.path.endswith("/compare")
    ]
    assert compared == []
