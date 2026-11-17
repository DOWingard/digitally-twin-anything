"""The branch selector against a computed twin with scenarios run through a real worker:
every tab reads the chosen scenario's branch, the slider runs over its versions, the header
says plainly that a scenario is shown, and the live twin comes back as it was left."""

from __future__ import annotations

import time
from typing import Any

from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_branch,
    choose_dropdown,
    choose_object,
    click_graph_element,
    click_tab,
    dropdown_options,
    follow_checkbox,
    graph_nodes,
    grid_cells,
    open_page,
    select_version,
    selected_seq,
    slider_range,
    slider_value,
    text_of,
    wait_for,
)
from ui_testkit import SWITCH_LIMIT_S, SWITCH_S, TIMED_RUNS

TANK_PATH = "/api/v1/objects/Tank"


def label(scenario: dict[str, Any]) -> str:
    return T.scenario_option(scenario["name"], scenario["base_t"], scenario["status"])


def tank_levels(world: Any, **params: Any) -> dict[str, float]:
    rows = world.get("/objects/Tank", limit=1000, **params)["data"]
    return {row["uid"]: row["level"] for row in rows}


def shown_levels(browser: Any) -> dict[str, float]:
    uids = grid_cells(browser, ids.SHEET_BOX, "uid")
    levels = grid_cells(browser, ids.SHEET_BOX, "level")
    if not uids or len(uids) != len(levels) or not all(levels):
        return {}
    return {uid: float(level) for uid, level in zip(uids, levels, strict=True)}


def open_tanks(browser: Any, url: str) -> None:
    open_page(browser, url)
    click_tab(browser, "Spreadsheet")
    wait_for(browser, lambda: " at #" in text_of(browser, ids.SHEET_STATUS), "a sheet")
    choose_dropdown(browser, ids.SHEET_TYPE, "Tank")
    wait_for(browser, lambda: shown_levels(browser), "the tanks")


def test_the_scenarios_whose_branch_exists_are_offered(computed, browser):
    open_page(browser, computed.ui_url)
    scenarios = computed.scenarios
    offered = [scenarios[key] for key in ("as_is", "not_run", "failed", "closed")]
    # One base: by name; the deleted scenario has no branch any more.
    by_name = sorted(offered, key=lambda scenario: scenario["name"])
    expected = [T.LIVE_OPTION] + [label(found) for found in by_name]
    assert wait_for(
        browser,
        lambda: (found := dropdown_options(browser, ids.BRANCH_SELECT)) == expected and found,
        "the scenarios offered",
    )
    assert scenarios["dropped"]["status"] == "deleted"
    assert not scenarios["dropped"]["exists"]
    assert label(scenarios["dropped"]) not in expected
    assert text_of(browser, ids.SCENARIO_NOTICE) == ""


def test_choosing_a_scenario_redraws_the_open_tab_from_its_branch(computed, browser):
    closed = computed.scenarios["closed"]
    base_seq = computed.base.seq
    head = closed["head"]
    live_head = computed.get("/meta/health")["head"]
    simulated = tank_levels(computed, at="head", branch=closed["branch"])
    live = tank_levels(computed)
    assert simulated != live
    open_tanks(browser, computed.ui_url)
    assert shown_levels(browser) == live

    for _ in range(TIMED_RUNS):
        mark = computed.api_requests.mark()
        started = time.monotonic()
        choose_branch(browser, label(closed))
        wait_for(browser, lambda: shown_levels(browser) == simulated, "the simulated levels")
        SWITCH_S.append(time.monotonic() - started)
        tanks = [seen for seen in computed.api_requests.from_page(mark) if seen.path == TANK_PATH]
        assert tanks and all(seen.value("branch") == closed["branch"] for seen in tanks)
        assert {seen.value("at") for seen in tanks} == {head_id(computed, closed)}

        assert selected_seq(browser) == head["seq"]
        assert slider_range(browser) == (base_seq, head["seq"])
        assert not follow_checkbox(browser).is_selected()
        assert not follow_checkbox(browser).is_enabled()
        notice = text_of(browser, ids.SCENARIO_NOTICE)
        assert T.SCENARIO_BADGE in notice and closed["name"] in notice
        assert T.minute_text(closed["base_t"]) in notice
        assert "scenario-shown" in browser.find_element(By.ID, ids.HEADER).get_attribute("class")
        assert text_of(browser, ids.NEWER) == "at head"
        assert f"of the scenario {closed['name']}" in text_of(browser, ids.SHEET_STATUS)

        choose_branch(browser, T.LIVE_OPTION)
        wait_for(browser, lambda: shown_levels(browser) == live, "the live levels again")
        assert selected_seq(browser) == live_head["seq"]
        assert text_of(browser, ids.SCENARIO_NOTICE) == ""
        assert follow_checkbox(browser).is_enabled() and follow_checkbox(browser).is_selected()

    slow = [seconds for seconds in SWITCH_S if seconds > SWITCH_LIMIT_S]
    assert not slow, f"switching branch over {SWITCH_LIMIT_S} s: {SWITCH_S}"


