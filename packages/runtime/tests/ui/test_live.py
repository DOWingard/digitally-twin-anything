"""A live twin whose head keeps moving, seen through the interface.

Not following, a session stays on its version in every tab and reads nothing
but the twin's health while runs write new heads; following, it moves to each
head and the open tab redraws there without losing its place. The header tells
the twin's time, how old its data is against its contract, and its last run.

Runs are started by the test through the API, with the plant's sources as
folders of CSV files and the API's clock moved by hand.
"""

from __future__ import annotations

import time
from datetime import timedelta

from dta_core.sources import SourceUnavailable
from dta_runtime.ui import ids
from live_testkit import at
from selenium.webdriver.common.by import By
from ui_page import (
    choose_dropdown,
    click_tab,
    follow_checkbox,
    grid_cells,
    grid_scroll_top,
    head_seq,
    header_filter_input,
    keep_grid,
    open_page,
    same_grid,
    scroll_grid,
    select_version,
    selected_cells,
    selected_seq,
    sort_by,
    text_of,
    wait_for,
    wait_graph_at,
)
from ui_testkit import FOLLOW_LIMIT_S, FOLLOW_S

DOWN = "the source did not answer in time"
HEALTH = "/api/v1/meta/health"
# Reads of the twin's data at a version.
VERSIONED = ("/api/v1/objects/", "/api/v1/object/", "/api/v1/graph/", "/api/v1/meta/schema")
TANK = "Tank"


def advance(live, step: int, level: float) -> None:
    """New data for the next run: a level of tank T1, and the clock a quarter of an hour on."""
    minutes = 16 + 15 * step
    live.folders["scada"].append("levels", [("T1", at(minutes - 2), level)])
    live.clock.set(at(minutes))


def sheet_levels(browser) -> dict[str, str]:
    uids = grid_cells(browser, ids.SHEET_BOX, "uid")
    levels = grid_cells(browser, ids.SHEET_BOX, "level")
    return dict(zip(uids, levels, strict=True)) if len(uids) == len(levels) else {}


def open_tank_sheet(browser, seq: int) -> dict[str, str]:
    click_tab(browser, "Spreadsheet")
    choose_dropdown(browser, ids.SHEET_TYPE, TANK)
    wait_for(browser, lambda: f"Tank at #{seq}" in text_of(browser, ids.SHEET_STATUS), "Tank")
    return wait_for(browser, lambda: sheet_levels(browser), "the tanks")


def test_a_session_not_following_stays_on_its_version_then_jumps_to_the_head(make_live, browser):
    live = make_live()
    advance(live, 1, 3.0)
    assert live.run()["state"] == "succeeded"
    advance(live, 2, 3.5)
    assert live.run()["state"] == "succeeded"
    open_page(browser, live.ui_url)
    head = head_seq(browser)
    assert head == live.head_seq()
    chosen = head - 1
    select_version(browser, chosen)
    wait_for(browser, lambda: not follow_checkbox(browser).is_selected(), "follow to stop")
    # `%23` is `#`: the version is named by its seq.
    chosen_id = live.driver.get(f"/api/v1/history/versions/%23{chosen}").json()["data"]["id"]

    # What each tab shows at the chosen version.
    levels = open_tank_sheet(browser, chosen)
    assert levels["node:T1"] == "3"
    click_tab(browser, "History")
    wait_for(
        browser, lambda: selected_cells(browser, ids.LOG_BOX, "seq") == [str(chosen)], "the row"
    )
    click_tab(browser, "Graph")
    nodes = set(wait_graph_at(browser, chosen))

    # Five runs write new heads while the page stays on the graph.
    mark = live.api_requests.mark()
    written = 0
    for step in range(3, 8):
        advance(live, step, 3.5 + step)
        done = live.run()
        assert done["state"] == "succeeded", done
        written += int(done["result"]["versions_written"])
    newest = live.head_seq()
    assert newest == head + written
    wait_for(browser, lambda: head_seq(browser) == newest, "the newest head", FOLLOW_LIMIT_S)
    newer = newest - chosen
    assert text_of(browser, ids.NEWER) == f"{newer} newer versions"
    assert selected_seq(browser) == chosen
    assert f"at #{chosen}" in text_of(browser, ids.GRAPH_STATUS)
    assert set(wait_graph_at(browser, chosen)) == nodes
    asked = live.api_requests.from_page(mark)
    assert asked and {request.path for request in asked} == {HEALTH}

    # Every tab still shows the chosen version, read by its id.
    mark = live.api_requests.mark()
    assert open_tank_sheet(browser, chosen) == levels
    click_tab(browser, "History")
    wait_for(
        browser, lambda: selected_cells(browser, ids.LOG_BOX, "seq") == [str(chosen)], "the row"
    )
    wait_for(browser, lambda: f"#{chosen} against" in text_of(browser, ids.DIFF), "the changes")
    reads = [r for r in live.api_requests.from_page(mark) if r.path.startswith(VERSIONED)]
    assert reads and {r.value("at") for r in reads} == {chosen_id}

    # With the history tab open, a run brings only the twin's health and, in the sync
    # section that shows the head, its sources and runs; nothing at a version.
    wait_for(browser, lambda: grid_cells(browser, ids.RUNS, "uid"), "the runs")
    mark = live.api_requests.mark()
    advance(live, 8, 2.0)
    done = live.run()
    newest = live.head_seq()
    wait_for(browser, lambda: head_seq(browser) == newest, "the newest head", FOLLOW_LIMIT_S)
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.RUNS, "uid")[:1] == [done["result"]["run"]],
        "the new run in the runs grid",
    )
    asked = {request.path for request in live.api_requests.from_page(mark)}
    assert asked <= {HEALTH, "/api/v1/sync/sources", "/api/v1/sync/runs"}
    assert selected_seq(browser) == chosen
    assert text_of(browser, ids.NEWER) == f"{newest - chosen} newer versions"

    # Jump to head: every tab shows the head.
    browser.find_element(By.ID, ids.JUMP).click()
    wait_for(browser, lambda: selected_seq(browser) == newest, "the jump to the head")
    assert text_of(browser, ids.NEWER) == "at head"
    wait_for(browser, lambda: f"#{newest} against" in text_of(browser, ids.DIFF), "the changes")
    wait_for(
        browser,
        lambda: (
            grid_cells(browser, ids.LOG_BOX, "seq")[:1] == [str(newest)]
            and selected_cells(browser, ids.LOG_BOX, "seq") == [str(newest)]
        ),
        "the head selected in the commit log",
    )
    assert open_tank_sheet(browser, newest)["node:T1"] == "2"
    click_tab(browser, "Graph")
    wait_graph_at(browser, newest)


