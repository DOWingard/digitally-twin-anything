"""A version is read once the slider stays still, and a search once the typing pauses:
stepping through ten versions reads one, typing a word searches for the word."""

from __future__ import annotations

import time

from dta_runtime.ui import ids
from dta_runtime.ui.header import SETTLE_MS
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_dropdown,
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
    wait_graph_at(browser, head_seq(browser))
    mark = stack.api_requests.mark()

    choose_dropdown(browser, ids.GRAPH_OBJECT, "node:J2", typed="node:J2")
    wait_for(browser, lambda: "around node:J2" in text_of(browser, ids.GRAPH_STATUS), "J2")
    time.sleep(QUIET_S)

    searches = [
        r
        for r in stack.api_requests.since(mark)
        if r.path.startswith("/api/v1/objects/")
        and any(clause.startswith("uid:like:") for clause in r.values("where"))
    ]
    # One search: each node type asked once, for the whole word.
    assert searches
    assert {clause for r in searches for clause in r.values("where")} == {"uid:like:%node:J2%"}
    assert len({r.path for r in searches}) == len(searches)
