"""The sync section of the history tab: the sources, the runs, the rows a run set aside and
"Run now", as of the twin's head, against a live twin whose runs the test starts."""

from __future__ import annotations

import time

from dta_core.sources import SourceUnavailable
from dta_runtime.ui import ids, sync
from dta_runtime.ui.header import POLL_MS
from live_api_testkit import hidden, plant
from live_testkit import Folder, at, speeds_source
from selenium.webdriver.common.by import By
from ui_page import (
    choose_dropdown,
    click_run,
    grid_cells,
    grid_scroll_top,
    keep_grid,
    open_history,
    run_now,
    run_outcome,
    same_grid,
    scroll_grid,
    select_version,
    set_input,
    source_cells,
    text_of,
    wait_for,
)
from ui_testkit import RUN_NOW_LIMIT_S

DOWN = "the source did not answer in time"
SYNC_READS = ("/api/v1/sync/", "/api/v1/lifecycle/jobs/")
# Twelve pump speeds above the highest the ontology allows, then one that holds; and a
# tank level above the tank's highest.
SPEEDS = [("PU1", at(minute), 5.0 + minute) for minute in range(1, 13)] + [("PU1", at(13), 1.0)]
HIGH_LEVELS = [("T1", at(5), 9.5), ("T2", at(7), 2.5)]


def advance(live, minutes: int) -> None:
    """The clock a step on, with a new level of tank T1 for the run to read."""
    live.folders["scada"].append("levels", [("T1", at(minutes - 2), 1.0 + minutes / 100)])
    live.clock.set(at(minutes))


def test_the_sources_tell_their_state_and_a_failing_source_recovers(make_live, browser):
    live = make_live()
    open_history(browser, live.ui_url)
    scada = wait_for(
        browser,
        lambda: (found := source_cells(browser, "scada")).get("status") == "ok" and found,
        "the sources",
    )
    assert scada["connector"] == "file"
    assert "00:07" in scada["watermark"]
    assert "levels: read to ts" in scada["datasets"] and "2 rows" in scada["datasets"]
    assert not scada["marked"]
    shown = text_of(browser, ids.SOURCES)

    # The section shows the head whatever version is selected.
    select_version(browser, 1)
    assert text_of(browser, ids.SOURCES) == shown

    live.plant.break_("lab", SourceUnavailable(DOWN))
    advance(live, 31)
    assert live.run()["result"]["status"] == "partial"
    lab = wait_for(
        browser,
        lambda: (found := source_cells(browser, "lab")).get("status") == "failing" and found,
        "lab failing",
    )
    assert lab["error"] == f"source.unavailable: {DOWN}"
    assert lab["marked"]
    assert source_cells(browser, "scada")["status"] == "ok"

    # Polls that bring nothing new read nothing for the section.
    mark = live.api_requests.mark()
    deadline = time.monotonic() + 2 * POLL_MS / 1000.0 + 3.0
    while time.monotonic() < deadline:
        polls = [r for r in live.api_requests.from_page(mark) if r.path == "/api/v1/meta/health"]
        if len(polls) >= 2:
            break
        time.sleep(0.2)
    asked = live.api_requests.from_page(mark)
    assert len([r for r in asked if r.path == "/api/v1/meta/health"]) >= 2
    assert not [r.path for r in asked if r.path.startswith(SYNC_READS)]

    live.plant.mend()
    advance(live, 46)
    assert live.run()["result"]["status"] == "succeeded"
    lab = wait_for(
        browser,
        lambda: (found := source_cells(browser, "lab")).get("status") == "ok" and found,
        "lab mended",
    )
    assert lab["error"] == "" and not lab["marked"]


