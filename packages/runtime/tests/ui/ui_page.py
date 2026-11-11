"""The interface's page as the browser tests drive it: what to click, type and read."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from dta_runtime.ui import ids
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

WAIT_S = 15.0


def wait_for[T](driver: WebDriver, check: Callable[[], T], what: str, timeout: float = WAIT_S) -> T:
    """The first truthy result of `check`, retried while the page updates."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = check()
        except (StaleElementReferenceException, JavascriptException, WebDriverException):
            last = None
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"timed out after {timeout} s waiting for {what}; last saw {last!r}\n{page_report(driver)}"
    )


def page_report(driver: WebDriver) -> str:
    """What the browser logged and which callback requests it made, for a failure message."""
    lines: list[str] = []
    try:
        for entry in driver.get_log("browser"):
            lines.append(f"console {entry.get('level')}: {str(entry.get('message'))[:300]}")
        sent: dict[str, str] = {}
        answered: dict[str, int] = {}
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            params = message.get("params", {})
            if message["method"] == "Network.requestWillBeSent":
                request = params.get("request", {})
                if "_dash-update-component" in request.get("url", ""):
                    body = json.loads(request.get("postData") or "{}")
                    sent[params["requestId"]] = str(body.get("output"))[:120]
            elif message["method"] == "Network.responseReceived":
                answered[params["requestId"]] = params.get("response", {}).get("status")
        for request_id, output in sent.items():
            lines.append(f"callback {output}: {answered.get(request_id, 'no answer')}")
    except (WebDriverException, ValueError, KeyError) as exc:
        lines.append(f"no report: {exc!r}")
    return "\n".join(lines[-80:]) or "the browser logged nothing"


def text_of(driver: WebDriver, element_id: str) -> str:
    found = driver.find_elements(By.ID, element_id)
    return found[0].text if found else ""


def open_page(driver: WebDriver, url: str) -> None:
    driver.get(url)
    wait_for(driver, lambda: "#" in text_of(driver, ids.VERSION_DETAIL), "the selected version")


def selected_seq(driver: WebDriver) -> int | None:
    text = text_of(driver, ids.VERSION_DETAIL)
    if not text.startswith("#"):
        return None
    return int(text[1:].split()[0])


def head_seq(driver: WebDriver) -> int | None:
    text = text_of(driver, ids.HEAD_LABEL)
    if not text.startswith("head #"):
        return None
    return int(text.removeprefix("head #").split()[0])


def select_version(driver: WebDriver, seq: int) -> None:
    """Move the version slider with the keyboard, as a user can: Home, then arrows."""
    thumb = driver.find_element(By.CSS_SELECTOR, f"#{ids.SLIDER} [role=slider]")
    thumb.send_keys(Keys.HOME)
    if seq:
        thumb.send_keys(*([Keys.ARROW_RIGHT] * seq))
    wait_for(driver, lambda: selected_seq(driver) == seq, f"version #{seq} to be selected")


def click_tab(driver: WebDriver, label: str) -> None:
    for tab in driver.find_elements(By.CSS_SELECTOR, f"#{ids.TABS} .tab"):
        if tab.text.strip() == label:
            tab.click()
            return
    raise AssertionError(f"no tab {label!r}")


def follow_checkbox(driver: WebDriver) -> WebElement:
    return driver.find_element(By.CSS_SELECTOR, f"#{ids.FOLLOW} input[type=checkbox]")


# -- graph -----------------------------------------------------------------------

_CY = (
    "const box = document.getElementById(arguments[0]);"
    "const cy = box && box._cyreg && box._cyreg.cy;"
)


def graph_nodes(driver: WebDriver) -> list[str]:
    return driver.execute_script(
        _CY + "return cy ? cy.nodes().map(n => n.id()) : [];", ids.GRAPH_VIEW
    )


def graph_edges(driver: WebDriver) -> list[dict[str, Any]]:
    return driver.execute_script(
        _CY + "return cy ? cy.edges().map(e => e.data()) : [];", ids.GRAPH_VIEW
    )


def graph_status(driver: WebDriver) -> str:
    return text_of(driver, ids.GRAPH_STATUS)


def wait_graph_at(driver: WebDriver, seq: int) -> list[str]:
    wait_for(driver, lambda: f"at #{seq}" in graph_status(driver), f"the graph at #{seq}")
    return wait_for(driver, lambda: graph_nodes(driver), "nodes drawn")


