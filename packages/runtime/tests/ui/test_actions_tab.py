"""The actions tab against a real API over a real store, with a real plant behind the
executor: the action types, the records in every status with their filters and paging,
everything about a record, and the control loops are what the API says; nothing on the
tab acts on the twin."""

from __future__ import annotations

import json
import time
from typing import Any

from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from selenium.webdriver.common.by import By
from ui_page import (
    acting_controls,
    choose_filter,
    click_tab,
    open_page,
    text_of,
    visible_tabs,
    wait_for,
    wait_grid,
)
from ui_testkit import ACTIONS_TAB_LIMIT_S, ACTIONS_TAB_S, TIMED_RUNS
from ui_trust import BULK_RECORDS, ActingWorld, Served, changes_asked

STATUSES = {"proposed", "rejected", "approved", "blocked", "executed", "failed"}
BLOCK = 100


def records_id(status: str | None = None, action: str | None = None) -> dict[str, str]:
    """The Dash id of the records grid made for these filters."""
    query = json.dumps({"action": action, "status": status}, sort_keys=True)
    return {"role": ids.RECORDS_GRID, "query": query}


_API = "const api = window.dash_ag_grid.getApi(arguments[0]);"


def loaded(driver: Any, grid_id: Any, start: int, end: int) -> list[dict[str, Any] | None]:
    """The rows `start` to `end` of an infinite grid, None for a row not loaded yet."""
    return driver.execute_script(
        _API + "const rows = [];"
        "for (let i = arguments[1]; i < arguments[2]; i++) {"
        "  const node = api.getDisplayedRowAtIndex(i); rows.push(node && node.data || null); }"
        "return rows;",
        grid_id,
        start,
        end,
    )


def wait_loaded(
    driver: Any, grid_id: Any, start: int, end: int, what: str, timeout: float = 15.0
) -> list[dict[str, Any]]:
    def check() -> list[list[dict[str, Any]]] | None:
        rows = loaded(driver, grid_id, start, end)
        return [rows] if rows and all(rows) else None

    return wait_for(driver, check, what, timeout)[0]


def displayed_count(driver: Any, grid_id: Any) -> int:
    return int(driver.execute_script(_API + "return api.getDisplayedRowCount();", grid_id))


def scroll_to_index(driver: Any, grid_id: Any, index: int) -> None:
    """Scroll the grid so the row at `index` is in view, as dragging its scroll bar does."""
    driver.execute_script(_API + "api.ensureIndexVisible(arguments[1], 'top');", grid_id, index)


def scroll_to_end(driver: Any, grid_id: Any, timeout: float = 60.0) -> int:
    """Scroll down block by block until the grid knows its last row; its row count then."""

    def reached() -> int | None:
        known, count, last = driver.execute_script(
            _API + "const count = api.getDisplayedRowCount();"
            "api.ensureIndexVisible(count - 1, 'bottom');"
            "const node = api.getDisplayedRowAtIndex(count - 1);"
            "return [api.isLastRowIndexKnown(), count, !!(node && node.data)];",
            grid_id,
        )
        return count if known and last else None

    return wait_for(driver, reached, "the last record", timeout)


def show_row(driver: Any, grid_id: Any, uid: str) -> None:
    driver.execute_script(
        _API + "api.ensureNodeVisible(api.getRowNode(arguments[1]), 'middle');", grid_id, uid
    )


def click_record(driver: Any, uid: str, grid_id: Any | None = None) -> None:
    """Click the action cell of a record in the grid, scrolled into view first."""
    selector = f"#{ids.RECORDS} .ag-row[row-id='{uid}'] .ag-cell[col-id='action']"

    def click() -> bool:
        show_row(driver, grid_id or records_id(), uid)
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if not found or not found[0].is_displayed():
            return False
        found[0].click()
        return True

    wait_for(driver, click, f"record {uid} to be clicked")


def record_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": record["uid"],
        "proposed_at": T.time_text(record["proposed_at"]),
        "action": record["action"],
        "object": record["object"],
        "params": T.params_text(record["params"]),
        "source": record["source"],
        "fidelity": record["fidelity"] or "",
        "status": T.record_status_text(record["status"]),
        "status_key": record["status"],
        "reconciliation": record["reconciliation"] or "",
        "effective_at": T.time_text(record["effective_at"]),
    }


def part_lines(driver: Any, name: str) -> list[str]:
    """The lines of one part of the record shown, or its one sentence."""
    found = driver.find_elements(By.CSS_SELECTOR, f"#{ids.RECORD_DETAIL} [data-part={name}]")
    if not found:
        return []
    items = found[0].find_elements(By.TAG_NAME, "li")
    if items:
        return [item.text for item in items]
    heading = found[0].find_element(By.TAG_NAME, "h5").text
    return [found[0].text.removeprefix(heading).strip()]


def open_actions(browser: Any, served: Served) -> None:
    open_page(browser, served.ui_url)
    wait_for(browser, lambda: "Actions" in visible_tabs(browser), "the actions tab")
    click_tab(browser, "Actions")