def test_the_runs_grid_pages_through_the_api_keeps_its_place_and_filters(
    make_live, browser, monkeypatch
):
    monkeypatch.setattr(sync, "RUNS_BLOCK_ROWS", 3)
    live = make_live()
    for step in range(1, 12):
        if step in (4, 8):
            live.plant.break_("lab", SourceUnavailable(DOWN))
        advance(live, 16 + 15 * step)
        live.run()
        live.plant.mend()
    replay = live.driver.post(
        "/api/v1/sync/runs", json={"replay": {"source": "lab", "dataset": "readings"}}
    )
    assert replay.status_code == 202, replay.text
    assert live.wait(replay.json()["job"]["id"])["state"] == "succeeded"
    listed = [run["uid"] for run in live.runs()]
    assert len(listed) == 13

    mark = live.api_requests.mark()
    open_history(browser, live.ui_url)
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.RUNS, "uid")[:8] == listed[:8],
        "the newest runs, newest first",
    )
    pages = [r for r in live.api_requests.from_page(mark) if r.path == "/api/v1/sync/runs"]
    assert len(pages) >= 3 and {r.value("limit") for r in pages} == {"3"}
    assert [r for r in pages if r.value("cursor")]
    marked = browser.find_elements(By.CSS_SELECTOR, f"#{ids.RUNS} .ag-row.run-partial")
    assert marked

    # A new run comes into the grid where it stands.
    scroll_grid(browser, ids.RUNS, 60)
    wait_for(browser, lambda: grid_scroll_top(browser, ids.RUNS) == 60, "the scroll")
    keep_grid(browser, ids.RUNS, "runs")
    advance(live, 16 + 15 * 12)
    newest = live.run()["result"]["run"]
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.RUNS, "uid")[:2] == [newest, listed[0]],
        "the new run in the grid",
    )
    assert grid_scroll_top(browser, ids.RUNS) == 60
    assert same_grid(browser, ids.RUNS, "runs")

    # Filters are the API's.
    partial = [run["uid"] for run in live.runs(status="partial")]
    assert len(partial) == 2
    choose_dropdown(browser, ids.RUNS_STATUS, "partial")
    wait_for(browser, lambda: grid_cells(browser, ids.RUNS, "uid") == partial, "partial runs")
    replays = [run["uid"] for run in live.runs(kind="replay")]
    assert len(replays) == 1
    choose_dropdown(browser, ids.RUNS_STATUS, "succeeded")
    choose_dropdown(browser, ids.RUNS_KIND, "replay")
    wait_for(browser, lambda: grid_cells(browser, ids.RUNS, "uid") == replays, "the replay")
    asked = [r for r in live.api_requests.from_page(mark) if r.path == "/api/v1/sync/runs"]
    assert any(r.value("status") == "succeeded" and r.value("kind") == "replay" for r in asked)


def test_the_rows_a_run_set_aside_are_paged_filtered_and_counted(
    make_live, browser, monkeypatch, tmp_path
):
    monkeypatch.setattr(sync, "REJECTS_BLOCK_ROWS", 5)
    root = tmp_path / "plant"
    sources, folders = plant(root, "registry", "scada", levels=HIGH_LEVELS)
    folders["telemetry"] = Folder(root / "telemetry")
    sources["telemetry"] = hidden(speeds_source(folders["telemetry"], SPEEDS), "telemetry")
    live = make_live((sources, folders))
    messy = live.runs()[0]
    assert messy["rows_rejected"] == 13
    live.clock.set(at(31))
    quiet = live.run()["result"]
    assert quiet["rows_rejected"] == 0

    open_history(browser, live.ui_url)
    assert sync.NO_RUN_CHOSEN in text_of(browser, ids.REJECTS)
    mark = live.api_requests.mark()
    click_run(browser, messy["uid"])
    total = f"13 rows set aside by run {messy['uid']}"
    wait_for(browser, lambda: text_of(browser, ids.REJECTS_TOTAL) == total, "the total")
    rules = wait_for(
        browser,
        lambda: len(found := grid_cells(browser, ids.REJECTS, "rule")) == 13 and found,
        "the rows set aside",
    )
    assert sorted(set(rules)) == ["check", "range"] and rules.count("range") == 12
    pages = [
        r
        for r in live.api_requests.from_page(mark)
        if r.path == f"/api/v1/sync/runs/{messy['uid']}/rejects"
    ]
    assert len(pages) >= 3 and {r.value("limit") for r in pages} == {"5"}
    assert [r for r in pages if r.value("cursor")]

    set_input(browser, ids.REJECTS_RULE, "range")
    wait_for(
        browser,
        lambda: (
            text_of(browser, ids.REJECTS_TOTAL)
            == f"12 rows set aside by run {messy['uid']} match the filters"
        ),
        "the total of the range rule",
    )
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.REJECTS, "rule") == ["range"] * 12,
        "only the range rule",
    )
    set_input(browser, ids.REJECTS_RULE, "")
    choose_dropdown(browser, ids.REJECTS_MAPPING, "scada.levels")
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.REJECTS, "mapping") == ["scada.levels"],
        "only the levels",
    )
    assert text_of(browser, ids.REJECTS_TOTAL) == (
        f"1 row set aside by run {messy['uid']} match the filters"
    )

    click_run(browser, quiet["run"])
    wait_for(
        browser,
        lambda: text_of(browser, ids.REJECTS) == f"Run {quiet['run']} set no row aside.",
        "a run that set nothing aside",
    )


