"""The API token stays on the server: not in the page, not in anything the browser receives."""

from __future__ import annotations

import json

from api_testkit import node
from dta_runtime.ui import ids
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_dropdown,
    click_graph_element,
    click_tab,
    grid_cells,
    header_filter_input,
    open_page,
    select_version,
    text_of,
    wait_for,
    wait_graph_at,
)

UPDATE_PATH = "/_dash-update-component"


def _callback_bodies(browser) -> list[str]:
    """Bodies of the callback responses the browser received, read through DevTools."""
    bodies = []
    for entry in browser.get_log("performance"):
        message = json.loads(entry["message"])["message"]
        if message["method"] != "Network.responseReceived":
            continue
        if UPDATE_PATH not in message["params"]["response"]["url"]:
            continue
        try:
            found = browser.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": message["params"]["requestId"]}
            )
        except Exception:  # an evicted body cannot be read; the server side covers it
            continue
        bodies.append(found.get("body", ""))
    return bodies


def test_the_token_never_reaches_the_browser(stack, browser):
    open_page(browser, stack.ui_url)
    head = max(stack.history.versions)
    # Every kind of call the page makes: header, graph, details, search, sheet, history, logs.
    wait_graph_at(browser, head)
    choose_dropdown(browser, ids.GRAPH_OBJECT, node("J2"), typed="J2")
    wait_for(browser, lambda: "around node:J2" in text_of(browser, ids.GRAPH_STATUS), "J2")
    click_graph_element(browser, node("J2"))
    wait_for(browser, lambda: "elevation" in text_of(browser, ids.GRAPH_DETAILS), "details")
    click_tab(browser, "Spreadsheet")
    wait_for(browser, lambda: grid_cells(browser, ids.SHEET_BOX, "uid"), "sheet rows")
    header_filter_input(browser, ids.SHEET_BOX, "elevation").send_keys("45", Keys.ENTER)
    wait_for(browser, lambda: grid_cells(browser, ids.SHEET_BOX, "uid") == [node("J2")], "filtered")
    click_tab(browser, "History")
    wait_for(browser, lambda: grid_cells(browser, ids.LOG_BOX, "seq"), "commit log")
    wait_for(browser, lambda: text_of(browser, ids.DIFF), "changes")
    select_version(browser, 9)
    wait_for(browser, lambda: "#9 against" in text_of(browser, ids.DIFF), "changes at #9")

    token = stack.token
    assert token not in browser.page_source
    assert token not in browser.current_url
    storage = browser.execute_script(
        "return JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage),"
        " document.cookie])"
    )
    assert token not in storage
    assert all(token not in json.dumps(cookie) for cookie in browser.get_cookies())

    received = _callback_bodies(browser)
    assert len(received) > 10
    assert all(token not in body for body in received)

    # Everything the interface sent to any browser this session, byte for byte:
    # the page, its scripts and layout, and every callback response.
    sent = list(stack.ui_responses.sent)
    paths = {path for path, _ in sent}
    assert {"/", "/_dash-layout", "/_dash-dependencies", UPDATE_PATH} <= paths
    leaked = [path for path, body in sent if token.encode() in body]
    assert not leaked

    # Nor is it in the log lines of the stack.
    assert token not in stack.log_file.read_text(encoding="utf-8")