def test_a_session_following_moves_to_each_head_and_keeps_its_place(make_live, browser):
    live = make_live()
    # Enough versions for the commit log to scroll; T1 ends above T2 (2.5).
    for step in range(1, 15):
        advance(live, step, 4.0)
        assert live.run()["state"] == "succeeded"
    open_page(browser, live.ui_url)
    head = head_seq(browser)
    assert head == live.head_seq() and selected_seq(browser) == head
    assert follow_checkbox(browser).is_selected()

    # The spreadsheet, filtered and sorted: the tanks by level, lowest first.
    assert open_tank_sheet(browser, head) == {"node:T1": "4", "node:T2": "2.5"}
    header_filter_input(browser, ids.SHEET_BOX, "name").send_keys("tank")
    sort_by(browser, ids.SHEET_BOX, "level")
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.SHEET_BOX, "uid") == ["node:T2", "node:T1"],
        "the tanks sorted by level",
    )
    keep_grid(browser, ids.SHEET_BOX, "sheet")
    mark = live.api_requests.mark()
    advance(live, 15, 1.5)
    assert live.run()["state"] == "succeeded"
    ended = time.monotonic()
    newest = head + 1
    wait_for(browser, lambda: selected_seq(browser) == newest, "the new head", FOLLOW_LIMIT_S)
    FOLLOW_S.append(time.monotonic() - ended)
    wait_for(
        browser,
        lambda: (
            f"Tank at #{newest}" in text_of(browser, ids.SHEET_STATUS)
            and grid_cells(browser, ids.SHEET_BOX, "uid") == ["node:T1", "node:T2"]
        ),
        "the tanks at the new head, still sorted by level",
    )
    assert sheet_levels(browser) == {"node:T1": "1.5", "node:T2": "2.5"}
    assert header_filter_input(browser, ids.SHEET_BOX, "name").get_attribute("value") == "tank"
    assert same_grid(browser, ids.SHEET_BOX, "sheet")
    newest_id = live.health()["head"]["id"]
    blocks = [
        r
        for r in live.api_requests.from_page(mark)
        if r.path == "/api/v1/objects/Tank" and r.value("at") == newest_id
    ]
    assert blocks and all(
        r.value("sort") == "level" and r.values("where") == ["name:like:%tank%"] for r in blocks
    )

    # The commit log, scrolled: the new head is read into it where it stands.
    click_tab(browser, "History")
    wait_for(browser, lambda: grid_cells(browser, ids.LOG_BOX, "seq"), "the commit log")
    scroll_grid(browser, ids.LOG_BOX, 120)
    wait_for(browser, lambda: grid_scroll_top(browser, ids.LOG_BOX) == 120, "the scroll")
    keep_grid(browser, ids.LOG_BOX, "log")
    advance(live, 16, 1.0)
    assert live.run()["state"] == "succeeded"
    ended = time.monotonic()
    newest += 1
    wait_for(browser, lambda: selected_seq(browser) == newest, "the new head", FOLLOW_LIMIT_S)
    FOLLOW_S.append(time.monotonic() - ended)
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.LOG_BOX, "seq")[:2] == [str(newest), str(newest - 1)],
        "the new head in the commit log",
    )
    assert grid_scroll_top(browser, ids.LOG_BOX) == 120
    assert same_grid(browser, ids.LOG_BOX, "log")

    # The graph.
    click_tab(browser, "Graph")
    wait_graph_at(browser, newest)
    advance(live, 17, 0.75)
    assert live.run()["state"] == "succeeded"
    ended = time.monotonic()
    newest += 1
    wait_for(browser, lambda: selected_seq(browser) == newest, "the new head", FOLLOW_LIMIT_S)
    FOLLOW_S.append(time.monotonic() - ended)
    wait_graph_at(browser, newest)
    assert follow_checkbox(browser).is_selected()
    assert text_of(browser, ids.NEWER) == "at head"