def head_id(world: Any, scenario: dict[str, Any]) -> str:
    return world.get("/history/versions/head", branch=scenario["branch"])["data"]["id"]


def test_the_slider_runs_over_the_scenarios_versions(computed, browser):
    closed = computed.scenarios["closed"]
    base_seq = computed.base.seq
    open_tanks(browser, computed.ui_url)
    choose_branch(browser, label(closed))
    wait_for(browser, lambda: selected_seq(browser) == closed["head"]["seq"], "the scenario head")
    thumb = browser.find_element(By.CSS_SELECTOR, f"#{ids.SLIDER} [role=slider]")
    thumb.send_keys(Keys.HOME)
    wait_for(browser, lambda: selected_seq(browser) == base_seq, "the scenario's base")
    at_base = tank_levels(computed, at=f"#{base_seq}", branch=closed["branch"])
    assert at_base == tank_levels(computed, at=f"#{base_seq}")
    wait_for(browser, lambda: shown_levels(browser) == at_base, "the levels at the base")
    assert text_of(browser, ids.NEWER) == f"{closed['head']['seq'] - base_seq} newer versions"
    thumb.send_keys(Keys.ARROW_LEFT)
    time.sleep(1.0)
    assert slider_value(browser) == base_seq, "the slider goes no lower than the base"
    browser.find_element(By.ID, ids.JUMP).click()
    wait_for(browser, lambda: selected_seq(browser) == closed["head"]["seq"], "the head again")


def test_the_live_twin_comes_back_as_it_was_left(computed, browser):
    open_page(browser, computed.ui_url)
    live_head = computed.get("/meta/health")["head"]["seq"]
    select_version(browser, 40)
    assert not follow_checkbox(browser).is_selected()
    choose_branch(browser, label(computed.scenarios["as_is"]))
    wait_for(
        browser,
        lambda: selected_seq(browser) == computed.scenarios["as_is"]["head"]["seq"],
        "the scenario's head",
    )
    # Another scenario, then the live twin: the session is at #40 again, not following.
    choose_branch(browser, label(computed.scenarios["not_run"]))
    wait_for(browser, lambda: selected_seq(browser) == computed.base.seq, "the scenario's head")
    assert slider_range(browser) == (computed.base.seq, computed.base.seq)
    choose_branch(browser, T.LIVE_OPTION)
    wait_for(browser, lambda: selected_seq(browser) == 40, "#40 again")
    assert slider_value(browser) == 40
    assert slider_range(browser) == (0, live_head)
    assert not follow_checkbox(browser).is_selected()


def test_a_scenarios_values_are_marked_in_their_provenance(computed, browser):
    closed = computed.scenarios["closed"]
    open_page(browser, computed.ui_url)
    choose_branch(browser, label(closed))
    wait_for(browser, lambda: selected_seq(browser) == closed["head"]["seq"], "the scenario head")
    choose_object(browser, "node:T1", "node:T1")
    wait_for(browser, lambda: "node:T1" in graph_nodes(browser), "T1 drawn")
    click_graph_element(browser, "node:T1")

    def level_row() -> Any:
        found = browser.find_elements(
            By.CSS_SELECTOR, f"#{ids.GRAPH_DETAILS} tr[data-property='level']"
        )
        return found[0] if found and "simulated" in found[0].text else None

    row = wait_for(browser, level_row, "T1's level and its provenance")
    assert T.FACT_MARKS["simulated"] in row.text
    assert "fact-simulated" in row.get_attribute("class")

    click_graph_element(browser, "Valve|link:V1|node:J1|node:T1")

    def status_row() -> Any:
        found = browser.find_elements(
            By.CSS_SELECTOR, f"#{ids.GRAPH_DETAILS} tr[data-property='status']"
        )
        return found[0] if found and "assumed" in found[0].text else None

    row = wait_for(browser, status_row, "V1's status and its provenance")
    assert T.FACT_MARKS["assumed"] in row.text
    assert "closed" in row.text

    # On the live twin the same values were observed, and nothing is marked.
    choose_branch(browser, T.LIVE_OPTION)
    click_graph_element(browser, "node:T1")
    wait_for(
        browser,
        lambda: (
            "observed" in text_of(browser, ids.GRAPH_DETAILS)
            and not browser.find_elements(By.CSS_SELECTOR, f"#{ids.GRAPH_DETAILS} .fact-mark")
        ),
        "T1 as observed",
    )


def test_a_twin_without_scenarios_offers_the_live_twin_only(plain, browser):
    open_page(browser, plain.ui_url)
    assert plain.get("/scenarios")["data"] == []
    time.sleep(0.5)
    assert dropdown_options(browser, ids.BRANCH_SELECT) == [T.LIVE_OPTION]
    assert follow_checkbox(browser).is_enabled()
    assert text_of(browser, ids.SCENARIO_NOTICE) == ""
