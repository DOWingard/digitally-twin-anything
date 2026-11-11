"""Tables generated from the ontology descriptors.

Every identifier placed in SQL comes from a descriptor name that has been
checked against the naming patterns, and is backquoted. The only free text
taken from the ontology is a check expression, which is refused unless it is
a single self-contained expression.
"""

from __future__ import annotations

import hashlib
import math

from ..schema import (
    CLASS_NAME,
    FIELD_NAME,
    UID_FIELD,
    UID_MAX_LENGTH,
    Field,
    Kind,
    ObjectType,
    Ontology,
)
from .codec import STRING_LENGTH, is_list
from .errors import InvalidOntology

OBJECT_TABLE = "_object"
ONTOLOGY_TABLE = "dta_ontology"
META_TABLE = "dta_meta"
# Tables with this prefix are operational and never part of a version.
SCRATCH_TABLE_PREFIX = "dta_tmp_"
FORMAT_VERSION = 1

_IDENTIFIER_LENGTH = 64
# The history tables are named `dolt_diff_<table>` with columns `from_<column>`.
_TABLE_LENGTH = _IDENTIFIER_LENGTH - len("dolt_diff_")
_COLUMN_LENGTH = _IDENTIFIER_LENGTH - len("from_")
# `to_commit` and `to_commit_date` are columns of every history table.
_RESERVED_PROPERTIES = {UID_FIELD, "commit", "commit_date"}
_CHECK_LENGTH = 1000
_FORBIDDEN_IN_CHECK = (";", "`", "--", "#", "/*", "*/")

RULE_UID_TYPE = "uid_type"
RULE_REFERENCE = "reference"
RULE_RANGE = "range"
RULE_CHECK = "check"


_FIXED_IDENTIFIERS = {OBJECT_TABLE, "_type", "_prov"}


def quote(name: str) -> str:
    """A checked identifier between backquotes."""
    if not (FIELD_NAME.match(name) or CLASS_NAME.match(name) or name in _FIXED_IDENTIFIERS):
        raise InvalidOntology(f"not a safe identifier: {name!r}")
    return f"`{name}`"


def literal(text: str) -> str:
    """A string literal for DDL, where parameters cannot be bound."""
    return "'" + text.replace("'", "''") + "'"


def constraint_name(table: str, kind: str, name: str = "") -> str:
    full = f"{table}__{kind}" + (f"_{name}" if name else "")
    if len(full) <= _IDENTIFIER_LENGTH:
        return full
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
    return f"{full[: _IDENTIFIER_LENGTH - 17]}_{digest}"


# -- checking ------------------------------------------------------------------


def check_expression_problem(expression: object) -> str | None:
    """Why a check expression cannot be placed in a table definition, or None."""
    if not isinstance(expression, str) or not expression.strip():
        return "is empty"
    if len(expression) > _CHECK_LENGTH:
        return f"is longer than {_CHECK_LENGTH} characters"
    for token in _FORBIDDEN_IN_CHECK:
        if token in expression:
            return f"contains {token!r}"
    if any(ord(character) < 32 for character in expression):
        return "contains a control character"
    depth = 0
    quote_open: str | None = None
    skip = False
    for character in expression:
        if skip:
            skip = False
        elif quote_open:
            if character == "\\":
                skip = True
            elif character == quote_open:
                quote_open = None
        elif character in ("'", '"'):
            quote_open = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return "closes a parenthesis it did not open"
    if quote_open or skip:
        return "has an unterminated string"
    if depth:
        return "has an unclosed parenthesis"
    return None


def _enum_problem(values: tuple[str, ...]) -> str | None:
    if not values:
        return "has no values"
    if len(set(values)) != len(values):
        return "repeats a value"
    for value in values:
        if not isinstance(value, str) or not value:
            return f"has a value that is not text: {value!r}"
        if len(value) > STRING_LENGTH:
            return f"has a value longer than {STRING_LENGTH} characters"
        if value != value.strip():
            return f"has a value with surrounding whitespace: {value!r}"
        if "\\" in value or any(ord(character) < 32 for character in value):
            return f"has a value with a backslash or control character: {value!r}"
    return None


def _field_problems(ontology: Ontology, object_type: ObjectType, field: Field) -> list[str]:
    where = f"{object_type.name}.{field.name}"
    problems = []
    if not isinstance(field.name, str) or not FIELD_NAME.match(field.name):
        return [f"{where}: property name does not match {FIELD_NAME.pattern}"]
    if field.name in _RESERVED_PROPERTIES:
        problems.append(f"{where}: property name is reserved")
    if len(field.name) > _COLUMN_LENGTH:
        problems.append(f"{where}: property name is longer than {_COLUMN_LENGTH} characters")
    if field.kind == Kind.ref:
        if field.multivalued:
            problems.append(f"{where}: a multivalued reference must be modelled as a link type")
        if field.target is None or not (
            ontology.has_type(field.target) or field.target in ontology.abstract
        ):
            problems.append(f"{where}: references unknown type {field.target!r}")
    if field.kind == Kind.enum:
        if field.target is None or field.target not in ontology.enums:
            problems.append(f"{where}: uses unknown enum {field.target!r}")
    numeric = field.kind in (Kind.integer, Kind.float)
    for label, bound in (("minimum", field.minimum), ("maximum", field.maximum)):
        if bound is None:
            continue
        if not numeric:
            problems.append(f"{where}: {label} is set on a property that is not a number")
        elif isinstance(bound, bool) or not isinstance(bound, int | float):
            problems.append(f"{where}: {label} is not a number")
        elif not math.isfinite(bound):
            problems.append(f"{where}: {label} is not finite")
    return problems


