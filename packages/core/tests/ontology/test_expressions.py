import pytest
from dta_core.ontology import screen_expression

COLUMNS = {"uid", "level", "min_level", "max_level", "status", "flow"}


@pytest.mark.parametrize(
    "expression",
    [
        "level >= 0",
        "level IS NULL OR (level >= min_level AND level <= max_level)",
        "status IN ('open', 'closed')",
        "status <> 'it''s'",
        "NOT (flow < -1.5e3) and level != 2 * min_level + .5 - max_level / 3 % 2",
        "level is not null",
    ],
)
def test_allowed_expressions(expression):
    assert screen_expression(expression, COLUMNS) == []


@pytest.mark.parametrize(
    ("expression", "rule"),
    [
        ("level >= 0; DROP TABLE tank", "check.syntax"),
        ("level >= 0 -- comment", "check.syntax"),
        ("level >= 0 /* comment */", "check.syntax"),
        ("level # comment", "check.syntax"),
        ('status = "open"', "check.syntax"),
        ("status = 'open", "check.syntax"),
        ("status = 'a\\' OR 1=1 --'", "check.syntax"),
        ("status = `open`", "check.syntax"),
        ("(level > 0", "check.syntax"),
        ("level > 0)", "check.syntax"),
        ("", "check.syntax"),
        (None, "check.syntax"),
        ("height > 0", "check.identifier"),
        ("LENGTH(status) > 0", "check.identifier"),
        ("level BETWEEN 0 AND 1", "check.identifier"),
        ("_prov IS NULL", "check.identifier"),
        ("Level > 0", "check.identifier"),
    ],
)
def test_rejected_expressions(expression, rule):
    issues = screen_expression(expression, COLUMNS)
    assert rule in {issue.rule for issue in issues}, issues
