"""Read-only, indexed access to a manifest, shared by the checks and gates."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..models import (
    ColumnProfile,
    DatasetProfile,
    Manifest,
    Mapping,
    Question,
    QuestionKind,
    Source,
    Tier,
)

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PROPERTY_REF = re.compile(r"^[A-Z][A-Za-z0-9]*\.[a-z][a-z0-9_]*$")
FORECAST_KINDS = frozenset({QuestionKind.predictive, QuestionKind.prescriptive})
INTERFACE_ENV_FIELDS = ("base_url_env", "token_env", "api_port_env", "ui_port_env")


def placeholders(text: str | None) -> list[str]:
    """Variable names referenced as `${NAME}` in a text."""
    return PLACEHOLDER.findall(text) if text else []


def is_property_ref(value: str) -> bool:
    return bool(PROPERTY_REF.match(value))


def split_ref(ref: str) -> tuple[str, str]:
    type_name, _, prop = ref.partition(".")
    return type_name, prop


def fmt_duration(value: timedelta) -> str:
    """Compact, deterministic rendering such as `1 d 6 h` or `30 s`."""
    total = value.total_seconds()
    if total == 0:
        return "0 s"
    sign = "-" if total < 0 else ""
    remaining = abs(total)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{int(count)} {unit}")
    if remaining:
        parts.append(f"{remaining:g} s")
    return sign + " ".join(parts)


def walk_strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string inside nested dicts and lists, with its dotted path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{path}.{index}")


@dataclass(frozen=True)
class MappedColumn:
    """One place a `Type.property` is read from."""

    source_id: str
    source: Source
    mapping_id: str
    mapping: Mapping
    column: str | None
    dataset: DatasetProfile | None  # None when the source is not profiled or lacks the dataset

    @property
    def column_profile(self) -> ColumnProfile | None:
        if self.dataset is None or self.column is None:
            return None
        return self.dataset.columns.get(self.column)


class ManifestView:
    """The manifest with empty sections defaulted and the lookups the checks share."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        contract = manifest.contract
        self.contract = contract
        self.questions: dict[str, Question] = dict(contract.questions or {}) if contract else {}
        self.controls = dict(contract.controls) if contract else {}
        self.functions = dict(manifest.functions or {})
        self.models = dict(manifest.models)
        self.actions = dict(manifest.actions)
        self.sources = dict(manifest.sources or {})
        self.loops = dict(manifest.control_loops)
        self.env = dict(manifest.env)

    @property
    def tier(self) -> Tier | None:
        return self.contract.tier if self.contract else None

    @property
    def live(self) -> bool:
        return self.tier == Tier.live

    @property
    def static(self) -> bool:
        return self.tier == Tier.static

    def mapped_columns(self, ref: str) -> list[MappedColumn]:
        """Every mapping that fills `Type.property`, in source and mapping id order."""
        type_name, prop = split_ref(ref)
        found = []
        for source_id, source in sorted(self.sources.items()):
            for mapping_id, mapping in sorted((source.mappings or {}).items()):
                if mapping.object_type != type_name or prop not in (mapping.properties or {}):
                    continue
                dataset = None
                if source.profile is not None and mapping.dataset is not None:
                    dataset = source.profile.datasets.get(mapping.dataset)
                found.append(
                    MappedColumn(
                        source_id=source_id,
                        source=source,
                        mapping_id=mapping_id,
                        mapping=mapping,
                        column=mapping.properties[prop].column,
                        dataset=dataset,
                    )
                )
        return found

    def well_formed_targets(self, question: Question) -> list[str]:
        return [ref for ref in question.targets or [] if is_property_ref(ref)]

    def dependencies(self, question: Question) -> list[str]:
        """Ids of the sources that map at least one of the question's targets."""
        found = {
            mapped.source_id
            for ref in self.well_formed_targets(question)
            for mapped in self.mapped_columns(ref)
        }
        return sorted(found)

    def env_uses(self) -> Iterator[tuple[str, str]]:
        """Every variable name the manifest refers to, with the path that refers to it."""
        for source_id, source in sorted(self.sources.items()):
            base = f"sources.{source_id}"
            for name in source.env:
                yield name, f"{base}.env"
            for name in placeholders(source.location):
                yield name, f"{base}.location"
        for action_id, action in sorted(self.actions.items()):
            executor = action.executor
            if executor is None:
                continue
            base = f"actions.{action_id}.executor"
            for name in executor.env:
                yield name, f"{base}.env"
            for name in placeholders(executor.url):
                yield name, f"{base}.url"
            for header, value in sorted(executor.headers.items()):
                for name in placeholders(value):
                    yield name, f"{base}.headers.{header}"
            for path, text in walk_strings(executor.body, f"{base}.body"):
                for name in placeholders(text):
                    yield name, path
        for function_id, function in sorted(self.functions.items()):
            for name in function.env:
                yield name, f"functions.{function_id}.env"
        notification = self.contract.notification if self.contract else None
        if notification is not None and notification.webhook_env:
            yield notification.webhook_env, "contract.notification.webhook_env"
        interfaces = self.manifest.interfaces
        for field in INTERFACE_ENV_FIELDS:
            yield getattr(interfaces, field), f"interfaces.{field}"