def show_record(browser: Any, served: Served, uid: str) -> dict[str, Any]:
    click_record(browser, uid)
    wait_for(
        browser,
        lambda: browser.find_elements(By.CSS_SELECTOR, f"#{ids.RECORD_DETAIL} [data-uid='{uid}']"),
        f"record {uid}",
    )
    return served.get(f"/actions/records/{uid}")["data"]


def test_the_types_the_first_records_and_the_loops_are_what_the_api_says(
    acting: ActingWorld, browser
):
    served = acting.served
    mark = served.api_requests.mark()
    open_actions(browser, served)
    listing = served.get("/actions")
    shown = wait_grid(browser, ids.ACTION_TYPES_GRID, lambda rows: len(rows) == 5, "the types")
    assert shown == [
        {
            "id": action["id"],
            "control": action["control"] or "",
            "target": action["target"] or "",
            "autonomy": action["autonomy"],
            "binding": action["binding"] or "",
            "bounds": T.bounds_text(action["bounds"]),
            "settle": T.duration_text(action["settle"]),
            "status": action["status"],
        }
        for action in listing["data"]
    ]
    by_id = {row["id"]: row for row in shown}
    assert by_id["set_valve"]["bounds"] == "1 to 200, steps of at most 50"
    assert by_id["set_pump"]["bounds"] == "one of true, false"
    assert listing["executor_configured"] is True
    assert text_of(browser, ids.EXECUTOR_STATUS) == T.EXECUTOR_ON

    # The first block of records, newest first, as the API's first page of a hundred.
    first = served.get("/actions/records", limit=BLOCK)["data"]
    rows = wait_loaded(browser, records_id(), 0, BLOCK, "the first block of records")
    assert rows == [record_row(record) for record in first]
    story = rows[: len(acting.records) + 2]
    assert {row["status_key"] for row in story} == STATUSES
    assert [row["reconciliation"] for row in story].count("mismatch") == 1
    # A refused, failed or mismatched record is told by its colour as well as its word.
    for key, marked in (
        ("rejected", "record-rejected"),
        ("blocked", "record-blocked"),
        ("failed", "record-failed"),
        ("mismatch", "record-mismatch"),
    ):
        uid = acting.records[key]
        show_row(browser, records_id(), uid)
        row = wait_for(
            browser,
            lambda uid=uid: browser.find_elements(
                By.CSS_SELECTOR, f"#{ids.RECORDS} .ag-center-cols-container [row-id='{uid}']"
            ),
            f"the {key} record's row",
        )[0]
        assert marked in row.get_attribute("class")

    loops = served.get("/control/loops")["data"]
    shown = wait_grid(browser, ids.LOOPS_GRID, lambda rows: len(rows) == 1, "the loop")
    assert shown == [
        {
            "id": "pumping",
            "policy": "pump_policy",
            "interval": T.duration_text("PT15M"),
            "enabled": "yes",
            "last_run_at": T.time_text(loops[0]["last_run_at"]),
        }
    ]
    run = loops[0]["last_run"]
    assert run["key"] == acting.loop_run["key"]
    cells = [
        cell.text
        for cell in browser.find_elements(
            By.CSS_SELECTOR, f"#{ids.LOOP_RUNS} tr[data-loop=pumping] td"
        )
    ]
    assert cells == [
        "pumping",
        T.time_text(run["at"]),
        "succeeded",
        "2",
        "0",
        "1",
        "0",
        "0",
        "1",
        "the valve stands at 11",
        "",
    ]
    assert acting_controls(browser, "actions-tab") == []
    assert changes_asked(served.api_requests.from_page(mark)) == []


def test_a_record_shows_its_proposal_refusal_decision_call_and_observation(
    acting: ActingWorld, browser
):
    served = acting.served
    open_actions(browser, served)
    wait_loaded(browser, records_id(), 0, 20, "the newest records")
    assert part_lines(browser, "proposal") == []
    assert text_of(browser, ids.RECORD_DETAIL) == T.CHOOSE_RECORD

    # Executed, then seen at 11 against the 25 it was set to.
    found = show_record(browser, served, acting.records["mismatch"])
    assert part_lines(browser, "proposal") == T.proposal_lines(found)
    assert "status ✓ executed, reconciliation mismatch" in part_lines(browser, "proposal")
    assert part_lines(browser, "reasons") == [T.NO_REASONS]
    assert part_lines(browser, "approval") == [
        "approved by operator@plant at 2026-01-05 02:15:00 UTC: looks right"
    ]
    assert part_lines(browser, "execution") == T.execution_lines(found["execution"])
    observed = part_lines(browser, "observed")
    assert observed == T.observed_lines(found["observed"], found["reconciled_at"])
    assert observed[0] == "observed 11 against expected 25 (difference 14, tolerance 0.5)"

    # Sent, and refused by the plant.
    found = show_record(browser, served, acting.records["failed"])
    execution = part_lines(browser, "execution")
    assert execution == T.execution_lines(found["execution"])
    assert {"HTTP status 422", "tries 1", "error: the system answered 422"} <= set(execution)
    assert part_lines(browser, "observed") == [T.NO_OBSERVED]

    # Refused by the bounds of its control, before any decision or call.
    found = show_record(browser, served, acting.records["rejected"])
    assert part_lines(browser, "reasons") == [
        "params.value: 500 is above the control's maximum 200",
        "params.value: a change of 495 from 5 is larger than the control's max_step 50",
    ]
    assert part_lines(browser, "approval") == [T.NO_APPROVAL]
    assert part_lines(browser, "execution") == [T.NO_EXECUTION]

    # Turned down by its approver.
    show_record(browser, served, acting.records["turned_down"])
    assert part_lines(browser, "approval") == [
        "rejected by operator@plant at 2026-01-05 02:30:00 UTC: not now"
    ]
    assert part_lines(browser, "reasons") == ["approval: turned down by operator@plant"]

    # Blocked by the interlock while the twin was failed.
    found = show_record(browser, served, acting.records["blocked"])
    assert found["fidelity"] == "failed"
    assert part_lines(browser, "reasons") == [
        "fidelity: the twin is failed: it could not put itself right; an autonomous action "
        "executes only while it is healthy or correcting"
    ]

    # Waiting for a decision: nothing has happened to it yet.
    show_record(browser, served, acting.records["proposed"])
    assert [
        part_lines(browser, name) for name in ("reasons", "approval", "execution", "observed")
    ] == [[T.NO_REASONS], [T.NO_APPROVAL], [T.NO_EXECUTION], [T.NO_OBSERVED]]