def live_line(browser) -> str:
    return text_of(browser, ids.LIVE_LINE)


def test_the_header_tells_the_twins_time_how_old_its_data_is_and_its_last_run(make_live, browser):
    live = make_live()
    open_page(browser, live.ui_url)
    line = wait_for(browser, lambda: "twin time" in live_line(browser) and live_line(browser), "")
    assert line == (
        "twin time 2026-01-05 00:16 UTC (clock: source plant) · data 9 min old (limit 1 h)"
        " · last run succeeded at 2026-01-05 00:16 UTC"
    )

    # The clock moves past the limit of the levels question: its data, from scada, is stale.
    live.clock.set(at(7) + timedelta(hours=1, minutes=1))
    line = wait_for(browser, lambda: "stale" in live_line(browser) and live_line(browser), "stale")
    assert "twin time 2026-01-05 01:08 UTC" in line
    assert (
        "data 1 h 1 min old (limit 1 h) stale: not fresh levels "
        "(1 h 1 min old, limit 1 h, waits for scada)"
    ) in line
    assert browser.find_element(By.CSS_SELECTOR, f"#{ids.LIVE_LINE} .stale-mark").is_displayed()

    # A run in progress is told while it is held; its end, once it has finished.
    gate = live.plant.hold("scada")
    job = live.start()
    assert gate.reached.wait(30)
    wait_for(browser, lambda: "run in progress" in live_line(browser), "the run in progress")
    gate.open()
    assert live.wait(job)["state"] == "succeeded"
    wait_for(
        browser,
        lambda: (
            "last run succeeded at 2026-01-05 01:08 UTC" in live_line(browser)
            and "run in progress" not in live_line(browser)
        ),
        "the run's end",
    )

    # A partial run, then a failed one, are marked.
    live.plant.break_("lab", SourceUnavailable(DOWN))
    live.clock.set(at(91))
    assert live.run()["result"]["status"] == "partial"
    wait_for(browser, lambda: "last run partial at 2026-01-05 01:31 UTC" in live_line(browser), "")
    assert browser.find_element(By.CSS_SELECTOR, f"#{ids.LIVE_LINE} .run-partial")
    for source in ("registry", "scada"):
        live.plant.break_(source, SourceUnavailable(DOWN))
    live.clock.set(at(106))
    assert live.run()["state"] == "failed"
    wait_for(browser, lambda: "last run failed at 2026-01-05 01:46 UTC" in live_line(browser), "")
    assert browser.find_element(By.CSS_SELECTOR, f"#{ids.LIVE_LINE} .run-failed")

    # A clock that cannot be read: no time, and no age measured.
    live.clock.down = True
    line = wait_for(
        browser,
        lambda: "clock unavailable" in live_line(browser) and live_line(browser),
        "the clock unavailable",
    )
    assert line.startswith("clock unavailable (clock: source plant) · data age unknown (limit 1 h)")
    assert "twin time" not in line


def test_a_static_twin_has_no_live_line(stack, browser):
    open_page(browser, stack.ui_url)
    wait_graph_at(browser, head_seq(browser))
    assert live_line(browser) == ""
    assert not browser.find_element(By.ID, ids.LIVE_LINE).is_displayed()
