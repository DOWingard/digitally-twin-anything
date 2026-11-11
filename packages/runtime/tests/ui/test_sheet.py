"""The spreadsheet pages, sorts and filters through the API: checked on the requests the API
received, not only on what the grid shows; a filter the API refuses is shown with its code."""

from __future__ import annotations

from dta_runtime.ui import ids
from dta_runtime.ui.sheet import BLOCK_ROWS
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_dropdown,
    click_tab,
    grid_cells,
    header_filter_input,
    open_page,
    sort_by,
    text_of,
    wait_for,
    wait_graph_at,
)
from ui_testkit import READINGS

READING_PATH = "/api/v1/objects/Reading"
# The water history's own readings plus those of the extension.
READING_ROWS = 6 + READINGS


def _open_readings(stack, browser):
    open_page(browser, stack.ui_url)
    click_tab(browser, "Spreadsheet")
    wait_for(browser, lambda: "Junction at #" in text_of(browser, ids.SHEET_STATUS), "a sheet")
    mark = stack.api_requests.mark()
    choose_dropdown(browser, ids.SHEET_TYPE, "Reading")
    wait_for(browser, lambda: "Reading at #" in text_of(browser, ids.SHEET_STATUS), "Readings")
    wait_for(browser, lambda: grid_cells(browser, ids.SHEET_BOX, "uid"), "the first block")
    return mark


def _last_index(browser) -> int:
    indexes = [
        int(row.get_attribute("row-index"))
        for row in browser.find_elements(
            By.CSS_SELECTOR, f"#{ids.SHEET_BOX} .ag-pinned-left-cols-container .ag-row"
        )
        if (row.get_attribute("row-index") or "").isdigit()
        and row.find_elements(By.CSS_SELECTOR, ".ag-cell[col-id='uid']")
        and row.find_element(By.CSS_SELECTOR, ".ag-cell[col-id='uid']").text
    ]
    return max(indexes, default=-1)


def _set_filter(browser, column, text):
    field = header_filter_input(browser, ids.SHEET_BOX, column)
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.DELETE)
    if text:
        field.send_keys(text)


def test_sheet_pages_through_the_api_a_block_at_a_time(stack, browser):
    mark = _open_readings(stack, browser)
    head = stack.history.versions[max(stack.history.versions)]
    viewport = browser.find_element(By.CSS_SELECTOR, f"#{ids.SHEET_BOX} .ag-body-viewport")
    seen = -1
    for _ in range(READING_ROWS // BLOCK_ROWS + 3):
        browser.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", viewport)
        seen = wait_for(
            browser,
            lambda before=seen: (last := _last_index(browser)) > before and last,
            "the next block",
        )
        if seen >= READING_ROWS - 1:
            break
    assert seen == READING_ROWS - 1
    requests = stack.api_requests.since(mark, READING_PATH)
    blocks = -(-READING_ROWS // BLOCK_ROWS)
    assert len(requests) == blocks, [request.query for request in requests]
    # Every request asks for one block of the selected version; none reads the table whole.
    assert {request.value("limit") for request in requests} == {str(BLOCK_ROWS)}
    assert {request.value("at") for request in requests} == {head.id}
    cursors = [request.value("cursor") for request in requests]
    assert cursors[0] is None
    assert all(cursors[1:]) and len(set(cursors[1:])) == blocks - 1


def test_sheet_sorts_and_filters_through_the_api(stack, browser):
    mark = _open_readings(stack, browser)

    sort_by(browser, ids.SHEET_BOX, "value")
    wait_for(
        browser,
        lambda: any(
            r.value("sort") == "value" for r in stack.api_requests.since(mark, READING_PATH)
        ),
        "an ascending sort request",
    )
    sort_by(browser, ids.SHEET_BOX, "value")
    wait_for(
        browser,
        lambda: any(
            r.value("sort") == "-value" for r in stack.api_requests.since(mark, READING_PATH)
        ),
        "a descending sort request",
    )
    top = wait_for(
        browser,
        lambda: (
            (cells := grid_cells(browser, ids.SHEET_BOX, "value")) and cells[0] == "96" and cells
        ),
        "the largest value first",
    )
    assert top[0] == "96"

    filtered = stack.api_requests.mark()
    _set_filter(browser, "sequence", "400")
    rows = wait_for(
        browser,
        lambda: (
            (cells := grid_cells(browser, ids.SHEET_BOX, "uid")) == ["reading:S2-0400"] and cells
        ),
        "the one reading with sequence 400",
    )
    assert rows == ["reading:S2-0400"]
    asked = stack.api_requests.since(filtered, READING_PATH)
    assert any(
        request.values("where") == ["sequence:eq:400"] and request.value("sort") == "-value"
        for request in asked
    ), [request.query for request in asked]

    _set_filter(browser, "sequence", "")
    texts = stack.api_requests.mark()
    _set_filter(browser, "uid", "S2-001")
    rows = wait_for(
        browser,
        lambda: len(cells := grid_cells(browser, ids.SHEET_BOX, "uid")) == 10 and cells,
        "the ten readings whose uid contains S2-001",
    )
    assert all("S2-001" in uid for uid in rows)
    assert any(
        request.values("where") == ["uid:like:%S2-001%"]
        for request in stack.api_requests.since(texts, READING_PATH)
    )


def test_a_filter_the_api_refuses_is_shown_with_its_code(stack, browser):
    _open_readings(stack, browser)
    refused = stack.api_requests.mark()
    # `sequence` holds integers: the API refuses 2.5 rather than the interface guessing.
    _set_filter(browser, "sequence", "2.5")
    problem = wait_for(
        browser, lambda: text_of(browser, ids.SHEET_FEEDBACK), "the problem to be shown"
    )
    assert "store.bad_filter" in problem
    assert "Bad filter" in problem
    assert "trace id" in problem
    assert "Traceback" not in browser.page_source
    assert any(
        request.values("where") == ["sequence:eq:2.5"]
        for request in stack.api_requests.since(refused, READING_PATH)
    )

    # The page keeps working: the sheet reads again once the filter is cleared,
    # and the other tabs still answer.
    _set_filter(browser, "sequence", "")
    wait_for(browser, lambda: not text_of(browser, ids.SHEET_FEEDBACK), "the problem to clear")
    assert wait_for(browser, lambda: grid_cells(browser, ids.SHEET_BOX, "uid"), "rows again")
    click_tab(browser, "Graph")
    head = max(stack.history.versions)
    assert wait_graph_at(browser, head)