def ontology_problems(ontology: Ontology) -> list[str]:
    """Everything that stops the ontology from being turned into tables."""
    problems: list[str] = []
    tables: dict[str, str] = {}
    names: set[str] = set()
    for name, values in ontology.enums.items():
        problem = _enum_problem(tuple(values))
        if problem:
            problems.append(f"enum {name}: {problem}")
    for object_type in ontology.types:
        name = object_type.name
        if not isinstance(name, str) or not CLASS_NAME.match(name):
            problems.append(f"{name!r}: type name does not match {CLASS_NAME.pattern}")
            continue
        if name in names:
            problems.append(f"{name}: type is defined twice")
        names.add(name)
        if len(name) > _IDENTIFIER_LENGTH:
            problems.append(f"{name}: type name is longer than {_IDENTIFIER_LENGTH} characters")
        for ancestor in object_type.ancestors:
            if not isinstance(ancestor, str) or not CLASS_NAME.match(ancestor):
                problems.append(f"{name}: ancestor name {ancestor!r} is not a class name")
        table = object_type.table
        if not FIELD_NAME.match(table):
            problems.append(f"{name}: table name {table!r} is not a plain identifier")
            continue
        if len(table) > _TABLE_LENGTH:
            problems.append(f"{name}: table name is longer than {_TABLE_LENGTH} characters")
        if (
            table in (ONTOLOGY_TABLE, META_TABLE)
            or table.startswith(SCRATCH_TABLE_PREFIX)
            or table.startswith("dolt_")
        ):
            problems.append(f"{name}: table name {table} is reserved")
        if table in tables:
            problems.append(f"{name}: shares the table name {table} with {tables[table]}")
        tables[table] = name
        seen: set[str] = set()
        for field in object_type.fields:
            problems.extend(_field_problems(ontology, object_type, field))
            if field.name in seen:
                problems.append(f"{name}.{field.name}: property is defined twice")
            seen.add(field.name)
        if object_type.link is not None:
            for end in object_type.link:
                if not object_type.has_field(end) or object_type.field(end).kind != Kind.ref:
                    problems.append(f"{name}: link end {end!r} is not a reference property")
        check_names: set[str] = set()
        for check_name, expression in object_type.checks:
            if not isinstance(check_name, str) or not FIELD_NAME.match(check_name):
                problems.append(f"{name}: check name {check_name!r} is not a plain identifier")
                continue
            if check_name in check_names:
                problems.append(f"{name}: check {check_name} is defined twice")
            check_names.add(check_name)
            problem = check_expression_problem(expression)
            if problem:
                problems.append(f"{name}: check {check_name} {problem}")
    for abstract, members in ontology.abstract.items():
        if not isinstance(abstract, str) or not CLASS_NAME.match(abstract):
            problems.append(f"{abstract!r}: abstract type name is not a class name")
        for member in members:
            if member not in names:
                problems.append(f"{abstract}: lists unknown concrete type {member!r}")
    return problems


def check_ontology(ontology: Ontology) -> None:
    problems = ontology_problems(ontology)
    if problems:
        suffix = "" if len(problems) == 1 else f" (and {len(problems) - 1} more)"
        raise InvalidOntology(
            f"ontology cannot be stored: {problems[0]}{suffix}", detail={"problems": problems}
        )


# -- generating ----------------------------------------------------------------


def column_type(ontology: Ontology, field: Field) -> str:
    if field.kind == Kind.json or is_list(field):
        return "JSON"
    if field.kind == Kind.string:
        return "TEXT" if field.long_text else f"VARCHAR({STRING_LENGTH})"
    if field.kind == Kind.integer:
        return "BIGINT"
    if field.kind == Kind.float:
        return "DOUBLE"
    if field.kind == Kind.boolean:
        return "BOOLEAN"
    if field.kind == Kind.datetime:
        return "DATETIME(6)"
    if field.kind == Kind.date:
        return "DATE"
    if field.kind == Kind.ref:
        return f"VARCHAR({UID_MAX_LENGTH})"
    if field.kind == Kind.enum:
        values = ontology.enums[field.target or ""]
        return "ENUM(" + ", ".join(literal(value) for value in values) + ")"
    raise InvalidOntology(f"property kind {field.kind!r} has no column type")


def column_definition(ontology: Ontology, field: Field) -> str:
    null = " NOT NULL" if field.required else ""
    return f"{quote(field.name)} {column_type(ontology, field)}{null}"