def test_the_records_filter_by_status_and_action(acting: ActingWorld, browser):
    served = acting.served
    open_actions(browser, served)
    wait_loaded(browser, records_id(), 0, BLOCK, "every record")
    chosen: str | None = None
    for status, action, count in (
        ("executed", None, 2),
        ("rejected", None, 2),
        ("rejected", "set_valve", 2),
        ("blocked", "set_pump", 2),
        ("executed", "set_pump", 1),
    ):
        choose_filter(browser, ids.RECORDS_STATUS, status)
        if action != chosen:
            assert action, "an action filter is never cleared here"
            choose_filter(browser, ids.RECORDS_ACTION, action)
            chosen = action
        expected = served.every(
            "/actions/records", status=status, **({"action": action} if action else {})
        )
        assert len(expected) == count
        grid_id = records_id(status, action)
        rows = wait_loaded(browser, grid_id, 0, count, f"the {status} {action or ''} records")
        assert displayed_count(browser, grid_id) == count
        assert rows == [record_row(record) for record in expected]
        assert {row["status_key"] for row in rows} == {status}
        if action:
            assert {row["action"] for row in rows} == {action}


def test_the_records_page_past_the_first_block_to_the_last(acting: ActingWorld, browser):
    served = acting.served
    open_actions(browser, served)
    every = served.every("/actions/records")
    assert len(every) == BULK_RECORDS + len(acting.records) + 2
    grid_id = records_id()
    wait_loaded(browser, grid_id, 0, BLOCK, "the first block")
    scroll_to_index(browser, grid_id, BLOCK + BLOCK // 2)
    second = wait_loaded(browser, grid_id, BLOCK, 2 * BLOCK, "the second block")
    assert second == [record_row(record) for record in every[BLOCK : 2 * BLOCK]]
    # The second block is the second page the API gives.
    page = served.get("/actions/records", limit=BLOCK)
    following = served.get("/actions/records", limit=BLOCK, cursor=page["next_cursor"])
    assert [row["uid"] for row in second] == [record["uid"] for record in following["data"]]

    assert scroll_to_end(browser, grid_id) == len(every)
    rows = wait_loaded(browser, grid_id, 0, len(every), "every record", timeout=30.0)
    assert [row["uid"] for row in rows] == [record["uid"] for record in every]
    assert rows[-1]["uid"] == f"twin_action_record:bulk{BULK_RECORDS - 1:04d}"


def test_the_actions_tab_is_drawn_in_time(acting: ActingWorld, browser):
    served = acting.served
    assert len(served.every("/actions/records")) >= BULK_RECORDS

    def drawn() -> bool:
        types = wait_grid(browser, ids.ACTION_TYPES_GRID, lambda rows: True, "types")
        loops = wait_grid(browser, ids.LOOPS_GRID, lambda rows: True, "loops")
        rows = loaded(browser, records_id(), 0, BLOCK)
        return (
            len(types) == 5
            and len(loops) == 1
            and bool(text_of(browser, ids.LOOP_RUNS))
            and bool(rows)
            and all(rows)
        )

    for _ in range(TIMED_RUNS):
        open_page(browser, served.ui_url)
        wait_for(browser, lambda: "Actions" in visible_tabs(browser), "the actions tab")
        started = time.perf_counter()
        click_tab(browser, "Actions")
        wait_for(browser, drawn, "the whole actions tab", timeout=30.0)
        ACTIONS_TAB_S.append(time.perf_counter() - started)
    assert max(ACTIONS_TAB_S[-TIMED_RUNS:]) <= ACTIONS_TAB_LIMIT_S, ACTIONS_TAB_S
