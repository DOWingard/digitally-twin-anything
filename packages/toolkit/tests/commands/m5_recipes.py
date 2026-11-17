"""The skill's recipes for what a twin computes, run as written against the m5 twin.

Each recipe's Python is taken from the rendered `reference/recipes.md`, its
placeholders replaced by real names of the twin, and run after the Setup and
Orient recipes in one namespace, the way an agent would run them in order.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDERS: dict[str, dict[str, str]] = {
    "Answer a question": {
        "/questions/<question>/answer": "/questions/q_level/answer",
        '{"<argument>": "<value>"}': '{"property": "Sensor.level"}',
    },
    "Run a function with arguments": {
        "<function>": "f_report",
        '{"<argument>": "<value>"}': '{"uid": "sensor:n1"}',
    },
    "List models and read a backtest": {'"<spec>"': '"level"'},
    "Predict": {},
    "Evaluate again after new data": {'"<spec>"': '"level"'},
    "Build a training set": {'"<Type>.<property>"': '"Sensor.level"'},
    "Ask what would happen: scenarios": {
        '"<Type>.<property>"': '"Site.inflow"',
        '"<uid>"': '"site:north"',
        '"<number>"': "0.5",
        '"<model uid>"': '"twin_model:whatif/drift"',
        '"<name>"': '"more inflow"',
        '"<name>, unchanged"': '"more inflow, unchanged"',
        '"<why>"': '"half a metre an hour more into the north works"',
        '"<Type>.<property the models predict>"': '"Sensor.level"',
    },
    "Keep values you worked out": {
        '"<uid>"': '"sensor:n1"',
        '"<property>"': '"estimate"',
        '"<number>"': "4.5",
        '"<Type>"': '"Sensor"',
        '"<function or model uid>"': '"twin_function:f_report"',
    },
    "Search across types": {"<text>": "n1"},
}

_SECTION = re.compile(r"^## (?:\d+\. )?(?P<title>.+)$", re.MULTILINE)
_PYTHON = re.compile(r"```python\n(?P<code>.*?)```", re.DOTALL)


def sections(text: str) -> dict[str, str]:
    """The body of each `## ` section of a rendered recipes file, by title."""
    found = list(_SECTION.finditer(text))
    return {
        match.group("title"): text[
            match.end() : found[i + 1].start() if i + 1 < len(found) else None
        ]
        for i, match in enumerate(found)
    }


def python_of(body: str) -> list[str]:
    return [match.group("code") for match in _PYTHON.finditer(body)]


def prepared(text: str) -> list[tuple[str, str]]:
    """(title, code) of each recipe to run, in order, placeholders replaced."""
    found = sections(text)
    setup = python_of(found["Setup"])
    steps = [("Setup", setup[0]), ("Orient", python_of(found["Orient"])[0])]
    for title, replacements in PLACEHOLDERS.items():
        codes = python_of(found[title])
        assert len(codes) == 1, (title, len(codes))
        code = codes[0]
        for old, new in replacements.items():
            assert old in code, (title, old)
            code = code.replace(old, new)
        assert '"<' not in code and "/<" not in code, (title, code)
        steps.append((title, code))
    return steps


def run(text: str) -> dict[str, Any]:
    """Run every recipe; the namespace they leave behind."""
    namespace: dict[str, Any] = {"__name__": "recipes"}
    for title, code in prepared(text):
        try:
            exec(compile(code, f"<recipe {title}>", "exec"), namespace)  # noqa: S102
        except Exception as exc:
            raise AssertionError(f"recipe {title!r} failed: {exc}\n{code}") from exc
    return namespace