def click_graph_element(driver: WebDriver, element_id: str) -> None:
    """A real click on a drawn node or edge, at its position on the canvas."""
    view = driver.find_element(By.ID, ids.GRAPH_VIEW)

    def position() -> dict[str, float] | None:
        return driver.execute_script(
            _CY
            + "if (!cy) return null; const el = cy.getElementById(arguments[1]);"
            + "if (el.empty()) return null;"
            + "return el.isNode() ? el.renderedPosition() : el.renderedMidpoint();",
            ids.GRAPH_VIEW,
            element_id,
        )

    # Wait for the layout to settle: the position stops moving.
    first = wait_for(driver, position, f"{element_id} to be drawn")
    time.sleep(0.3)
    point = position() or first
    size = view.size
    ActionChains(driver).move_to_element_with_offset(
        view, point["x"] - size["width"] / 2, point["y"] - size["height"] / 2
    ).click().perform()


def choose_dropdown(
    driver: WebDriver, element_id: str, text: str, typed: str | None = None
) -> None:
    """Open a Dash dropdown, type into its search field and click the option shown as `text`."""
    box = driver.find_element(By.ID, element_id)
    box.click()
    popup_id = wait_for(driver, lambda: box.get_attribute("aria-controls"), f"{element_id} to open")
    popup = wait_for(driver, lambda: driver.find_element(By.ID, popup_id), "the dropdown's list")
    if typed is not None:
        search = wait_for(
            driver,
            lambda: [
                field for field in popup.find_elements(By.TAG_NAME, "input") if field.is_displayed()
            ],
            f"the search field of {element_id}",
        )[0]
        search.send_keys(typed)
    option = wait_for(
        driver,
        lambda: [
            item
            for item in driver.find_element(By.ID, popup_id).find_elements(
                By.CSS_SELECTOR, "[role=option]"
            )
            if item.is_displayed() and item.text.strip() == text
        ],
        f"option {text!r} of {element_id}",
    )[0]
    option.click()


def set_input(driver: WebDriver, element_id: str, value: str) -> None:
    field = driver.find_element(By.ID, element_id)
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.DELETE)
    field.send_keys(value, Keys.ENTER)


# -- grids -----------------------------------------------------------------------


def grid_cells(driver: WebDriver, container_id: str, column: str) -> list[str]:
    """Texts of one column of the rows a grid has drawn, in row order."""
    rows = driver.find_elements(
        By.CSS_SELECTOR, f"#{container_id} .ag-center-cols-container .ag-row"
    )
    found: list[tuple[int, str]] = []
    for row in rows:
        index = row.get_attribute("row-index")
        cells = row.find_elements(By.CSS_SELECTOR, f".ag-cell[col-id='{column}']")
        if cells and index is not None and index.isdigit():
            found.append((int(index), cells[0].text))
    if not found:
        rows = driver.find_elements(
            By.CSS_SELECTOR, f"#{container_id} .ag-pinned-left-cols-container .ag-row"
        )
        for row in rows:
            index = row.get_attribute("row-index")
            cells = row.find_elements(By.CSS_SELECTOR, f".ag-cell[col-id='{column}']")
            if cells and index is not None and index.isdigit():
                found.append((int(index), cells[0].text))
    return [text for _, text in sorted(found)]


def grid_headers(driver: WebDriver, container_id: str) -> list[str]:
    return [
        header.get_attribute("col-id")
        for header in driver.find_elements(By.CSS_SELECTOR, f"#{container_id} .ag-header-cell")
    ]


def scroll_grid(driver: WebDriver, container_id: str, top: int) -> None:
    viewport = driver.find_element(By.CSS_SELECTOR, f"#{container_id} .ag-body-viewport")
    driver.execute_script("arguments[0].scrollTop = arguments[1];", viewport, top)


def header_filter_input(driver: WebDriver, container_id: str, column: str) -> WebElement:
    """The floating filter field of a column: the header cell of that column holding an input."""
    return wait_for(
        driver,
        lambda: [
            field
            for cell in driver.find_elements(
                By.CSS_SELECTOR, f"#{container_id} .ag-header-cell[col-id='{column}']"
            )
            for field in cell.find_elements(By.TAG_NAME, "input")
            if field.is_displayed() and field.is_enabled()
        ],
        f"the filter field of {column}",
    )[0]


def sort_by(driver: WebDriver, container_id: str, column: str) -> None:
    driver.find_element(
        By.CSS_SELECTOR, f"#{container_id} .ag-header-cell[col-id='{column}'] .ag-header-cell-label"
    ).click()


# -- network ---------------------------------------------------------------------


def network_urls(driver: WebDriver) -> list[str]:
    """Every URL the browser requested since the last call."""
    urls = []
    for entry in driver.get_log("performance"):
        message = json.loads(entry["message"])["message"]
        if message["method"] == "Network.requestWillBeSent":
            urls.append(message["params"]["request"]["url"])
    return urls
