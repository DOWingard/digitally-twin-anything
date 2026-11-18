"""The fidelity and actions tabs when the API is gone, and the secrets they never show: the
API's token and the plant's, which the executor sends with every call."""

from __future__ import annotations

import json
from typing import Any

from dta_runtime.ui import ids
from selenium.webdriver.common.by import By
from test_actions_tab import open_actions, records_id, show_record, wait_loaded
from test_token import UPDATE_PATH
from ui_page import click_tab, open_page, problem_text, visible_tabs, wait_for, wait_grid
from ui_trust import PLANT_TOKEN, ActingWorld

FIDELITY_FEEDBACK = (
    ids.FIDELITY_FEEDBACK,
    ids.FIDELITY_CHARTS_FEEDBACK,
    ids.INCIDENTS_FEEDBACK,
    ids.SIGNALS_FEEDBACK,
    ids.CHECKS_FEEDBACK,
    ids.NOTIFICATIONS_FEEDBACK,
)
ACTIONS_FEEDBACK = (ids.ACTION_TYPES_FEEDBACK, ids.RECORDS_FEEDBACK, ids.LOOPS_FEEDBACK)
UNREACHABLE = "The API cannot be reached"


def test_with_the_api_gone_each_part_of_both_tabs_says_so_in_place(make_acting, browser):
    world: ActingWorld = make_acting()
    served = world.served
    open_page(browser, served.ui_url)
    wait_for(
        browser,
        lambda: {"Fidelity", "Actions"} <= set(visible_tabs(browser)),
        "the fidelity and actions tabs",
    )
    served.stop_api()

    click_tab(browser, "Fidelity")
    for feedback in FIDELITY_FEEDBACK:
        shown = wait_for(
            browser,
            lambda feedback=feedback: problem_text(browser, feedback),
            f"the problem in {feedback}",
        )
        assert UNREACHABLE in shown, (feedback, shown)
    # Nothing was drawn in their place but the problem.
    assert not browser.find_elements(By.CSS_SELECTOR, f"#{ids.FIDELITY_CHARTS} .js-plotly-plot")

    click_tab(browser, "Actions")
    for feedback in ACTIONS_FEEDBACK:
        shown = wait_for(
            browser,
            lambda feedback=feedback: problem_text(browser, feedback),
            f"the problem in {feedback}",
        )
        assert UNREACHABLE in shown, (feedback, shown)
    assert wait_grid(browser, ids.ACTION_TYPES_GRID, lambda rows: True, "the types") == []
    # The tabs stay, as the page knew them before the API went.
    assert {"Fidelity", "Actions"} <= set(visible_tabs(browser))


def seen_by_browser(browser: Any) -> tuple[list[str], list[str]]:
    """Every request the browser sent, URL and body, and the bodies of the callback answers
    it received, read once from its network log."""
    sent: list[str] = []
    received: list[str] = []
    for entry in browser.get_log("performance"):
        message = json.loads(entry["message"])["message"]
        params = message["params"]
        if message["method"] == "Network.requestWillBeSent":
            request = params["request"]
            sent.append(f"{request.get('url')}\n{request.get('postData') or ''}")
        elif message["method"] == "Network.responseReceived":
            if UPDATE_PATH not in params["response"]["url"]:
                continue
            try:
                found = browser.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": params["requestId"]}
                )
            except Exception:  # an evicted body cannot be read; the server side covers it
                continue
            received.append(found.get("body", ""))
    return sent, received


def test_neither_the_api_token_nor_the_plant_token_reaches_the_browser(
    acting: ActingWorld, browser
):
    served = acting.served
    open_actions(browser, served)
    wait_loaded(browser, records_id(), 0, 20, "the newest records")
    # The record whose call to the plant carried the plant's token.
    found = show_record(browser, served, acting.records["mismatch"])
    assert found["execution"]["http_status"] == 200
    wait_grid(browser, ids.LOOPS_GRID, lambda rows: len(rows) == 1, "the loop")
    click_tab(browser, "Fidelity")
    wait_for(
        browser,
        lambda: browser.find_elements(By.CSS_SELECTOR, f"#{ids.FIDELITY_SUMMARY} [data-field]"),
        "the fidelity summary",
    )
    wait_grid(browser, ids.CHECKS_GRID, lambda rows: True, "the checks")

    sent, received = seen_by_browser(browser)
    assert len(received) > 10
    # The record's answer was read, and the call it tells of holds no secret.
    assert any(found["execution"]["idempotency_key"] in body for body in received)
    storage = browser.execute_script(
        "return JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage),"
        " document.cookie])"
    )
    served_bodies = [body for _, body in served.ui_responses.sent]
    log_text = served.log_file.read_text(encoding="utf-8")
    for secret in (served.token, PLANT_TOKEN):
        assert secret not in browser.page_source
        assert secret not in browser.current_url
        assert secret not in storage
        assert all(secret not in json.dumps(cookie) for cookie in browser.get_cookies())
        assert all(secret not in request for request in sent)
        assert all(secret not in body for body in received)
        assert not [body for body in served_bodies if secret.encode() in body]
        assert secret not in log_text
