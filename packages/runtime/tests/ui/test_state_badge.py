"""The state badge and the failed line in the header, and which twins have the fidelity and
actions tabs: against a real API over a real store, in a real browser."""

from __future__ import annotations

from collections import Counter

from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from dta_runtime.ui.header import POLL_MS
from ui_page import (
    badge_class,
    badge_text,
    click_tab,
    follow_checkbox,
    open_page,
    problem_text,
    text_of,
    visible_tabs,
    wait_for,
)

HEALTH = "/api/v1/meta/health"
INCIDENTS = "/api/v1/fidelity/incidents"
STATIC_TABS = ["Graph", "Spreadsheet", "History", "Questions", "Scenarios"]
# A new state is shown within one poll and a few seconds.
SHOWN_S = POLL_MS / 1000.0 + 5.0


def test_the_badge_moves_with_the_twin_without_a_request_of_its_own(make_badge, browser):
    world = make_badge()
    served = world.served
    open_page(browser, served.ui_url)
    text = wait_for(browser, lambda: badge_text(browser), "the badge")
    assert text.startswith("fidelity ? unknown") and "0 open incidents" in text
    assert "state-unknown" in badge_class(browser)
    assert text_of(browser, ids.FAILED_LINE) == ""
    # Not following the head: a poll then asks for the health and nothing else.
    follow_checkbox(browser).click()
    wait_for(browser, lambda: not follow_checkbox(browser).is_selected(), "follow to stop")
    mark = served.api_requests.mark()

    for state, move in (
        ("healthy", world.healthy),
        ("correcting", world.correcting),
        ("failed", world.failed),
    ):
        assert move() == state
        detail = served.get("/meta/health")["fidelity_detail"]
        words = T.state_text(state)
        text = wait_for(
            browser,
            lambda words=words: words in badge_text(browser) and badge_text(browser),
            f"the badge to say {state}",
            SHOWN_S,
        )
        # The state is told in a word and a sign, since when and the incidents; the colour
        # only repeats it.
        assert text == "fidelity " + words + T.badge_details(
            detail["since"], detail["incidents"]["open"], detail["incidents"]["failed"]
        )
        assert f"since {T.minute_text(detail['since'])}" in text
        assert f"state-{state}" in badge_class(browser)
        if state != "failed":
            assert text_of(browser, ids.FAILED_LINE) == ""
    assert "1 open incident, 1 failed" in badge_text(browser)

    (failed,) = served.every("/fidelity/incidents", status="failed")
    line = wait_for(browser, lambda: text_of(browser, ids.FAILED_LINE), "the failed line", SHOWN_S)
    assert line == T.FAILED_WARNING + f" Failed: the stale incident of {failed['subject']}."
    assert failed["subject"] == "level"

    # Two more polls: the failure is unchanged, so its incidents are not read again.
    polled = len(served.api_requests.from_page(mark))
    wait_for(
        browser,
        lambda: len(served.api_requests.from_page(mark)) >= polled + 2,
        "two more polls",
        2 * SHOWN_S,
    )
    asked = Counter(request.path for request in served.api_requests.from_page(mark))
    assert set(asked) == {HEALTH, INCIDENTS}, asked
    assert asked[INCIDENTS] == 1
    assert asked[HEALTH] >= 5


def test_a_static_twin_has_no_badge_and_neither_tab(stack, browser):
    mark = stack.api_requests.mark()
    open_page(browser, stack.ui_url)
    wait_for(browser, lambda: visible_tabs(browser) == STATIC_TABS, "the tabs of a static twin")
    # The health was read, and with it the page knows the twin is static: it reads no
    # manifest to decide its tabs.
    wait_for(
        browser,
        lambda: any(request.path == HEALTH for request in stack.api_requests.from_page(mark)),
        "the health",
    )
    assert badge_text(browser) == ""
    assert browser.find_element("id", ids.STATE_BADGE).get_attribute("hidden") is not None
    assert text_of(browser, ids.FAILED_LINE) == ""
    assert visible_tabs(browser) == STATIC_TABS
    asked = {request.path for request in stack.api_requests.from_page(mark)}
    assert "/api/v1/meta/manifest" not in asked
    assert not any(path.startswith(("/api/v1/fidelity", "/api/v1/actions")) for path in asked)


def test_a_live_twin_that_sets_no_fidelity_says_so_and_has_no_actions_tab(make_live, browser):
    live = make_live()
    open_page(browser, live.ui_url)
    wait_for(
        browser,
        lambda: visible_tabs(browser) == [*STATIC_TABS, "Fidelity"],
        "the fidelity tab and no actions tab",
    )
    assert badge_text(browser).startswith("fidelity ? unknown")
    click_tab(browser, "Fidelity")
    wait_for(browser, lambda: text_of(browser, ids.FIDELITY_SUMMARY) == T.NO_FIDELITY, "the note")
    for feedback in (ids.FIDELITY_FEEDBACK, ids.INCIDENTS_FEEDBACK, ids.CHECKS_FEEDBACK):
        assert problem_text(browser, feedback) == ""
    assert visible_tabs(browser) == [*STATIC_TABS, "Fidelity"]
