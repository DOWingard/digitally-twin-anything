"""The questions tab against a computed twin: the table says what the API says, a question
is asked only on a click, and its answer, or the API's refusal, is shown as the API gave it."""

from __future__ import annotations

import time
from typing import Any

from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from dta_runtime.ui.answers import BLOCK_ROWS
from ui_page import (
    answer_text,
    argument_field,
    argument_names,
    choose_question,
    click_tab,
    figure_data,
    grid_cells,
    open_page,
    press_ask,
    question_ids,
    question_row,
    scroll_grid,
    text_of,
    type_argument,
    wait_answered,
    wait_for,
    wait_refused,
)
from ui_testkit import QUESTIONS_LIMIT_S, QUESTIONS_S, TIMED_RUNS

ANSWER = "/api/v1/questions/{}/answer"
HEALTH = "/api/v1/meta/health"


def open_questions(driver: Any, url: str) -> float:
    """Open the page, then the questions tab; how long the table took to show."""
    open_page(driver, url)
    started = time.monotonic()
    click_tab(driver, T.QUESTIONS_TAB)
    wait_for(driver, lambda: question_ids(driver), "the questions table", timeout=30.0)
    return time.monotonic() - started


def page_requests(world: Any, mark: int) -> list[Any]:
    """What the interface asked the API for since `mark`, without its health polls."""
    return [seen for seen in world.api_requests.from_page(mark) if seen.path != HEALTH]


def api_questions(world: Any) -> dict[str, dict[str, Any]]:
    return {found["id"]: found for found in world.get("/questions", limit=1000)["data"]}


def stated_questions(world: Any) -> dict[str, dict[str, Any]]:
    return world.get("/meta/manifest")["manifest"]["contract"]["questions"]


