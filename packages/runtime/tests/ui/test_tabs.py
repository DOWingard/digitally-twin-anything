"""Each tab shows the twin as the API serves it, at the head and at an older version."""

from __future__ import annotations

from api_testkit import J6_SEQ, VALVE_REMOVED_SEQ, link, node
from dta_runtime.ui import ids
from ui_page import (
    choose_dropdown,
    click_tab,
    grid_cells,
    grid_headers,
    head_seq,
    open_page,
    select_version,
    text_of,
    wait_for,
    wait_graph_at,
)
from ui_testkit import FIRST_RUN, SECOND_RUN

# The valve still exists, J6 and Pipe.material do not, and no sync run is recorded yet.
OLD_SEQ = VALVE_REMOVED_SEQ - 1


def test_graph_differs_between_head_and_an_older_version(stack, browser):
    open_page(browser, stack.ui_url)
    head = head_seq(browser)
    assert head == max(stack.history.versions)
    at_head = set(wait_graph_at(browser, head))
    edges_at_head = {
        edge.get("link_uid")
        for edge in browser.execute_script(
            "return document.getElementById(arguments[0])._cyreg.cy.edges().map(e => e.data())",
            ids.GRAPH_VIEW,
        )
    }
    assert node("J6") in at_head
    assert link("V1") not in edges_at_head

    select_version(browser, OLD_SEQ)
    old = set(wait_graph_at(browser, OLD_SEQ))
    edges_old = {
        edge.get("link_uid")
        for edge in browser.execute_script(
            "return document.getElementById(arguments[0])._cyreg.cy.edges().map(e => e.data())",
            ids.GRAPH_VIEW,
        )
    }
    assert node("J6") not in old
    assert link("V1") in edges_old
    assert old - {node("J6")} <= at_head


def test_sheet_differs_between_head_and_an_older_version(stack, browser):
    open_page(browser, stack.ui_url)
    click_tab(browser, "Spreadsheet")
    head = head_seq(browser)
    wait_for(browser, lambda: f"Junction at #{head}" in text_of(browser, ids.SHEET_STATUS), "sheet")
    uids = wait_for(browser, lambda: grid_cells(browser, ids.SHEET_BOX, "uid"), "rows at head")
    assert uids == [node(key) for key in ("J1", "J2", "J3", "J4", "J5", "J6")]
    demand_head = grid_cells(browser, ids.SHEET_BOX, "demand")
    assert demand_head[0] == "42"

    select_version(browser, OLD_SEQ)
    wait_for(
        browser, lambda: f"Junction at #{OLD_SEQ}" in text_of(browser, ids.SHEET_STATUS), "sheet"
    )
    old = wait_for(
        browser,
        lambda: (cells := grid_cells(browser, ids.SHEET_BOX, "uid")) and len(cells) == 5 and cells,
        "rows at the older version",
    )
    assert old == [node(key) for key in ("J1", "J2", "J3", "J4", "J5")]
    demand_old = grid_cells(browser, ids.SHEET_BOX, "demand")
    assert demand_old[0] != demand_head[0]
    assert J6_SEQ > OLD_SEQ

    # The columns follow the ontology at the version: Pipe gained `material` later.
    choose_dropdown(browser, ids.SHEET_TYPE, "Pipe (link)")
    wait_for(browser, lambda: "Pipe at #" in text_of(browser, ids.SHEET_STATUS), "Pipe sheet")
    columns_old = wait_for(browser, lambda: grid_headers(browser, ids.SHEET_BOX), "Pipe columns")
    assert "material" not in columns_old
    select_version(browser, head)
    wait_for(browser, lambda: f"Pipe at #{head}" in text_of(browser, ids.SHEET_STATUS), "Pipe")
    columns_head = wait_for(
        browser,
        lambda: (
            "material" in grid_headers(browser, ids.SHEET_BOX)
            and grid_headers(browser, ids.SHEET_BOX)
        ),
        "material at head",
    )
    assert "material" in columns_head


def test_history_differs_between_head_and_an_older_version(stack, browser):
    open_page(browser, stack.ui_url)
    click_tab(browser, "History")
    head = head_seq(browser)
    log_seqs = wait_for(
        browser, lambda: grid_cells(browser, ids.LOG_BOX, "seq"), "the commit log at head"
    )
    assert log_seqs[0] == str(head)
    assert text_of(browser, ids.FIDELITY) == "unknown"
    diff = wait_for(browser, lambda: text_of(browser, ids.DIFF), "the changes of the head")
    assert f"#{head} against #{head - 1}" in diff
    assert "TwinSyncRun" in diff and "Junction" in diff
    runs = wait_for(browser, lambda: text_of(browser, ids.RUNS), "the sync runs at head")
    assert "2 sync runs" in runs
    assert FIRST_RUN in runs and SECOND_RUN in runs
    assert "the source did not answer" in runs

    select_version(browser, VALVE_REMOVED_SEQ)
    diff_old = wait_for(
        browser,
        lambda: (
            (text := text_of(browser, ids.DIFF))
            and f"#{VALVE_REMOVED_SEQ} against" in text
            and text
        ),
        "the changes of an older version",
    )
    assert "Valve" in diff_old and "removed" in diff_old and link("V1") in diff_old
    runs_old = wait_for(
        browser,
        lambda: "No sync run" in (text := text_of(browser, ids.RUNS)) and text,
        "no sync runs at the older version",
    )
    assert FIRST_RUN not in runs_old