def field_constraints(object_type: ObjectType, field: Field) -> list[tuple[str, str, str]]:
    """(constraint name, clause, rule) for the constraints one property brings."""
    table = object_type.table
    column = quote(field.name)
    found = []
    if field.kind == Kind.ref:
        found.append(
            (
                constraint_name(table, "ref", field.name),
                f"FOREIGN KEY ({column}) REFERENCES `{OBJECT_TABLE}` (`{UID_FIELD}`)",
                RULE_REFERENCE,
            )
        )
    if field.kind in (Kind.integer, Kind.float) and not field.multivalued:
        if field.minimum is not None:
            found.append(
                (
                    constraint_name(table, "min", field.name),
                    f"CHECK ({column} >= {float(field.minimum)!r})",
                    RULE_RANGE,
                )
            )
        if field.maximum is not None:
            found.append(
                (
                    constraint_name(table, "max", field.name),
                    f"CHECK ({column} <= {float(field.maximum)!r})",
                    RULE_RANGE,
                )
            )
    return found


def check_constraint(object_type: ObjectType, name: str, expression: str) -> tuple[str, str]:
    return constraint_name(object_type.table, "chk", name), f"CHECK ({expression})"


def constraint_rules(object_type: ObjectType) -> dict[str, tuple[str | None, str]]:
    """Constraint name -> (property, rule), to explain an error raised by the database."""
    table = object_type.table
    rules: dict[str, tuple[str | None, str]] = {
        constraint_name(table, "object"): (None, RULE_UID_TYPE),
        constraint_name(table, "type"): (None, RULE_UID_TYPE),
    }
    for field in object_type.fields:
        for name, _, rule in field_constraints(object_type, field):
            rules[name] = (field.name, rule)
    for check_name, expression in object_type.checks:
        rules[check_constraint(object_type, check_name, expression)[0]] = (None, RULE_CHECK)
    return rules


def type_table(ontology: Ontology, object_type: ObjectType) -> str:
    table = object_type.table
    lines = [
        f"`{UID_FIELD}` VARCHAR({UID_MAX_LENGTH}) NOT NULL",
        f"`_type` VARCHAR({_IDENTIFIER_LENGTH}) NOT NULL DEFAULT {literal(object_type.name)}",
    ]
    lines.extend(column_definition(ontology, field) for field in object_type.fields)
    lines.append("`_prov` JSON NOT NULL")
    lines.append(f"PRIMARY KEY (`{UID_FIELD}`)")
    # A uid is registered once with one type, so no two tables can hold the same uid.
    lines.append(
        f"CONSTRAINT `{constraint_name(table, 'object')}` FOREIGN KEY (`{UID_FIELD}`, `_type`) "
        f"REFERENCES `{OBJECT_TABLE}` (`{UID_FIELD}`, `type`)"
    )
    lines.append(
        f"CONSTRAINT `{constraint_name(table, 'type')}` "
        f"CHECK (`_type` = {literal(object_type.name)})"
    )
    for field in object_type.fields:
        for name, clause, _ in field_constraints(object_type, field):
            lines.append(f"CONSTRAINT `{name}` {clause}")
    for check_name, expression in object_type.checks:
        name, clause = check_constraint(object_type, check_name, expression)
        lines.append(f"CONSTRAINT `{name}` {clause}")
    return f"CREATE TABLE {quote(table)} (\n  " + ",\n  ".join(lines) + "\n)"


def system_tables() -> list[str]:
    return [
        f"CREATE TABLE `{OBJECT_TABLE}` (\n"
        f"  `{UID_FIELD}` VARCHAR({UID_MAX_LENGTH}) NOT NULL,\n"
        f"  `type` VARCHAR({_IDENTIFIER_LENGTH}) NOT NULL,\n"
        f"  PRIMARY KEY (`{UID_FIELD}`),\n"
        f"  UNIQUE KEY `uid_type` (`{UID_FIELD}`, `type`)\n)",
        f"CREATE TABLE `{ONTOLOGY_TABLE}` (\n"
        "  `id` TINYINT NOT NULL,\n"
        "  `hash` CHAR(64) NOT NULL,\n"
        "  `descriptor` LONGTEXT NOT NULL,\n"
        "  `source` LONGTEXT NOT NULL,\n"
        "  PRIMARY KEY (`id`),\n"
        f"  CONSTRAINT `{ONTOLOGY_TABLE}__one` CHECK (`id` = 1)\n)",
        f"CREATE TABLE `{META_TABLE}` (\n"
        "  `id` TINYINT NOT NULL,\n"
        "  `format` INT NOT NULL,\n"
        f"  `twin_id` VARCHAR({UID_MAX_LENGTH}) NOT NULL,\n"
        "  PRIMARY KEY (`id`),\n"
        f"  CONSTRAINT `{META_TABLE}__one` CHECK (`id` = 1)\n)",
    ]


def install_statements(ontology: Ontology) -> list[str]:
    """Every table of a new twin. References all point at the object registry,
    so the order of creation does not matter and reference cycles are fine."""
    check_ontology(ontology)
    return system_tables() + [type_table(ontology, t) for t in ontology.types]
