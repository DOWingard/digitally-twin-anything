"""The history tab's filters go to the API, and a row of the commit log selects its version."""

from __future__ import annotations

from api_testkit import MATERIAL_SEQ, NETWORK_SEQ, VALVE_REMOVED_SEQ
from dta_runtime.ui import ids
from dta_runtime.ui.client import ApiClient, ApiProblem
from selenium.webdriver.common.by import By
from ui_page import (
    choose_dropdown,
    click_tab,
    follow_checkbox,
    grid_cells,
    open_page,
    selected_seq,
    set_input,
    text_of,
    wait_for,
)

VERSIONS_PATH = "/api/v1/history/versions"


def _log_seqs(browser, expected):
    return wait_for(
        browser,
        lambda: (seqs := grid_cells(browser, ids.LOG_BOX, "seq")) == expected and seqs,
        f"the commit log to show {expected}",
    )


def test_commit_log_filters_go_to_the_api(stack, browser):
    open_page(browser, stack.ui_url)
    click_tab(browser, "History")
    wait_for(browser, lambda: grid_cells(browser, ids.LOG_BOX, "seq"), "the commit log")

    mark = stack.api_requests.mark()
    choose_dropdown(browser, ids.LOG_CAUSE, "manual")
    _log_seqs(browser, [str(MATERIAL_SEQ), str(VALVE_REMOVED_SEQ)])
    assert any(r.value("cause") == "manual" for r in stack.api_requests.since(mark, VERSIONS_PATH))

    browser.find_element(By.CSS_SELECTOR, f"#{ids.LOG_CAUSE} .dash-dropdown-clear").click()
    mark = stack.api_requests.mark()
    set_input(browser, ids.LOG_ACTOR, "gis-import")
    seqs = wait_for(
        browser,
        lambda: (
            (seqs := grid_cells(browser, ids.LOG_BOX, "seq"))
            and str(NETWORK_SEQ) in seqs
            and len(seqs) == 2
            and seqs
        ),
        "the versions written by gis-import",
    )
    assert seqs[-1] == str(NETWORK_SEQ)
    assert any(
        r.value("actor") == "gis-import" and r.value("cause") is None
        for r in stack.api_requests.since(mark, VERSIONS_PATH)
    )

    set_input(browser, ids.LOG_ACTOR, "")
    mark = stack.api_requests.mark()
    set_input(browser, ids.LOG_FROM, "2026-01-05T05:00:00Z")
    set_input(browser, ids.LOG_TO, "2026-01-05T07:00:00Z")
    _log_seqs(browser, ["7", "6", "5"])
    assert any(
        r.value("from") == "2026-01-05T05:00:00Z" and r.value("to") == "2026-01-05T07:00:00Z"
        for r in stack.api_requests.since(mark, VERSIONS_PATH)
    )


def test_a_commit_log_row_selects_its_version(stack, browser):
    open_page(browser, stack.ui_url)
    click_tab(browser, "History")
    choose_dropdown(browser, ids.LOG_CAUSE, "manual")
    _log_seqs(browser, [str(MATERIAL_SEQ), str(VALVE_REMOVED_SEQ)])
    cell = browser.find_element(
        By.CSS_SELECTOR, f"#{ids.LOG_BOX} .ag-row[row-index='1'] .ag-cell[col-id='seq']"
    )
    cell.click()
    wait_for(browser, lambda: selected_seq(browser) == VALVE_REMOVED_SEQ, "the clicked version")
    assert not follow_checkbox(browser).is_selected()
    wait_for(
        browser,
        lambda: f"#{VALVE_REMOVED_SEQ} against" in text_of(browser, ids.DIFF),
        "the changes of the clicked version",
    )


def test_logs_filter_by_level_and_context(stack, browser):
    # A refused call, so that the stack's log holds a warning of the interface's client.
    client = ApiClient(stack.api_url, stack.token)
    try:
        client.get("/objects/{type}", path={"type": "Reading"}, params={"where": ["x:eq:1"]})
    except ApiProblem:
        pass
    finally:
        client.close()

    open_page(browser, stack.ui_url)
    click_tab(browser, "History")
    wait_for(browser, lambda: grid_cells(browser, ids.LOGS_GRID, "level"), "log lines")
    mark = stack.api_requests.mark()
    choose_dropdown(browser, ids.LOGS_LEVEL, "warn")
    levels = wait_for(
        browser,
        lambda: (
            (levels := grid_cells(browser, ids.LOGS_GRID, "level"))
            and set(levels) <= {"warn", "error"}
            and levels
        ),
        "only warnings and errors",
    )
    assert "warn" in levels
    set_input(browser, ids.LOGS_CONTEXT, "ui.client")
    contexts = wait_for(
        browser,
        lambda: (
            (found := grid_cells(browser, ids.LOGS_GRID, "context"))
            and all(context.startswith("ui.client") for context in found)
            and found
        ),
        "only the client's lines",
    )
    assert contexts
    asked = stack.api_requests.since(mark, "/api/v1/lifecycle/logs")
    assert any(r.value("level") == "warn" and r.value("context") == "ui.client" for r in asked)
