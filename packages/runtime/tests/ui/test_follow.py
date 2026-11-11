"""Following the head: off, the page stays and counts newer versions; on, it moves by itself.

These tests write versions, so each starts a stack of its own.
"""

from __future__ import annotations

import time

from dta_runtime.ui import ids
from dta_runtime.ui.header import POLL_MS
from selenium.webdriver.common.by import By
from ui_page import (
    follow_checkbox,
    head_seq,
    open_page,
    select_version,
    selected_seq,
    text_of,
    wait_for,
    wait_graph_at,
)
from ui_testkit import write_version

# One poll interval, plus the time to read the new head and redraw.
MOVE_WITHIN_S = POLL_MS / 1000.0 + 3.0


def test_follow_off_stays_and_counts_then_jump_and_follow_move(make_stack, browser):
    stack = make_stack()
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    assert selected_seq(browser) == head
    assert follow_checkbox(browser).is_selected()  # the page starts following

    follow_checkbox(browser).click()
    wait_for(browser, lambda: not follow_checkbox(browser).is_selected(), "follow to be off")
    graph_requests = stack.api_requests.mark()

    first = write_version(stack.history, demand=100.0)
    wait_for(browser, lambda: head_seq(browser) == first, "the new head", timeout=MOVE_WITHIN_S)
    assert selected_seq(browser) == head
    assert text_of(browser, ids.NEWER) == "1 newer version"
    second = write_version(stack.history, demand=101.0)
    wait_for(browser, lambda: head_seq(browser) == second, "the next head", timeout=MOVE_WITHIN_S)
    assert selected_seq(browser) == head
    assert text_of(browser, ids.NEWER) == "2 newer versions"
    # Staying means reading nothing new: no data read at the new heads.
    new_ids = {stack.history.versions[first].id, stack.history.versions[second].id}
    assert not [
        request
        for request in stack.api_requests.since(graph_requests)
        if request.value("at") in new_ids
    ]

    browser.find_element(By.ID, ids.JUMP).click()
    wait_for(browser, lambda: selected_seq(browser) == second, "the jump to the head")
    assert text_of(browser, ids.NEWER) == "at head"
    wait_graph_at(browser, second)
    assert not follow_checkbox(browser).is_selected()

    follow_checkbox(browser).click()
    wait_for(browser, lambda: follow_checkbox(browser).is_selected(), "follow to be on")
    third = write_version(stack.history, demand=102.0)
    written = time.monotonic()
    wait_for(browser, lambda: selected_seq(browser) == third, "the page to move", MOVE_WITHIN_S)
    moved_after = time.monotonic() - written
    assert moved_after <= MOVE_WITHIN_S
    assert text_of(browser, ids.NEWER) == "at head"
    wait_graph_at(browser, third)


def test_choosing_an_older_version_stops_following(make_stack, browser):
    stack = make_stack()
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    assert follow_checkbox(browser).is_selected()
    select_version(browser, head - 3)
    wait_for(browser, lambda: not follow_checkbox(browser).is_selected(), "follow to stop")
    newer = write_version(stack.history, demand=103.0)
    wait_for(browser, lambda: head_seq(browser) == newer, "the new head", timeout=MOVE_WITHIN_S)
    assert selected_seq(browser) == head - 3
    assert text_of(browser, ids.NEWER) == "4 newer versions"
