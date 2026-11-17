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
from ui_testkit import RUN_NOW_S

WAIT_S = 15.0
NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED"


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
    """Load the page and wait for its version. Chrome drops the requests in flight when a
    network interface of the machine changes (docker adds or removes one for each container
    started on it), and the page then never renders: such a load, and only such a load, is
    tried once more."""
    for attempt in (1, 2):
        driver.get(url)
        try:
            wait_for(
                driver, lambda: "#" in text_of(driver, ids.VERSION_DETAIL), "the selected version"
            )
            return
        except AssertionError as failure:
            if attempt == 2 or NETWORK_CHANGED not in str(failure):
                raise


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


def option_uid(text: str) -> str:
    """The uid an object search option is shown with first: `node:J2 · Junction · J2`."""
    return text.split(" · ", 1)[0]


def choose_object(driver: WebDriver, uid: str, typed: str) -> None:
    """Search the graph's object field for `typed` and choose the object `uid`."""
    choose_dropdown(driver, ids.GRAPH_OBJECT, uid, typed=typed, shown=option_uid)


def choose_dropdown(
    driver: WebDriver,
    element_id: str,
    text: str,
    typed: str | None = None,
    shown: Callable[[str], str] = str.strip,
) -> None:
    """Open a Dash dropdown, type into its search field and click the option shown as `text`
    (`shown` reads what an option shows from its text). A dropdown with a pattern-matching
    id is named by a CSS selector beginning with `[`."""
    by = By.CSS_SELECTOR if element_id.startswith("[") else By.ID
    box = driver.find_element(by, element_id)
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
            if item.is_displayed() and shown(item.text.strip()) == text
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


def bring_into_view(driver: WebDriver, element_id: str) -> None:
    """Scroll the page to an element. AG Grid leaves the rows of a grid that is off screen
    hidden until it is scrolled to, so their texts read as empty until then."""
    driver.execute_script(
        "const found = document.getElementById(arguments[0]);"
        "if (found) found.scrollIntoView({block: 'nearest'});",
        element_id,
    )


def grid_cells(driver: WebDriver, container_id: str, column: str) -> list[str]:
    """Texts of one column of the rows a grid has drawn, in row order."""
    bring_into_view(driver, container_id)
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


def selected_cells(driver: WebDriver, container_id: str, column: str) -> list[str]:
    """Texts of one column of the rows a grid shows as selected."""
    bring_into_view(driver, container_id)
    return [
        cell.text
        for cell in driver.find_elements(
            By.CSS_SELECTOR,
            f"#{container_id} .ag-center-cols-container .ag-row-selected "
            f".ag-cell[col-id='{column}']",
        )
    ]


def grid_scroll_top(driver: WebDriver, container_id: str) -> int:
    viewport = driver.find_element(By.CSS_SELECTOR, f"#{container_id} .ag-body-viewport")
    return int(driver.execute_script("return arguments[0].scrollTop;", viewport))


def keep_grid(driver: WebDriver, container_id: str, name: str) -> None:
    """Remember the grid drawn in a container, to tell later whether it was drawn anew."""
    driver.execute_script(
        "window['kept_' + arguments[1]] = document.querySelector("
        "'#' + arguments[0] + ' .ag-root-wrapper');",
        container_id,
        name,
    )


def same_grid(driver: WebDriver, container_id: str, name: str) -> bool:
    """Whether the grid remembered under `name` is still the one drawn in the container."""
    return bool(
        driver.execute_script(
            "const kept = window['kept_' + arguments[1]];"
            "return !!kept && kept === document.querySelector("
            "'#' + arguments[0] + ' .ag-root-wrapper');",
            container_id,
            name,
        )
    )


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


# -- sync section ------------------------------------------------------------------


def open_history(driver: WebDriver, url: str) -> None:
    open_page(driver, url)
    click_tab(driver, "History")


def source_cells(driver: WebDriver, source_id: str) -> dict[str, Any]:
    """What the sources table shows of one source; empty while it is not drawn."""
    rows = driver.find_elements(By.CSS_SELECTOR, f"#{ids.SOURCES} tr[data-source='{source_id}']")
    if not rows:
        return {}
    cells = [cell.text for cell in rows[0].find_elements(By.TAG_NAME, "td")]
    return {
        "connector": cells[1],
        "status": cells[2],
        "watermark": cells[3],
        "datasets": cells[5],
        "error": cells[6],
        "marked": "marked" in (rows[0].get_attribute("class") or ""),
    }


def click_run(driver: WebDriver, uid: str) -> None:
    """A click on a row of the runs grid, once the row is drawn where it can be clicked."""
    selector = f"#{ids.RUNS} .ag-row[row-id='{uid}'] .ag-cell[col-id='status']"

    def click() -> bool:
        bring_into_view(driver, ids.RUNS)
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if not found or not found[0].is_displayed():
            return False
        found[0].click()
        return True

    wait_for(driver, click, f"run {uid} to be clicked")


