"""Checks that need no database: selectors, value types, settings and generated tables."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest
from dta_core.errors import ConfigError
from dta_core.schema import FactKind, Field, Kind, ObjectType, Ontology
from dta_core.store import (
    Cause,
    CommitMeta,
    InvalidInput,
    InvalidOntology,
    Provenance,
    Selector,
    Store,
    StoreSettings,
    ddl,
)
from dta_core.store.codec import encode
from dta_core.store.validate import DescriptorValidator
from dta_store_testkit import water_ontology

COMMIT = "0123456789abcdefghijklmnopqrstuv"


# -- selectors -----------------------------------------------------------------


def test_single_selectors_parse_to_their_kind():
    assert Selector.parse("head").kind == "head"
    assert Selector.parse(COMMIT) == Selector("id", id=COMMIT)
    assert Selector.parse("#42") == Selector("seq", seq=42)
    parsed = Selector.parse("2024-05-01T12:00:00+02:00")
    assert parsed.kind == "time" and parsed.time == datetime(2024, 5, 1, 10, tzinfo=UTC)
    assert Selector.parse("2024-05-01T12:00:00Z").time == datetime(2024, 5, 1, 12, tzinfo=UTC)


def test_ranges_parse_with_any_single_selector_at_each_end_and_a_step():
    parsed = Selector.parse("#3..head")
    assert parsed.is_range and parsed.start == Selector("seq", seq=3) and parsed.end.kind == "head"
    assert parsed.step == 1
    stepped = Selector.parse(f"2024-01-01T00:00:00Z..{COMMIT}/5")
    assert stepped.step == 5 and stepped.start.kind == "time" and stepped.end.id == COMMIT
    assert str(stepped) == f"2024-01-01T00:00:00Z..{COMMIT}/5"
    assert str(Selector.parse("#0..#9")) == "#0..#9"


@pytest.mark.parametrize(
    "text",
    ["", "HEAD", "#", "#-1", "abc", "#1..", "..#2", "#1..#2..#3", "#1..#2/0", "#1..#2/x", "#1/2"],
)
def test_malformed_selectors_are_refused(text):
    with pytest.raises(InvalidInput) as caught:
        Selector.parse(text)
    assert caught.value.code == "store.bad_selector"


def test_a_time_selector_without_an_offset_is_refused_as_naive():
    with pytest.raises(InvalidInput) as caught:
        Selector.parse("2024-05-01T12:00:00")
    assert caught.value.code == "store.naive_datetime"


# -- provenance and commit metadata --------------------------------------------


def test_provenance_is_normalised_to_utc_and_round_trips_as_json():
    eastern = timezone(timedelta(hours=-5))
    given = Provenance("scada", "observed", datetime(2024, 1, 1, 7, tzinfo=eastern), 0.5)
    assert given.kind is FactKind.observed and given.t_valid == datetime(2024, 1, 1, 12, tzinfo=UTC)
    document = given.to_json()
    assert document == {"s": "scada", "k": "observed", "c": 0.5, "t": "2024-01-01T12:00:00Z"}
    assert Provenance.from_json(document) == given
    assert Provenance("m", FactKind.inferred, "2024-01-01T12:00:00+00:00").t_valid == given.t_valid


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"source": "", "kind": "observed", "t_valid": datetime(2024, 1, 1, tzinfo=UTC)},
            "store.invalid_provenance",
        ),
        (
            {"source": "s", "kind": "guessed", "t_valid": datetime(2024, 1, 1, tzinfo=UTC)},
            "store.invalid_provenance",
        ),
        (
            {"source": "s", "kind": "observed", "t_valid": datetime(2024, 1, 1)},
            "store.naive_datetime",
        ),
        (
            {"source": "s", "kind": "observed", "t_valid": "2024-01-01T00:00:00"},
            "store.naive_datetime",
        ),
        ({"source": "s", "kind": "observed", "t_valid": "yesterday"}, "store.bad_time"),
        (
            {
                "source": "s",
                "kind": "observed",
                "t_valid": datetime(2024, 1, 1, tzinfo=UTC),
                "confidence": 1.5,
            },
            "store.invalid_provenance",
        ),
        (
            {
                "source": "s",
                "kind": "observed",
                "t_valid": datetime(2024, 1, 1, tzinfo=UTC),
                "confidence": float("nan"),
            },
            "store.invalid_provenance",
        ),
    ],
)
def test_malformed_provenance_is_refused(kwargs, code):
    with pytest.raises(InvalidInput) as caught:
        Provenance(**kwargs)
    assert caught.value.code == code


def test_commit_meta_normalises_time_to_utc_milliseconds():
    made = CommitMeta("sync", "loader", datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=UTC))
    assert made.cause is Cause.sync
    assert made.t == datetime(2024, 1, 1, 12, 0, 0, 123000, tzinfo=UTC)
    with pytest.raises(InvalidInput) as caught:
        CommitMeta("sync", "loader", datetime(2024, 1, 1))
    assert caught.value.code == "store.naive_datetime"
    with pytest.raises(InvalidInput) as caught:
        CommitMeta("teleport", "loader", datetime(2024, 1, 1, tzinfo=UTC))
    assert caught.value.code == "store.invalid_meta"


def test_commit_meta_extra_is_copied_and_must_be_json():
    source = {"backfill": True, "window": {"from": "2024-01-01", "n": [1, 2]}}
    made = CommitMeta("sync", "loader", datetime(2024, 1, 1, tzinfo=UTC), extra=source)
    source["window"]["n"].append(3)
    assert made.extra == {"backfill": True, "window": {"from": "2024-01-01", "n": [1, 2]}}
    for bad in (
        {"when": datetime(2024, 1, 1, tzinfo=UTC)},
        {"x": {1, 2}},
        {1: "x"},
        {"x": float("inf")},
        ["x"],
    ):
        with pytest.raises(InvalidInput) as caught:
            CommitMeta("sync", "loader", datetime(2024, 1, 1, tzinfo=UTC), extra=bad)
        assert caught.value.code == "store.invalid_meta"


# -- settings ------------------------------------------------------------------

_ENV = {
    "DTA_STORE_HOST": "127.0.0.1",
    "DTA_STORE_PORT": "13307",
    "DTA_STORE_USER": "root",
    "DTA_STORE_PASSWORD": "not-a-real-secret",
    "DTA_STORE_DATABASE": "twin",
}


def test_settings_come_from_the_environment_and_hide_the_password():
    settings = StoreSettings.from_env(_ENV)
    assert (settings.host, settings.port, settings.database) == ("127.0.0.1", 13307, "twin")
    assert "not-a-real-secret" not in repr(settings)


def test_missing_settings_fail_naming_every_missing_variable():
    partial = {k: v for k, v in _ENV.items() if k not in ("DTA_STORE_PASSWORD", "DTA_STORE_HOST")}
    with pytest.raises(ConfigError) as caught:
        StoreSettings.from_env(partial)
    assert set(caught.value.detail["missing"]) == {"DTA_STORE_PASSWORD", "DTA_STORE_HOST"}
    with pytest.raises(ConfigError):
        StoreSettings.from_env({**_ENV, "DTA_STORE_PORT": "mysql"})


def test_a_database_name_that_is_not_a_plain_identifier_is_refused():
    with pytest.raises(InvalidInput) as caught:
        StoreSettings.from_env({**_ENV, "DTA_STORE_DATABASE": "twin`; DROP DATABASE x"})
    assert caught.value.code == "store.invalid_settings"


# -- generated tables ----------------------------------------------------------


def test_type_table_holds_keys_references_ranges_and_checks():
    ontology = water_ontology()
    statement = ddl.type_table(ontology, ontology.type("Tank"))
    assert statement.startswith("CREATE TABLE `tank` (")
    for fragment in (
        "`uid` VARCHAR(190) NOT NULL",
        "`_type` VARCHAR(64) NOT NULL DEFAULT 'Tank'",
        "`name` VARCHAR(255) NOT NULL",
        "`level` DOUBLE",
        "`status` ENUM('ok', 'low', 'out of service')",
        "`curve` JSON",
        "`geometry` JSON",
        "`commissioned` DATE",
        "`sampled_at` DATETIME(6)",
        "`sealed` BOOLEAN",
        "`fill_count` BIGINT",
        "`notes` LONGTEXT",
        "`_prov` JSON NOT NULL",
        "FOREIGN KEY (`uid`, `_type`) REFERENCES `_object` (`uid`, `type`)",
        "CHECK (`_type` = 'Tank')",
        "CONSTRAINT `tank__min_capacity` CHECK (`capacity` >= 0.0)",
        "CONSTRAINT `tank__chk_level_within_capacity` CHECK (level is null or level <= capacity)",
    ):
        assert fragment in statement, fragment
    pipe = ddl.type_table(ontology, ontology.type("Pipe"))
    assert (
        "CONSTRAINT `pipe__ref_start_node` FOREIGN KEY (`start_node`) REFERENCES `_object` (`uid`)"
        in pipe
    )
    assert ontology.type("TwinSyncRun").table == "dta_sync_run"
    assert len(ddl.install_statements(ontology)) == 3 + len(ontology.types)


@pytest.mark.parametrize(
    "expression",
    [
        "level >= 0; DROP TABLE tank",
        "level >= 0 -- trailing",
        "level >= 0 # trailing",
        "level >= 0 /* hidden */",
        "`level` >= 0",
        "level >= 0) , ADD COLUMN x INT, CHECK (1 = 1",
        "(level >= 0",
        "name <> 'unterminated",
        "",
    ],
)
def test_check_expressions_that_are_not_one_plain_expression_are_refused(expression):
    assert ddl.check_expression_problem(expression) is not None
    tank = ObjectType("Tank", (Field("level", Kind.float),), checks=(("c", expression),))
    with pytest.raises(InvalidOntology) as caught:
        ddl.check_ontology(Ontology(types=(tank,)))
    assert caught.value.code == "store.invalid_ontology"


def test_ordinary_check_expressions_are_accepted():
    for expression in (
        "level >= 0",
        "(a is null) or (b in ('x,y', 'it''s'))",
        "a <= b and c <> ')'",
    ):
        assert ddl.check_expression_problem(expression) is None, expression
    # A semicolon is refused even inside a string: the rule stays simple enough to trust.
    assert ddl.check_expression_problem("b = 'x;y'") is not None


def _only(*fields: Field, name: str = "Thing", **kwargs) -> Ontology:
    return Ontology(types=(ObjectType(name, tuple(fields), **kwargs),), **{})


@pytest.mark.parametrize(
    ("ontology", "needle"),
    [
        (_only(Field("uid", Kind.string)), "reserved"),
        (_only(Field("commit", Kind.string)), "reserved"),
        (_only(Field("Bad-Name", Kind.string)), "does not match"),
        (_only(Field("x", Kind.string), name="bad name"), "does not match"),
        (_only(Field("x", Kind.ref, target="Missing")), "unknown type"),
        (_only(Field("x", Kind.ref, target="Thing", multivalued=True)), "link type"),
        (_only(Field("x", Kind.enum, target="Missing")), "unknown enum"),
        (_only(Field("x", Kind.string, minimum=1)), "not a number"),
        (_only(Field("x", Kind.float, minimum=float("inf"))), "not finite"),
        (_only(Field("x", Kind.string), Field("x", Kind.string)), "defined twice"),
        (_only(Field("x", Kind.string), name="TwinOntology", meta=True), "reserved"),
        (_only(Field("x", Kind.string), link=("x", "y")), "not a reference"),
        (
            Ontology(
                types=(ObjectType("A", (Field("e", Kind.enum, target="E"),)),),
                enums={"E": ("ok", "back\\slash")},
            ),
            "backslash",
        ),
        (
            Ontology(types=(ObjectType("HTTPSource", ()), ObjectType("HttpSource", ()))),
            "shares the table name",
        ),
    ],
)
def test_an_ontology_that_cannot_be_stored_safely_is_refused_before_any_connection(
    ontology, needle
):
    with pytest.raises(InvalidOntology) as caught:
        Store(StoreSettings.from_env(_ENV), ontology)
    assert any(needle in problem for problem in caught.value.detail["problems"]), (
        caught.value.detail
    )


def test_every_problem_of_an_ontology_is_listed_together():
    broken = Ontology(
        types=(
            ObjectType(
                "Thing",
                (Field("uid", Kind.string), Field("x", Kind.ref, target="Nope")),
                checks=(("c", "1; 2"),),
            ),
        )
    )
    assert len(ddl.ontology_problems(broken)) == 3


# -- values --------------------------------------------------------------------


def test_values_are_bound_as_utc_and_json():
    tank = water_ontology().type("Tank")
    plus_two = timezone(timedelta(hours=2))
    assert encode(tank.field("sampled_at"), datetime(2024, 1, 1, 12, tzinfo=plus_two)) == datetime(
        2024, 1, 1, 10
    )
    assert encode(tank.field("sampled_at"), "2024-01-01T12:00:00+02:00") == datetime(2024, 1, 1, 10)
    assert encode(tank.field("curve"), [1, 2.5]) == "[1.0,2.5]"
    assert (
        encode(tank.field("inspections"), [datetime(2024, 1, 1, tzinfo=UTC)])
        == '["2024-01-01T00:00:00Z"]'
    )
    assert encode(tank.field("geometry"), {"b": 1, "a": [1]}) == '{"a":[1],"b":1}'
    assert encode(tank.field("geometry"), '{"b": 1}') == '{"b":1}'
    assert encode(tank.field("level"), None) is None
    for field_name, bad in (
        ("sampled_at", datetime(2024, 1, 1)),
        ("level", "3"),
        ("level", float("nan")),
        ("fill_count", True),
        ("fill_count", 2.0),
        ("sealed", 1),
        ("curve", 1.0),
        ("curve", ["a"]),
        ("geometry", 5),
        ("geometry", "not json"),
        ("commissioned", datetime(2024, 1, 1, tzinfo=UTC)),
        ("name", 7),
    ):
        with pytest.raises(ValueError):
            encode(tank.field(field_name), bad)


def test_descriptor_validator_reports_each_kind_of_problem():
    validator = DescriptorValidator(water_ontology())
    row = {
        "uid": "node:t1",
        "name": "x" * 300,
        "capacity": -1.0,
        "status": "exploded",
        "curve": [1.0, "two"],
        "code": "bad",
        "fill_count": -3,
    }
    found = {(v.property, v.rule) for v in validator.validate("Tank", [row])}
    assert found == {
        ("name", "length"),
        ("capacity", "range"),
        ("status", "enum"),
        ("curve", "type"),
        ("code", "pattern"),
        ("fill_count", "range"),
    }
    # A partial row may leave required properties out, but may not null them.
    assert validator.validate("Tank", [{"uid": "node:t1", "level": 1.0}], partial=True) == []
    nulled = validator.validate("Tank", [{"uid": "node:t1", "name": None}], partial=True)
    assert [(v.property, v.rule) for v in nulled] == [("name", "required")]
    missing = validator.validate("Tank", [{"uid": "node:t1", "level": 1.0}])
    assert {(v.property, v.rule) for v in missing} == {
        ("name", "required"),
        ("capacity", "required"),
    }


def test_settings_are_frozen():
    settings = StoreSettings.from_env(_ENV)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 1  # type: ignore[misc]
