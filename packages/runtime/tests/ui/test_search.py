"""The graph's object search asks `/search` once, across the graph's types, and offers what
it found by uid, type and name; when more match than it offers, it says so."""

from __future__ import annotations

import time

import httpx
from dta_runtime.ui import ids
from dta_runtime.ui.graph import SEARCH_LIMIT
from dta_runtime.ui.header import SETTLE_MS
from dta_runtime.ui.text import search_label
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import head_seq, open_page, wait_for, wait_graph_at

SEARCH = "/api/v1/search"
QUIET_S = 4 * SETTLE_MS / 1000.0


def offered(browser, typed: str) -> list[str]:
    """Type into the object field and read the options the dropdown shows once they stop
    changing."""
    box = browser.find_element(By.ID, ids.GRAPH_OBJECT)
    box.click()
    popup_id = wait_for(browser, lambda: box.get_attribute("aria-controls"), "the dropdown")
    popup = browser.find_element(By.ID, popup_id)
    field = [found for found in popup.find_elements(By.TAG_NAME, "input") if found.is_displayed()]
    field[0].send_keys(typed)

    def texts() -> list[str]:
        return [
            item.text.strip()
            for item in browser.find_element(By.ID, popup_id).find_elements(
                By.CSS_SELECTOR, "[role=option]"
            )
            if item.is_displayed()
        ]

    wait_for(browser, texts, f"options for {typed!r}")
    time.sleep(QUIET_S)
    found = texts()
    field[0].send_keys(Keys.ESCAPE)
    return found


def api_search(stack, request) -> dict:
    """The API's answer to the very query the page sent."""
    response = httpx.get(
        stack.api_url + SEARCH,
        params=list(request.query),
        headers={"Authorization": f"Bearer {stack.token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_one_request_offers_what_the_api_found(stack, browser):
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    wait_graph_at(browser, head)
    mark = stack.api_requests.mark()

    shown = offered(browser, "J")

    searches = stack.api_requests.since(mark, SEARCH)
    assert len(searches) == 1
    request = searches[0]
    assert request.value("q") == "%J%" and request.value("at") == stack.history.versions[head].id
    # No request per type, as there used to be.
    assert not [r for r in stack.api_requests.since(mark) if r.path.startswith("/api/v1/objects/")]
    found = api_search(stack, request)["data"]
    assert found and len(found) < SEARCH_LIMIT
    assert shown == [search_label(row["uid"], row["type"], row["name"]) for row in found]
    # The objects the search per type found, by uid, are among them.
    junctions = {"node:J1", "node:J2", "node:J3", "node:J4"}
    assert junctions <= {row["uid"] for row in found}
    assert "node:J2 · Junction · " in " | ".join(shown)


def test_more_matches_than_offered_are_counted(stack, browser):
    open_page(browser, stack.ui_url)
    wait_graph_at(browser, head_seq(browser))
    mark = stack.api_requests.mark()

    shown = offered(browser, "S2")

    (request,) = stack.api_requests.since(mark, SEARCH)
    answer = api_search(stack, request)
    assert answer["total"] > SEARCH_LIMIT
    more = answer["total"] - SEARCH_LIMIT
    assert len(shown) == SEARCH_LIMIT + 1
    assert shown[-1].startswith(f"… {more} more objects match S2")
