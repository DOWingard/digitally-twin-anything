"""Screening of check expressions before they become database constraints.

A check annotation is copied verbatim into a SQL `CHECK` clause, so it is held
to a small whitelist instead of being parsed: column names of its own class,
numbers, single-quoted string literals, comparison and arithmetic operators,
parentheses, commas (for `IN` lists) and the keywords AND, OR, NOT, IS, NULL,
IN. Function calls, other keywords, comments, backslashes and anything else
are refused, which keeps a malicious or mistaken ontology from injecting SQL.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

KEYWORDS = frozenset({"AND", "OR", "NOT", "IS", "NULL", "IN"})

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<string>'(?:[^'\\\r\n]|'')*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<operator><=|>=|<>|!=|=|<|>|\+|-|\*|/|%)
  | (?P<paren>[()])
  | (?P<comma>,)
    """,
    re.VERBOSE,
)
# Sequences that open a comment in MySQL-compatible engines.
_COMMENT_OPENERS = ("--", "/*", "*/")


@dataclass(frozen=True)
class ExpressionIssue:
    rule: str  # "check.syntax" or "check.identifier"
    message: str


def screen_expression(expression: object, columns: Collection[str]) -> list[ExpressionIssue]:
    """Every reason `expression` may not be used as a check on a table with `columns`."""
    if not isinstance(expression, str) or not expression.strip():
        return [ExpressionIssue("check.syntax", "check expression must be a non-empty string")]

    issues: list[ExpressionIssue] = []
    position = 0
    depth = 0
    previous: tuple[str, str, int] | None = None  # (group, text, end position)
    unknown: list[str] = []
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            character = expression[position]
            if character == "'":
                reason = "unterminated string literal, or a backslash or line break inside one"
            else:
                reason = f"character {character!r} is not allowed"
            issues.append(ExpressionIssue("check.syntax", f"{reason} (at offset {position})"))
            return issues
        group = match.lastgroup or ""
        text = match.group()
        if group == "operator" and previous is not None and previous[2] == match.start():
            pair = previous[1] + text
            if previous[0] == "operator" and any(opener in pair for opener in _COMMENT_OPENERS):
                issues.append(
                    ExpressionIssue("check.syntax", f"comment sequence {pair!r} is not allowed")
                )
        if group == "paren":
            depth += 1 if text == "(" else -1
            if depth < 0:
                issues.append(
                    ExpressionIssue("check.syntax", f"unbalanced ')' at offset {match.start()}")
                )
                depth = 0
        elif group == "name" and text.upper() not in KEYWORDS and text not in columns:
            if text not in unknown:
                unknown.append(text)
        if group != "space":
            previous = (group, text, match.end())
        else:
            previous = None
        position = match.end()

    if depth > 0:
        issues.append(ExpressionIssue("check.syntax", "unbalanced '(': missing ')'"))
    for name in unknown:
        issues.append(
            ExpressionIssue(
                "check.identifier",
                f"{name!r} is neither a column of this class nor an allowed keyword",
            )
        )
    return issues
