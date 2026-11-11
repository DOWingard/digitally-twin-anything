"""Comparison of two ontologies and the statements that take a twin from one to the other.

Only changes that leave every stored row valid and readable as before are
carried out: a new type, a new optional property, a new enum value, and a
new check that the stored rows already satisfy. Any other difference is
listed as a reason for refusing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schema import Field, Kind, ObjectType, Ontology
from . import ddl

# Differences in these attributes change what a stored value means or which values are legal.
_FIELD_ATTRIBUTES = (
    "kind",
    "required",
    "multivalued",
    "target",
    "role",
    "unit",
    "minimum",
    "maximum",
    "pattern",
    "long_text",
)


@dataclass
class MigrationPlan:
    reasons: list[str] = field(default_factory=list)
    # Statements that cannot fail on valid data, in the order to run them.
    statements: list[str] = field(default_factory=list)
    # (description, statement) of each new check; it fails when stored rows break it.
    checks: list[tuple[str, str]] = field(default_factory=list)
    new_types: list[str] = field(default_factory=list)
    new_properties: list[str] = field(default_factory=list)
    widened_enums: list[str] = field(default_factory=list)


def _compare_field(type_name: str, old: Field, new: Field) -> list[str]:
    reasons = []
    for attribute in _FIELD_ATTRIBUTES:
        before, after = getattr(old, attribute), getattr(new, attribute)
        if before != after:
            reasons.append(
                f"property {type_name}.{old.name} changes {attribute} from {before!r} to {after!r}"
            )
    return reasons


def _compare_type(plan: MigrationPlan, old: ObjectType, new: ObjectType, target: Ontology) -> None:
    name = old.name
    for attribute in ("ancestors", "link", "meta", "event"):
        before, after = getattr(old, attribute), getattr(new, attribute)
        if before != after:
            plan.reasons.append(f"type {name} changes {attribute} from {before!r} to {after!r}")
    table = ddl.quote(new.table)
    for old_field in old.fields:
        if not new.has_field(old_field.name):
            plan.reasons.append(f"property {name}.{old_field.name} is removed")
            continue
        plan.reasons.extend(_compare_field(name, old_field, new.field(old_field.name)))
    for new_field in new.fields:
        if old.has_field(new_field.name):
            continue
        if new_field.required:
            plan.reasons.append(
                f"new property {name}.{new_field.name} is required; existing objects have no value"
            )
            continue
        plan.new_properties.append(f"{name}.{new_field.name}")
        plan.statements.append(
            f"ALTER TABLE {table} ADD COLUMN {ddl.column_definition(target, new_field)}"
        )
        for constraint, clause, _ in ddl.field_constraints(new, new_field):
            plan.statements.append(f"ALTER TABLE {table} ADD CONSTRAINT `{constraint}` {clause}")
    old_checks, new_checks = dict(old.checks), dict(new.checks)
    for check_name, expression in old_checks.items():
        if check_name not in new_checks:
            plan.reasons.append(f"check {name}.{check_name} is removed")
        elif new_checks[check_name] != expression:
            plan.reasons.append(f"check {name}.{check_name} changes its expression")
    for check_name, expression in new_checks.items():
        if check_name not in old_checks:
            constraint, clause = ddl.check_constraint(new, check_name, expression)
            plan.checks.append(
                (
                    f"check {name}.{check_name}",
                    f"ALTER TABLE {table} ADD CONSTRAINT `{constraint}` {clause}",
                )
            )


def plan_migration(old: Ontology, new: Ontology) -> MigrationPlan:
    """What has to run to move from `old` to `new`, and every reason it cannot."""
    plan = MigrationPlan()
    for name, values in old.enums.items():
        if name not in new.enums:
            plan.reasons.append(f"enum {name} is removed")
            continue
        lost = [value for value in values if value not in new.enums[name]]
        if lost:
            plan.reasons.append(f"enum {name} loses the values {lost}")
    for name, members in old.abstract.items():
        if name not in new.abstract:
            plan.reasons.append(f"abstract type {name} is removed")
            continue
        lost = [member for member in members if member not in new.abstract[name]]
        if lost:
            plan.reasons.append(f"abstract type {name} loses the descendants {lost}")
    new_types = {t.name: t for t in new.types}
    old_names = set()
    for old_type in old.types:
        old_names.add(old_type.name)
        new_type = new_types.get(old_type.name)
        if new_type is None:
            plan.reasons.append(f"type {old_type.name} is removed")
            continue
        _compare_type(plan, old_type, new_type, new)
        # A widened enum is applied to every column that already uses it.
        for new_field in new_type.fields:
            if (
                new_field.kind == Kind.enum
                and not new_field.multivalued
                and old_type.has_field(new_field.name)
                and old.enums.get(new_field.target or "") != new.enums.get(new_field.target or "")
            ):
                plan.widened_enums.append(f"{new_type.name}.{new_field.name}")
                plan.statements.append(
                    f"ALTER TABLE {ddl.quote(new_type.table)} "
                    f"MODIFY COLUMN {ddl.column_definition(new, new_field)}"
                )
    for new_type in new.types:
        if new_type.name not in old_names:
            plan.new_types.append(new_type.name)
            plan.statements.append(ddl.type_table(new, new_type))
    return plan