def run_now(driver: WebDriver) -> float:
    """Click "Run now"; how long until the button is disabled and says a run is in progress.

    A run that ends at once (every source refuses) may be over before the page was
    looked at: its outcome in place of the one shown before counts as well."""
    button = driver.find_element(By.ID, ids.RUN_NOW)
    wait_for(driver, lambda: button.is_enabled(), "the button to be enabled")
    shown = text_of(driver, ids.RUN_STATUS)
    started = time.monotonic()
    button.click()

    def begun() -> bool:
        status = text_of(driver, ids.RUN_STATUS)
        if status == "run in progress":
            return not button.is_enabled()
        return bool(status) and status != shown

    wait_for(driver, begun, "the run in progress", timeout=10.0)
    elapsed = time.monotonic() - started
    RUN_NOW_S.append(elapsed)
    return elapsed


def run_outcome(driver: WebDriver) -> str:
    """The words that tell how the run asked for from the page ended."""
    return wait_for(
        driver,
        lambda: (text := text_of(driver, ids.RUN_STATUS)).startswith("The run") and text,
        "the outcome of the run",
        timeout=20.0,
    )


# -- branches ----------------------------------------------------------------------


def dropdown_options(driver: WebDriver, element_id: str) -> list[str]:
    """The options a Dash dropdown offers, read by opening it and closed again after."""
    box = driver.find_element(By.ID, element_id)
    box.click()
    popup_id = wait_for(driver, lambda: box.get_attribute("aria-controls"), f"{element_id} to open")
    texts = wait_for(
        driver,
        lambda: [
            item.text.strip()
            for item in driver.find_element(By.ID, popup_id).find_elements(
                By.CSS_SELECTOR, "[role=option]"
            )
        ],
        f"the options of {element_id}",
    )
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    return texts


def choose_branch(driver: WebDriver, label: str) -> None:
    choose_dropdown(driver, ids.BRANCH_SELECT, label)


def slider_range(driver: WebDriver) -> tuple[int, int]:
    thumb = driver.find_element(By.CSS_SELECTOR, f"#{ids.SLIDER} [role=slider]")
    return int(thumb.get_attribute("aria-valuemin")), int(thumb.get_attribute("aria-valuemax"))


def slider_value(driver: WebDriver) -> int:
    thumb = driver.find_element(By.CSS_SELECTOR, f"#{ids.SLIDER} [role=slider]")
    return int(thumb.get_attribute("aria-valuenow"))


# -- scenarios ---------------------------------------------------------------------


def scenario_row(driver: WebDriver, scenario_id: str) -> dict[str, str]:
    """What the scenarios list shows of one scenario, by column."""
    bring_into_view(driver, ids.SCENARIO_LIST)
    found: dict[str, str] = {}
    for row in driver.find_elements(
        By.CSS_SELECTOR, f"#{ids.SCENARIO_LIST} .ag-row[row-id='{scenario_id}']"
    ):
        for cell in row.find_elements(By.CSS_SELECTOR, ".ag-cell"):
            column = str(cell.get_attribute("col-id"))
            found[column] = cell.text
            if column == "status":
                found["status_class"] = cell.get_attribute("class") or ""
    return found


def choose_scenario(driver: WebDriver, scenario_id: str, name: str) -> None:
    selector = f"#{ids.SCENARIO_LIST} .ag-row[row-id='{scenario_id}'] .ag-cell[col-id='name']"

    def click() -> bool:
        bring_into_view(driver, ids.SCENARIO_LIST)
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if not found or not found[0].is_displayed():
            return False
        found[0].click()
        return True

    wait_for(driver, click, f"scenario {name} to be clicked")
    wait_for(
        driver,
        lambda: text_of(driver, ids.SCENARIO_TITLE).startswith(f"{name}:"),
        f"scenario {name} to be chosen",
    )


def pattern_selector(role: str, target: str) -> str:
    """A CSS selector of a component made per comparison and target."""
    return f'[id*=\'"role":"{role}"\'][id*=\'"target":"{target}"\']'


def pattern_id(driver: WebDriver, role: str, target: str) -> str | None:
    """The DOM id of a component made per comparison and target."""
    found = driver.find_elements(By.CSS_SELECTOR, pattern_selector(role, target))
    return found[0].get_attribute("id") if found else None


def compare_key(driver: WebDriver) -> str | None:
    """The key of the comparison drawn: a new comparison has a new one."""
    found = driver.find_elements(By.CSS_SELECTOR, '[id*=\'"role":"compare-chart"\']')
    if not found:
        return None
    return json.loads(str(found[0].get_attribute("id")))["key"]


