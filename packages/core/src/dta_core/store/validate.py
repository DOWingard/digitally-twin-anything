"""Row checks made from the descriptors alone.

Used when the store is given no validator of its own. It covers what the
descriptors state directly: value types, required properties, enum
membership, numeric bounds, patterns and text length.
"""

from __future__ import annotations

import re
from typing import Any

from ..schema import UID_FIELD, Field, Kind, Ontology, Violation
from . import codec

RULE_TYPE = "type"
RULE_REQUIRED = "required"
RULE_ENUM = "enum"
RULE_RANGE = "range"
RULE_PATTERN = "pattern"
RULE_LENGTH = "length"


class DescriptorValidator:
    def __init__(self, ontology: Ontology) -> None:
        self._ontology = ontology
        self._patterns: dict[str, re.Pattern[str] | None] = {}

    def _pattern(self, source: str) -> re.Pattern[str] | None:
        if source not in self._patterns:
            try:
                self._patterns[source] = re.compile(source)
            except re.error:
                # A pattern this engine cannot read is left to the database and the caller.
                self._patterns[source] = None
        return self._patterns[source]

    def _value_problems(self, field: Field, value: Any) -> list[tuple[str, str]]:
        try:
            codec.encode(field, value)
        except ValueError as exc:
            return [(RULE_TYPE, str(exc))]
        if field.kind == Kind.json:
            return []
        items = value if field.multivalued else [value]
        problems: list[tuple[str, str]] = []
        for item in items:
            if item is None:
                continue
            if field.kind == Kind.enum:
                allowed = self._ontology.enums.get(field.target or "", ())
                if item not in allowed:
                    problems.append((RULE_ENUM, f"{item!r} is not one of {list(allowed)}"))
            if field.kind in (Kind.integer, Kind.float):
                if field.minimum is not None and item < field.minimum:
                    problems.append((RULE_RANGE, f"{item} is below the minimum {field.minimum}"))
                if field.maximum is not None and item > field.maximum:
                    problems.append((RULE_RANGE, f"{item} is above the maximum {field.maximum}"))
            if field.kind == Kind.string:
                if not field.long_text and len(item) > codec.STRING_LENGTH:
                    problems.append(
                        (RULE_LENGTH, f"text is longer than {codec.STRING_LENGTH} characters")
                    )
                if field.pattern is not None:
                    pattern = self._pattern(field.pattern)
                    if pattern is not None and not pattern.search(item):
                        problems.append((RULE_PATTERN, f"does not match {field.pattern}"))
        return problems

    def validate(
        self, type_name: str, rows: list[dict[str, Any]], partial: bool = False
    ) -> list[Violation]:
        object_type = self._ontology.type(type_name)
        violations = []
        for row in rows:
            uid = row.get(UID_FIELD)
            uid = uid if isinstance(uid, str) else None
            for field in object_type.fields:
                present = field.name in row
                value = row.get(field.name)
                if value is None:
                    # Leaving a property out of a partial row keeps what is stored.
                    if field.required and (present or not partial):
                        violations.append(
                            Violation(
                                type_name,
                                uid,
                                field.name,
                                RULE_REQUIRED,
                                f"{field.name} is required",
                            )
                        )
                    continue
                for rule, message in self._value_problems(field, value):
                    violations.append(
                        Violation(type_name, uid, field.name, rule, f"{field.name}: {message}")
                    )
        return violations
