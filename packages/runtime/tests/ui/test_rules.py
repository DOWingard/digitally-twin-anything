"""Rules the interface keeps by construction: it never imports the store, and grid
filters become exactly the API's `where` clauses, or a problem when the API has none."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from dta_runtime import ui
from dta_runtime.ui.client import ApiProblem
from dta_runtime.ui.sheet import sort_parameter, where_clauses

UI_SOURCE = Path(ui.__file__).parent
# Modules that hold or reach a store connection; the interface reads through the API only.
FORBIDDEN = (
    "dta_core.store",
    "dta_runtime.twin",
    "dta_runtime.api",
    "dta_runtime.graph",
    "pymysql",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            found += [alias.name for alias in item.names]
        elif isinstance(item, ast.ImportFrom):
            base = item.module or ""
            if item.level:
                package = "dta_runtime.ui".split(".")
                base = ".".join(package[: len(package) - item.level + 1] + ([base] if base else []))
            found.append(base)
            found += [f"{base}.{alias.name}" for alias in item.names]
    return found


def test_the_interface_imports_nothing_that_reaches_the_store():
    sources = sorted(UI_SOURCE.rglob("*.py"))
    assert sources
    for path in sources:
        for name in _imports(path):
            assert not name.startswith(FORBIDDEN), f"{path.name} imports {name}"


KINDS = {
    "uid": "string",
    "name": "string",
    "elevation": "float",
    "sequence": "integer",
    "observed_at": "datetime",
    "installed_on": "date",
    "active": "boolean",
}


@pytest.mark.parametrize(
    ("model", "clauses"),
    [
        ({"name": {"filterType": "text", "type": "contains", "filter": "J"}}, ["name:like:%J%"]),
        ({"name": {"filterType": "text", "type": "startsWith", "filter": "J"}}, ["name:like:J%"]),
        ({"name": {"filterType": "text", "type": "endsWith", "filter": "1"}}, ["name:like:%1"]),
        ({"name": {"filterType": "text", "type": "equals", "filter": "J1"}}, ["name:eq:J1"]),
        ({"name": {"filterType": "text", "type": "notEqual", "filter": "J1"}}, ["name:ne:J1"]),
        ({"name": {"filterType": "text", "type": "blank"}}, ["name:null"]),
        ({"name": {"filterType": "text", "type": "notBlank"}}, ["name:notnull"]),
        (
            {"elevation": {"filterType": "number", "type": "greaterThan", "filter": 40}},
            ["elevation:gt:40"],
        ),
        (
            {"elevation": {"filterType": "number", "type": "lessThanOrEqual", "filter": 4.5}},
            ["elevation:le:4.5"],
        ),
        (
            {
                "elevation": {
                    "filterType": "number",
                    "type": "inRange",
                    "filter": 30,
                    "filterTo": 45,
                }
            },
            ["elevation:ge:30", "elevation:le:45"],
        ),
        (
            {"sequence": {"filterType": "number", "type": "equals", "filter": 4.0}},
            ["sequence:eq:4"],
        ),
        # Not an integer: passed on as typed, for the API to refuse.
        (
            {"sequence": {"filterType": "number", "type": "equals", "filter": 2.5}},
            ["sequence:eq:2.5"],
        ),
        (
            {
                "observed_at": {
                    "filterType": "date",
                    "type": "greaterThan",
                    "dateFrom": "2026-01-05 07:30:00",
                }
            },
            ["observed_at:gt:2026-01-05T07:30:00Z"],
        ),
        (
            {
                "installed_on": {
                    "filterType": "date",
                    "type": "equals",
                    "dateFrom": "2025-12-01 00:00:00",
                }
            },
            ["installed_on:eq:2025-12-01"],
        ),
        (
            {"active": {"filterType": "text", "type": "equals", "filter": "true"}},
            ["active:eq:true"],
        ),
        # A condition without its value yet is no filter.
        ({"name": {"filterType": "text", "type": "contains", "filter": ""}}, []),
        (None, []),
    ],
)
def test_grid_filters_become_the_apis_where_clauses(model, clauses):
    assert where_clauses(model, KINDS) == clauses


@pytest.mark.parametrize(
    "model",
    [
        {"name": {"filterType": "text", "type": "notContains", "filter": "J"}},
        {
            "name": {
                "filterType": "text",
                "operator": "OR",
                "conditions": [
                    {"filterType": "text", "type": "equals", "filter": "J1"},
                    {"filterType": "text", "type": "equals", "filter": "J2"},
                ],
            }
        },
        {"missing": {"filterType": "text", "type": "equals", "filter": "x"}},
    ],
)
def test_a_filter_the_api_cannot_express_is_a_problem(model):
    with pytest.raises(ApiProblem) as caught:
        where_clauses(model, KINDS)
    assert caught.value.code == "ui.unsupported_filter"


def test_the_first_sort_column_becomes_the_apis_sort():
    assert sort_parameter([]) is None
    assert sort_parameter([{"colId": "elevation", "sort": "asc"}]) == "elevation"
    assert sort_parameter([{"colId": "elevation", "sort": "desc"}]) == "-elevation"