def compare_traces(driver: WebDriver, target: str) -> list[dict[str, Any]]:
    element_id = pattern_id(driver, "compare-chart", target)
    if element_id is None:
        return []
    found = driver.execute_script(
        "const box = document.getElementById(arguments[0]);"
        "const plot = box && (box.classList.contains('js-plotly-plot') ? box"
        " : box.querySelector('.js-plotly-plot'));"
        "return plot && plot.data ? JSON.parse(JSON.stringify(plot.data)) : null;",
        element_id,
    )
    return list(found or [])


# -- questions ---------------------------------------------------------------------


def question_ids(driver: WebDriver) -> list[str]:
    """The questions the table shows, in the order it shows them."""
    return grid_cells(driver, ids.QUESTIONS_GRID, "id")


def question_row(driver: WebDriver, question: str) -> dict[str, str]:
    """What the table shows of one question, by column; empty while it is not drawn."""
    bring_into_view(driver, ids.QUESTIONS_GRID)
    found: dict[str, str] = {}
    for row in driver.find_elements(
        By.CSS_SELECTOR, f"#{ids.QUESTIONS_GRID} .ag-row[row-id='{question}']"
    ):
        for cell in row.find_elements(By.CSS_SELECTOR, ".ag-cell"):
            found[str(cell.get_attribute("col-id"))] = cell.text
            classes = cell.get_attribute("class") or ""
            if cell.get_attribute("col-id") == "status":
                found["status_class"] = classes
    return found


def choose_question(driver: WebDriver, question: str) -> None:
    """Click a question in the table; wait for its form."""
    selector = f"#{ids.QUESTIONS_GRID} .ag-row[row-id='{question}'] .ag-cell[col-id='id']"

    def click() -> bool:
        bring_into_view(driver, ids.QUESTIONS_GRID)
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if not found or not found[0].is_displayed():
            return False
        found[0].click()
        return True

    wait_for(driver, click, f"question {question} to be clicked")
    wait_for(
        driver,
        lambda: (
            text_of(driver, ids.QUESTION_TITLE).startswith(f"{question}:")
            or text_of(driver, ids.QUESTION_TITLE) == question
        ),
        f"the form of {question}",
    )
    wait_for(
        driver,
        lambda: driver.find_element(By.ID, ids.ASK).is_enabled(),
        "the Ask button to be enabled",
    )


def argument_field(driver: WebDriver, name: str) -> WebElement:
    selector = f"#{ids.QUESTION_FORM} [data-argument='{name}'] input"
    return driver.find_element(By.CSS_SELECTOR, selector)


def argument_names(driver: WebDriver) -> list[str]:
    return [
        str(found.get_attribute("data-argument"))
        for found in driver.find_elements(By.CSS_SELECTOR, f"#{ids.QUESTION_FORM} [data-argument]")
    ]


def type_argument(driver: WebDriver, name: str, value: str) -> None:
    found = argument_field(driver, name)
    found.send_keys(Keys.CONTROL, "a")
    found.send_keys(Keys.DELETE)
    found.send_keys(value)


def press_ask(driver: WebDriver) -> None:
    button = driver.find_element(By.ID, ids.ASK)
    bring_into_view(driver, ids.ASK)
    button.click()


def answer_text(driver: WebDriver) -> str:
    return text_of(driver, ids.ANSWER)


def wait_answered(driver: WebDriver, timeout: float = 60.0) -> str:
    """The answer's words once one is shown."""
    return wait_for(
        driver,
        lambda: (text := answer_text(driver)).startswith("Answered at") and text,
        "an answer",
        timeout=timeout,
    )


def wait_refused(driver: WebDriver, timeout: float = 60.0) -> str:
    """The refusal shown beside the form."""
    return wait_for(
        driver,
        lambda: text_of(driver, ids.ASK_FEEDBACK),
        "a refusal beside the form",
        timeout=timeout,
    )


def figure_data(driver: WebDriver, element_id: str) -> list[dict[str, Any]]:
    """The traces of a drawn Plotly figure, as the browser holds them."""
    found = driver.execute_script(
        "const box = document.getElementById(arguments[0]);"
        "const plot = box && box.querySelector('.js-plotly-plot');"
        "return plot && plot.data ? JSON.parse(JSON.stringify(plot.data)) : null;",
        element_id,
    )
    return list(found or [])


# -- network ---------------------------------------------------------------------


def network_urls(driver: WebDriver) -> list[str]:
    """Every URL the browser requested since the last call."""
    urls = []
    for entry in driver.get_log("performance"):
        message = json.loads(entry["message"])["message"]
        if message["method"] == "Network.requestWillBeSent":
            urls.append(message["params"]["request"]["url"])
    return urls
