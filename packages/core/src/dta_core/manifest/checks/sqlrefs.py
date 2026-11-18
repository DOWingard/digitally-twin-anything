"""The tables a view's SELECT reads, found without running or fully parsing it.

The twin runs a source's views in DuckDB, where each dataset is the table
`"<source>"."<dataset>"`. Its load parses them with DuckDB itself; the toolkit,
which validates the manifest before anything is built, has no SQL engine, so it
tokenises the statement and follows the clauses that name tables: `FROM` (in a
query, not inside `EXTRACT(... FROM ...)` or `IS DISTINCT FROM`), `JOIN` and the
commas of a `FROM` list. What it cannot tell apart from a table it leaves out:
a finding here must never refuse a view the load would accept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
   |(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
   |(?P<string>[Ee]?'(?:[^']|'')*(?:'|\Z))
   |(?P<dollar>\$(?P<tag>[A-Za-z_]*)\$.*?(?:\$(?P=tag)\$|\Z))
   |(?P<quoted>"(?:[^"]|"")*(?:"|\Z))
   |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
   |(?P<number>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)
   |(?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
# Words after which a FROM list, or the table a JOIN names, has ended.
_CLAUSE_ENDS = frozenset(
    {
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "QUALIFY",
        "WINDOW",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "SELECT",
        "FETCH",
        "RETURNING",
    }
)
_BEFORE_TABLE = frozenset({"LATERAL", "ONLY"})


@dataclass(frozen=True)
class Token:
    kind: str  # word, quoted, string, number or other
    text: str

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    @property
    def name(self) -> str | None:
        """The identifier the token is, as DuckDB reads it; None for anything else."""
        if self.kind == "word":
            return self.text
        if self.kind == "quoted" and len(self.text) >= 2 and self.text.endswith('"'):
            return self.text[1:-1].replace('""', '"')
        return None


@dataclass
class _Group:
    """One level of parentheses, or the statement itself."""

    query: bool = False  # a SELECT, or a FROM that opens it, was seen at this level
    seen: int = 0  # significant tokens seen at this level
    in_from: bool = False
    expect_table: bool = False
    # A WITH list: "name" waits for a CTE's name, "body" for its AS (...).
    with_state: str | None = None


@dataclass
class Scan:
    # Every table named, as its dotted parts: ("t",), ("source", "t") or more.
    tables: list[tuple[str, ...]] = field(default_factory=list)
    ctes: set[str] = field(default_factory=set)
    statements: int = 1


def tokens(sql: str) -> list[Token]:
    found = []
    for match in _TOKEN.finditer(sql):
        kind = match.lastgroup or "other"
        if kind in ("space", "comment"):
            continue
        if kind == "tag":  # the named group inside a dollar-quoted string
            kind = "dollar"
        found.append(Token("string" if kind == "dollar" else kind, match.group(0)))
    return found


def scan(sql: str) -> Scan:
    """The tables a SELECT statement names, and the names its WITH clause defines."""
    found = Scan()
    items = tokens(sql)
    stack = [_Group()]
    index = 0
    while index < len(items):
        token = items[index]
        group = stack[-1]
        previous = items[index - 1] if index else None
        index += 1
        if token.text == ";" and token.kind == "other":
            if len(stack) == 1 and index < len(items):
                found.statements += 1
                stack = [_Group()]
            continue

        if group.with_state == "name":
            name = token.name
            if token.upper == "RECURSIVE":
                continue
            if name is not None:
                found.ctes.add(name)
                group.with_state = "body"
                group.seen += 1
                continue
            group.with_state = None
        elif group.with_state == "body":
            if token.text == ",":
                group.with_state = "name"
                continue
            if token.upper in ("AS", "NOT", "MATERIALIZED"):
                continue
            if token.text != "(":
                group.with_state = None

        if group.expect_table:
            if token.upper in _BEFORE_TABLE:
                continue
            group.expect_table = False
            if token.name is not None and token.upper not in _CLAUSE_ENDS:
                parts = [token.name]
                while (
                    index + 1 < len(items)
                    and items[index].text == "."
                    and items[index + 1].name is not None
                ):
                    parts.append(items[index + 1].name or "")
                    index += 2
                if index < len(items) and items[index].text == "(":
                    continue  # a table function, such as range(10)
                found.tables.append(tuple(parts))
                group.seen += 1
                continue

        if token.text == "(" and token.kind == "other":
            group.seen += 1
            stack.append(_Group())
            continue
        if token.text == ")" and token.kind == "other":
            if len(stack) > 1:
                stack.pop()
            continue

        word = token.upper
        first = group.seen == 0
        group.seen += 1
        if word == "WITH":
            group.with_state = "name"
        elif word == "SELECT":
            group.query = True
            group.in_from = False
        elif word == "FROM":
            distinct = previous is not None and previous.upper == "DISTINCT"
            if (group.query or first) and not distinct:
                group.query = True
                group.in_from = True
                group.expect_table = True
        elif word == "JOIN" and group.in_from:
            group.expect_table = True
        elif word in _CLAUSE_ENDS:
            group.in_from = False
        elif token.text == "," and group.in_from:
            group.expect_table = True
    return found
