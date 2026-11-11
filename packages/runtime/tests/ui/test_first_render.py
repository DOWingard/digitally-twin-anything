"""Time to first render of each tab, against a stack started cold, under a limit fixed
before measuring: 5 s per tab."""

from __future__ import annotations

import time

from dta_runtime.ui import ids
from ui_page import click_tab, graph_nodes, grid_cells, text_of, wait_for
from ui_testkit import FIRST_RENDER_LIMIT_S, FIRST_RENDER_S


def test_each_tab_renders_within_the_limit(make_stack, browser):
    stack = make_stack()  # caches of the API and of the interface are empty
    head = max(stack.history.versions)

    started = time.monotonic()
    browser.get(stack.ui_url)
    wait_for(
        browser,
        lambda: f"at #{head}" in text_of(browser, ids.GRAPH_STATUS) and graph_nodes(browser),
        "the graph",
        timeout=FIRST_RENDER_LIMIT_S * 3,
    )
    FIRST_RENDER_S["graph"] = time.monotonic() - started

    started = time.monotonic()
    click_tab(browser, "Spreadsheet")
    wait_for(
        browser,
        lambda: grid_cells(browser, ids.SHEET_BOX, "uid"),
        "the first sheet's rows",
        timeout=FIRST_RENDER_LIMIT_S * 3,
    )
    FIRST_RENDER_S["spreadsheet"] = time.monotonic() - started

    started = time.monotonic()
    click_tab(browser, "History")
    wait_for(
        browser,
        lambda: (
            grid_cells(browser, ids.LOG_BOX, "seq")
            and f"#{head} against" in text_of(browser, ids.DIFF)
            and "sync run" in text_of(browser, ids.RUNS)
        ),
        "the commit log, the changes and the sync runs",
        timeout=FIRST_RENDER_LIMIT_S * 3,
    )
    FIRST_RENDER_S["history"] = time.monotonic() - started

    slow = {
        tab: seconds for tab, seconds in FIRST_RENDER_S.items() if seconds > FIRST_RENDER_LIMIT_S
    }
    assert not slow, f"first render over {FIRST_RENDER_LIMIT_S} s: {slow}"
