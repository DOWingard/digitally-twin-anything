"""The tables a view reads, found without an SQL engine: every table, and nothing else."""

from __future__ import annotations

import pytest
from dta_core.manifest.checks.sqlrefs import scan

CASES = [
    ('SELECT * FROM "export"."weather"', [("export", "weather")]),
    ("SELECT * FROM weather", [("weather",)]),
    ("select * from export.weather w", [("export", "weather")]),
    (
        'SELECT s.asset_id, r."timestamp", r.value FROM "telemetry"."readings" r '
        'JOIN "telemetry"."sensors" s ON s.sensor_id = r.sensor_id WHERE s.quantity = \'level\'',
        [("telemetry", "readings"), ("telemetry", "sensors")],
    ),
    ("SELECT * FROM a, b AS x, c", [("a",), ("b",), ("c",)]),
    ("SELECT * FROM a LEFT OUTER JOIN b USING (id) NATURAL JOIN c", [("a",), ("b",), ("c",)]),
    ("SELECT extract(epoch FROM ts) AS e FROM s.t", [("s", "t")]),
    ("SELECT substring(name FROM 2 FOR 3) FROM s.t", [("s", "t")]),
    ("SELECT a IS DISTINCT FROM b FROM s.t", [("s", "t")]),
    ("SELECT * FROM (SELECT * FROM s.t) AS sub", [("s", "t")]),
    ("SELECT * FROM s.t WHERE id IN (SELECT id FROM s.u)", [("s", "t"), ("s", "u")]),
    ("SELECT * FROM range(10)", []),
    ("SELECT * FROM read_csv('x.csv')", []),
    ("SELECT 'FROM weather' AS text, x FROM s.t", [("s", "t")]),
    ("SELECT x -- FROM weather\nFROM s.t", [("s", "t")]),
    ("SELECT x /* FROM weather */ FROM s.t", [("s", "t")]),
    ("FROM s.t SELECT x", [("s", "t")]),
    ("SELECT * FROM s.t UNION ALL SELECT * FROM s.u", [("s", "t"), ("s", "u")]),
    ("SELECT * FROM c.s.t", [("c", "s", "t")]),
    ('SELECT * FROM "odd ""name"""."t"', [('odd "name"', "t")]),
    ("SELECT max(x) OVER (PARTITION BY y ORDER BY z) FROM s.t", [("s", "t")]),
    ("SELECT CASE WHEN v >= 0.5 THEN 'on' ELSE 'off' END AS status FROM s.t", [("s", "t")]),
]


@pytest.mark.parametrize(("sql", "tables"), CASES)
def test_the_tables_of_a_select(sql, tables):
    assert scan(sql).tables == tables


def test_names_a_with_clause_defines_are_kept_apart():
    found = scan(
        "WITH RECURSIVE a(x) AS (SELECT * FROM s.t), b AS MATERIALIZED (SELECT * FROM a) "
        "SELECT * FROM b JOIN s.u USING (x)"
    )
    assert found.ctes == {"a", "b"}
    assert found.tables == [("s", "t"), ("a",), ("b",), ("s", "u")]


def test_statements_are_counted():
    assert scan("SELECT 1;").statements == 1
    assert scan("SELECT 1; SELECT 2").statements == 2