def test_run_now_starts_one_run_however_often_it_is_clicked_and_tells_how_it_ended(
    make_live, browser
):
    live = make_live()
    open_history(browser, live.ui_url)
    wait_for(browser, lambda: source_cells(browser, "scada"), "the sources")
    before = len(live.runs())
    mark = live.api_requests.mark()

    gate = live.plant.hold("scada")
    advance(live, 31)
    assert run_now(browser) <= RUN_NOW_LIMIT_S
    button = browser.find_element(By.ID, ids.RUN_NOW)
    for _ in range(5):
        button.click()
        browser.execute_script("arguments[0].click();", button)
    assert gate.reached.wait(30)
    assert not button.is_enabled()
    gate.open()
    text = run_outcome(browser)
    newest = live.runs()[0]
    assert text == (
        f"The run succeeded: 1 version and {newest['rows_written']} "
        f"row{'s' if newest['rows_written'] != 1 else ''} written."
    )
    assert len(live.runs()) == before + 1
    asked = live.api_requests.from_page(mark)
    assert len([r for r in asked if r.method == "POST" and r.path == "/api/v1/sync/runs"]) == 1
    assert button.is_enabled()
    assert "outcome-succeeded" in browser.find_element(
        By.CSS_SELECTOR, f"#{ids.RUN_STATUS} span"
    ).get_attribute("class")

    live.plant.break_("lab", SourceUnavailable(DOWN))
    advance(live, 46)
    assert run_now(browser) <= RUN_NOW_LIMIT_S
    text = run_outcome(browser)
    assert text.startswith("The run was partial: 1 version and ")
    assert text.endswith(f"Failing: lab (source.unavailable: {DOWN})")

    for source in ("registry", "scada"):
        live.plant.break_(source, SourceUnavailable(DOWN))
    live.clock.set(at(61))
    assert run_now(browser) <= RUN_NOW_LIMIT_S
    text = run_outcome(browser)
    assert text.startswith("The run failed: sync.source_failed: ")
    assert len(live.runs()) == before + 3
    assert text_of(browser, ids.RUN_FEEDBACK) == ""


def test_a_static_twin_has_no_sync_to_show_and_no_run_button(stack, browser):
    mark = stack.api_requests.mark()
    open_history(browser, stack.ui_url)
    static = wait_for(browser, lambda: text_of(browser, ids.SYNC_STATIC), "the sync section")
    assert static == sync.STATIC
    assert not browser.find_element(By.ID, ids.RUN_NOW).is_displayed()
    assert not browser.find_element(By.ID, ids.SOURCES).is_displayed()
    assert not [r.path for r in stack.api_requests.since(mark) if r.path.startswith(SYNC_READS)]