def answered(world: Any, question: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
    response = world.post(f"/questions/{question}/answer", json=body, **params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_table_says_what_the_api_says(computed, browser):
    for _ in range(TIMED_RUNS):
        QUESTIONS_S.append(open_questions(browser, computed.ui_url))
    found = api_questions(computed)
    stated = stated_questions(computed)
    by_priority = sorted(found, key=lambda question: stated[question]["priority"])
    assert question_ids(browser) == by_priority
    head = computed.get("/meta/health")["head"]
    assert f"{len(found)} questions as of #{head['seq']}" in text_of(browser, ids.QUESTIONS_STATUS)
    for question, entry in found.items():
        shown = wait_for(
            browser, lambda question=question: question_row(browser, question), question
        )
        tolerance = stated[question]["tolerance"]
        unit = entry["unit"] or tolerance.get("unit")
        metric = entry["metric"] or tolerance.get("metric")
        assert shown["text"] == stated[question]["text"]
        assert shown["kind"] == stated[question]["kind"]
        assert shown["status"] == T.status_text(entry["status"])
        assert f"status-{entry['status']}" in shown["status_class"]
        assert shown["error"] == T.measure_text(entry["measured_error"], unit, metric)
        assert shown["tolerance"] == T.tolerance_text(entry["tolerance"], unit, metric)
        assert shown["basis"] == (entry["basis"] or "")
        assert shown["function"] == (entry["function"] or stated[question].get("function", ""))
        assert shown["model"] == (entry["model"] or "")
        assert shown["measured_at"] == T.time_text(entry["measured_at"])
        assert shown["reason"] == (entry["reason"] or "")
    # Every status the twin has is told by a word and a sign, not by colour alone.
    statuses = {entry["status"] for entry in found.values()}
    assert statuses == {"validated", "provisional", "failing"}
    assert found["level_ahead"]["model"] == "twin_model:level/naive"
    slow = [seconds for seconds in QUESTIONS_S if seconds > QUESTIONS_LIMIT_S]
    assert not slow, f"questions tab first render over {QUESTIONS_LIMIT_S} s: {QUESTIONS_S}"


def test_nothing_is_asked_before_the_click(computed, browser):
    open_questions(browser, computed.ui_url)
    mark = computed.api_requests.mark()
    choose_question(browser, "mean_level")
    assert argument_names(browser) == ["group_by", "uids", "where"]
    type_argument(browser, "uids", "node:T1, node:T2")
    type_argument(browser, "uids", "")
    time.sleep(1.0)
    asked = page_requests(computed, mark)
    assert [(seen.method, seen.path) for seen in asked] == [
        ("GET", "/api/v1/functions/mean_level")
    ], asked

    head = computed.get("/meta/health")["head"]
    press_ask(browser)
    text = wait_answered(browser)
    expected = answered(computed, "mean_level", {}, at=f"#{head['seq']}")
    value = expected["value"][0]["value"]
    assert T.number_text(value) == "5.05"
    assert f"{T.number_text(value)} m" in text
    assert f"Answered at #{head['seq']} · {T.time_text(head['t'])} by mean_level" in text
    posts = [seen for seen in page_requests(computed, mark) if seen.method == "POST"]
    assert [seen.path for seen in posts] == [ANSWER.format("mean_level")]
    assert posts[0].value("at") == f"#{head['seq']}"
    assert posts[0].value("branch") == "main"
    assert posts[0].json() == {}


def test_arguments_typed_are_sent(computed, browser):
    open_questions(browser, computed.ui_url)
    choose_question(browser, "mean_level")
    type_argument(browser, "uids", "node:T1, node:T3")
    mark = computed.api_requests.mark()
    press_ask(browser)
    text = wait_answered(browser)
    expected = answered(computed, "mean_level", {"uids": ["node:T1", "node:T3"]})
    assert f"{T.number_text(expected['value'][0]['value'])} m" in text
    (post,) = [seen for seen in page_requests(computed, mark) if seen.method == "POST"]
    assert post.json() == {"uids": ["node:T1", "node:T3"]}


def test_a_forecast_is_drawn_with_its_model(computed, browser):
    open_questions(browser, computed.ui_url)
    choose_question(browser, "level_ahead")
    assert argument_names(browser) == ["horizon", "uids", "levels"]
    hint = argument_field(browser, "levels").get_attribute("placeholder") or ""
    assert "default 80, 95" in hint
    press_ask(browser)
    text = wait_answered(browser, timeout=120.0)
    assert "Model: twin_model:level/naive" in text
    assert "predicted by the model twin_model:level/naive" in text
    traces = wait_for(browser, lambda: figure_data(browser, ids.ANSWER), "the forecast's chart")
    expected = answered(computed, "level_ahead", {}, limit=5000)["value"]
    series = sorted({row["unique_id"] for row in expected})
    means = {trace["name"]: trace for trace in traces if trace.get("mode") == "lines+markers"}
    assert sorted(means) == [f"{uid} {T.MEAN_NAME}" for uid in series]
    first = [row for row in expected if row["unique_id"] == "node:T1"]
    assert means[f"node:T1 {T.MEAN_NAME}"]["y"] == [row["mean"] for row in first]
    bands = [trace for trace in traces if trace.get("fill") == "tonexty"]
    # Each series has a band per interval level (80 and 95 %).
    assert len(bands) == 2 * len(series)
    band_80 = next(trace for trace in bands if trace["name"] == f"node:T1 {T.BAND_NAME} 80 %")
    assert band_80["y"] == [row["lo_80"] for row in first]
    # The rows are in the grid below as well.
    wait_for(browser, lambda: grid_cells(browser, ids.ANSWER, "unique_id"), "the forecast's rows")


def test_a_table_answer_is_read_a_page_at_a_time(computed, browser):
    open_questions(browser, computed.ui_url)
    choose_question(browser, "level_history")
    assert argument_names(browser) == ["from", "to", "uids", "step"]
    type_argument(browser, "from", "#1")
    type_argument(browser, "to", "head")
    mark = computed.api_requests.mark()
    press_ask(browser)
    wait_answered(browser)
    whole = answered(computed, "level_history", {"from": "#1", "to": "head"}, limit=10_000)
    rows = whole["value"]
    assert len(rows) > 3 * BLOCK_ROWS
    first = wait_for(browser, lambda: grid_cells(browser, ids.ANSWER, "value"), "the first rows")
    assert [float(found) for found in first] == [row["value"] for row in rows[: len(first)]]
    assert text_of(browser, ids.ANSWER).count(f"value [{whole['unit']}]") == 1

    def drawn_after(start: int) -> list[tuple[int, str]]:
        scroll_grid(browser, ids.ANSWER, 42 * (start + 10))
        cells = browser.find_elements(
            "css selector", f"#{ids.ANSWER} .ag-center-cols-container .ag-row"
        )
        found = []
        for row in cells:
            index = row.get_attribute("row-index")
            value = row.find_elements("css selector", ".ag-cell[col-id='value']")
            if index and index.isdigit() and int(index) >= start and value and value[0].text:
                found.append((int(index), value[0].text))
        return sorted(found)

    later = wait_for(browser, lambda: drawn_after(BLOCK_ROWS + 20), "rows of a later page")
    for index, shown in later:
        assert float(shown) == rows[index]["value"], index
    posts = [seen for seen in page_requests(computed, mark) if seen.method == "POST"]
    assert all(seen.path == ANSWER.format("level_history") for seen in posts)
    # The first page came with the click; a later one is asked for with the cursor it gave.
    assert posts[0].value("cursor") is None
    assert len(posts) >= 2 and all(seen.value("cursor") for seen in posts[1:])
    assert all(seen.json() == {"from": "#1", "to": "head"} for seen in posts)


def test_a_provisional_answer_says_so(computed, browser):
    open_questions(browser, computed.ui_url)
    choose_question(browser, "levels")
    press_ask(browser)
    text = wait_answered(browser)
    expected = answered(computed, "levels", {})
    assert expected["provisional"] is True
    notice = browser.find_element("css selector", f"#{ids.ANSWER} .notice.provisional")
    assert notice.text == T.provisional_text(expected["reasons"])
    assert notice.get_attribute("role") == "status"
    assert "not measured yet" in text
    shown = wait_for(browser, lambda: grid_cells(browser, ids.ANSWER, "uid"), "the levels")
    assert shown == [row["uid"] for row in expected["value"]][: len(shown)]


def refused(world: Any, question: str, body: dict[str, Any]) -> dict[str, Any]:
    response = world.post(f"/questions/{question}/answer", json=body)
    assert response.status_code >= 400, response.text
    return response.json()


def assert_refusal(browser: Any, problem: dict[str, Any]) -> None:
    shown = wait_for(
        browser,
        lambda: problem["code"] in (text := wait_refused(browser)) and text,
        f"the refusal {problem['code']}",
        timeout=60.0,
    )
    assert problem["title"] in shown
    assert problem["code"] in shown
    assert problem["detail"] in shown


def test_refusals_are_shown_beside_a_form_that_keeps_what_was_typed(computed, browser):
    open_questions(browser, computed.ui_url)
    choose_question(browser, "level_history")
    type_argument(browser, "to", "head")
    type_argument(browser, "step", "PT1H")
    press_ask(browser)
    assert_refusal(browser, refused(computed, "level_history", {"to": "head", "step": "PT1H"}))
    assert "function.param_missing" in text_of(browser, ids.ASK_FEEDBACK)
    assert argument_field(browser, "to").get_attribute("value") == "head"
    assert argument_field(browser, "step").get_attribute("value") == "PT1H"
    assert not answer_text(browser).startswith("Answered at")

    # Asking again with the missing argument given answers, and the refusal goes.
    type_argument(browser, "from", "#40")
    press_ask(browser)
    wait_answered(browser)
    assert text_of(browser, ids.ASK_FEEDBACK) == ""

    choose_question(browser, "slow")
    assert T.CODE_ARGUMENTS in text_of(browser, ids.QUESTION_FORM)
    press_ask(browser)
    assert_refusal(browser, refused(computed, "slow", {}))
    assert "function.timeout" in text_of(browser, ids.ASK_FEEDBACK)

    choose_question(browser, "level_whatif")
    assert T.NO_FUNCTION in text_of(browser, ids.QUESTION_FORM)
    press_ask(browser)
    assert_refusal(browser, refused(computed, "level_whatif", {}))
    assert "function.not_found" in text_of(browser, ids.ASK_FEEDBACK)


def test_refusals_of_a_twin_without_models_or_worker(plain, browser):
    open_questions(browser, plain.ui_url)
    choose_question(browser, "level_ahead")
    type_argument(browser, "horizon", "PT30M")
    press_ask(browser)
    assert_refusal(browser, refused(plain, "level_ahead", {"horizon": "PT30M"}))
    assert "model.none_active" in text_of(browser, ids.ASK_FEEDBACK)
    assert argument_field(browser, "horizon").get_attribute("value") == "PT30M"

    choose_question(browser, "slow")
    # Choosing another question takes the last one's refusal away.
    assert text_of(browser, ids.ASK_FEEDBACK) == ""
    press_ask(browser)
    assert_refusal(browser, refused(plain, "slow", {}))
    assert "worker.unavailable" in text_of(browser, ids.ASK_FEEDBACK)
