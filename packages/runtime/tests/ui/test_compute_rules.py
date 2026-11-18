"""The interface's rules on its questions, branch selector and scenario comparison: every
word they show comes from `text.py`, and the API token reaches no browser."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from dta_runtime import ui
from dta_runtime.ui import ids
from dta_runtime.ui import text as T
from selenium.webdriver.common.by import By
from test_token import assert_token_kept
from ui_page import (
    choose_branch,
    choose_question,
    choose_scenario,
    click_tab,
    open_page,
    press_ask,
    selected_seq,
    text_of,
    wait_answered,
    wait_for,
)

UI_SOURCE = Path(ui.__file__).parent
MODULES = (
    "questions.py",
    "answers.py",
    "branches.py",
    "scenarios.py",
    "badge.py",
    "features.py",
    "fidelity.py",
    "actions.py",
)
# Arguments and keys whose text the page shows.
SHOWN_KEYWORDS = {"children", "label", "placeholder", "title", "detail", "name", "headerName"}
SHOWN_KEYS = {"label", "headerName", "text"}
# Calls whose first argument the page shows.
SHOWN_CALLS = {"note", "html", "dcc"}


def _literal_words(node: ast.AST) -> list[str]:
    """Words written into the code at a place the page shows: string literals that are more
    than spacing, and f-strings, inside the node (lists and tuples looked into)."""
    found: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        found.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        found.append(ast.unparse(node))
    elif isinstance(node, ast.List | ast.Tuple):
        for item in node.elts:
            found += _literal_words(item)
    return found


def _shown_words(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            owner = (
                func.value.id
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else None
            )
            name = func.id if isinstance(func, ast.Name) else None
            if owner in ("html", "dcc") or name in ("note",):
                for argument in node.args[:1]:
                    found += [(node.lineno, word) for word in _literal_words(argument)]
            if owner in ("html", "dcc", "go", "dag") or name in ("ApiProblem", "note"):
                for keyword in node.keywords:
                    if keyword.arg in SHOWN_KEYWORDS:
                        found += [(node.lineno, word) for word in _literal_words(keyword.value)]
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in SHOWN_KEYS:
                    found += [(node.lineno, word) for word in _literal_words(value)]
    return found


@pytest.mark.parametrize("module", MODULES)
def test_every_word_the_new_parts_show_comes_from_the_text_module(module):
    written = _shown_words(UI_SOURCE / module)
    assert not written, f"{module} writes words of its own: {written}"


def test_the_rule_finds_words_written_into_the_code(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "html.Div('Hello there')\n"
        "note(f'{count} things')\n"
        "dcc.Input(placeholder='type here')\n"
        "x = {'label': 'live', 'value': 'main'}\n"
        "html.Span([' ', T.WORD])\n",
        encoding="utf-8",
    )
    assert [word for _, word in _shown_words(sample)] == [
        "Hello there",
        "f'{count} things'",
        "type here",
        "live",
    ]


def test_the_new_parts_show_the_words_of_the_text_module(computed, browser):
    open_page(browser, computed.ui_url)
    tabs = [tab.text.strip() for tab in browser.find_elements(By.CSS_SELECTOR, f"#{ids.TABS} .tab")]
    assert T.QUESTIONS_TAB in tabs and T.SCENARIOS_TAB in tabs
    header = browser.find_element(By.ID, ids.HEADER).text
    assert T.BRANCH_SELECT_LABEL in header and T.LIVE_OPTION in header
    assert T.FOLLOW_LABEL.strip() in header

    click_tab(browser, T.QUESTIONS_TAB)
    wait_for(browser, lambda: T.QUESTIONS_HEADING in browser.page_source, "the questions tab")
    for words in (T.QUESTIONS_HEADING, T.ASK_HEADING, T.ANSWER_HEADING, T.CHOOSE_QUESTION):
        assert words in browser.find_element(By.CLASS_NAME, "questions-tab").text
    assert browser.find_element(By.ID, ids.ASK).text == T.ASK
    headers = [
        cell.text
        for cell in browser.find_elements(
            By.CSS_SELECTOR, f"#{ids.QUESTIONS_GRID} .ag-header-cell-text"
        )
    ]
    assert headers == list(T.QUESTION_COLUMNS.values())

    click_tab(browser, T.SCENARIOS_TAB)
    wait_for(browser, lambda: T.SCENARIOS_HEADING in browser.page_source, "the scenarios tab")
    tab = browser.find_element(By.CLASS_NAME, "scenarios-tab").text
    for words in (T.SCENARIOS_HEADING, T.CHOOSE_SCENARIO, T.TARGETS_LABEL, T.AGAINST_LABEL):
        assert words in tab
    assert browser.find_element(By.ID, ids.COMPARE).text == T.COMPARE


def test_the_token_never_reaches_the_browser_from_the_new_parts(computed, browser):
    open_page(browser, computed.ui_url)
    closed = computed.scenarios["closed"]
    click_tab(browser, T.QUESTIONS_TAB)
    wait_for(browser, lambda: text_of(browser, ids.QUESTIONS_STATUS), "the questions")
    choose_question(browser, "levels")
    press_ask(browser)
    wait_answered(browser)
    choose_question(browser, "stuck")
    press_ask(browser)
    wait_for(browser, lambda: text_of(browser, ids.ASK_FEEDBACK), "the refusal")
    choose_branch(browser, T.scenario_option(closed["name"], closed["base_t"], closed["status"]))
    wait_for(browser, lambda: selected_seq(browser) == closed["head"]["seq"], "the scenario")
    click_tab(browser, T.SCENARIOS_TAB)
    choose_scenario(browser, closed["id"], closed["name"])
    browser.find_element(By.ID, ids.COMPARE).click()
    wait_for(
        browser,
        lambda: (
            T.compared_text(closed["name"], T.LIVE_OPTION, 0, 0)[:20]
            in text_of(browser, ids.COMPARISON)
        ),
        "the comparison",
    )
    assert_token_kept(browser, computed)
