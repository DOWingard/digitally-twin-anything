"""A version is read once the slider stays still, and a search once the typing pauses:
stepping through ten versions reads one, typing a word searches for the word."""

from __future__ import annotations

import time

from dta_runtime.ui import ids
from dta_runtime.ui.header import SETTLE_MS
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_object,
    head_seq,
    open_page,
    selected_seq,
    text_of,
    wait_for,
    wait_graph_at,
)

# Reads of the twin's data at a version.
VERSIONED = ("/api/v1/objects/", "/api/v1/object/", "/api/v1/graph/", "/api/v1/meta/schema")
LOOKUP = "/api/v1/history/versions/"
SEARCH = "/api/v1/search"
# Long enough for a read that a step had started to reach the API.
QUIET_S = 4 * SETTLE_MS / 1000.0


def test_stepping_the_slider_through_ten_versions_reads_only_the_last(stack, browser):
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    wait_graph_at(browser, head)
    target = head - 10
    mark = stack.api_requests.mark()

    thumb = browser.find_element(By.CSS_SELECTOR, f"#{ids.SLIDER} [role=slider]")
    thumb.send_keys(*([Keys.ARROW_LEFT] * 10))
    wait_for(browser, lambda: selected_seq(browser) == target, f"version #{target}")
    wait_graph_at(browser, target)
    time.sleep(QUIET_S)

    seen = stack.api_requests.since(mark)
    lookups = [r for r in seen if r.path.startswith(LOOKUP)]
    assert [r.path.removeprefix(LOOKUP) for r in lookups] == [f"#{target}"]
    reads = [r for r in seen if r.path.startswith(VERSIONED)]
    assert reads and {r.value("at") for r in reads} == {stack.history.versions[target].id}
    assert len([r for r in reads if r.path == "/api/v1/graph/subgraph"]) == 1


def test_typing_a_word_searches_for_the_word_once(stack, browser):
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    wait_graph_at(browser, head)
    mark = stack.api_requests.mark()

    choose_object(browser, "node:J2", typed="node:J2")
    wait_for(browser, lambda: "around node:J2" in text_of(browser, ids.GRAPH_STATUS), "J2")
    time.sleep(QUIET_S)

    seen = stack.api_requests.since(mark)
    # One request, for the whole word, across the types; none per type any more.
    searches = [r for r in seen if r.path == SEARCH]
    assert [r.value("q") for r in searches] == ["%node:J2%"]
    assert searches[0].value("at") == stack.history.versions[head].id
    assert not [r for r in seen if r.path.startswith("/api/v1/objects/") and r.values("where")]
