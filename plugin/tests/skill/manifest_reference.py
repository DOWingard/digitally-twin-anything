"""Writes the skill's manifest reference from the tool's own schema and tables.

The reference lists every field of `manifest.yaml` with its type, whether it is
required, who owns it and what it means, then the built-in functions with their
parameters and the shipped model families. Generated rather than written so that
it cannot drift from the schema: the skill's tests compare the file with what
this module renders and fail when they differ.

Run from the repository root to rewrite it:

    uv run python plugin/tests/skill/manifest_reference.py
"""

from __future__ import annotations

import enum
import sys
import time
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import annotated_types
from dta_core.families import BASELINES, FAMILIES
from dta_core.functions import BUILTINS
from dta_core.log import Log
from dta_core.manifest.models import Manifest
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

log = Log("skill.manifest_reference")

ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT / "plugin" / "skills" / "build-twin" / "reference" / "manifest.md"
REGENERATE = "uv run python plugin/tests/skill/manifest_reference.py"

# The placeholder that stands for a key of each map of sections, by the map's field name.
KEY_NAMES = {
    "sources": "source",
    "datasets": "dataset",
    "columns": "column",
    "mappings": "mapping",
    "properties": "property",
    "questions": "question",
    "controls": "control",
    "constraints": "constraint",
    "models": "model",
    "candidates": "candidate",
    "functions": "function",
    "actions": "action",
    "parameters": "parameter",
    "control_loops": "loop",
    "env": "NAME",
}

# Fields the schema accepts and the build does not act on: the value is recorded in
# the manifest and shown in the summary only. The schema's descriptions say so too;
# the reference adds it to any description that does not. The skill's tests check that
# no code of the tool reads them, so this list fails loudly once one does.
RECORDED_ONLY = (
    "contract.cadence_preference",
    "contract.addons.sdk_python",
    "contract.addons.mcp",
    "contract.addons.llm_extraction",
    "contract.addons.readonly_sql",
    "contract.hosting",
)

# What the condition of a conditionally required field means, by its name in the schema.
CONDITIONS = {
    "live": "for a live twin",
    "live_series": "for a source of a live twin that has a mapping with a `valid_time`",
    "forecast": "for a predictive or prescriptive question",
    "objective": "when a question is prescriptive or the contract has controls",
    "webhook": "when `kind` is `webhook`",
    "executor": "when `binding` is `executor`",
}

# Sections the tool writes itself; their fields carry no ownership metadata.
WRITTEN_BY = {
    "profile": "`dta profile`",
    "derivation": "`dta sync derive --write`",
}

_DURATION = TypeAdapter(timedelta)


@dataclass(frozen=True)
class Row:
    path: str
    kind: str
    required: str
    owner: str
    default: str
    description: str


def _meta(info: FieldInfo) -> dict[str, Any] | None:
    extra = info.json_schema_extra
    if isinstance(extra, dict):
        found = extra.get("dta")
        if isinstance(found, dict):
            return found
    return None


def _plain(annotation: Any) -> Any:
    """The annotation without `None` in a union."""
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        rest = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0]
        return typing.Union[tuple(rest)]  # noqa: UP007 - rebuilt from parts at run time
    return annotation


def _is_section(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _map_value(annotation: Any) -> Any | None:
    """The value type of a `dict[str, X]`, or None for anything else."""
    if typing.get_origin(annotation) is dict:
        return typing.get_args(annotation)[1]
    return None


def _type_text(annotation: Any) -> str:
    annotation = _plain(annotation)
    origin = typing.get_origin(annotation)
    if annotation is Any:
        return "any value"
    if annotation is str:
        return "text"
    if annotation is bool:
        return "true or false"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is timedelta:
        return "duration (ISO 8601, such as `PT15M`)"
    if annotation is datetime:
        return "time (ISO 8601)"
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return "one of " + ", ".join(f"`{member.value}`" for member in annotation)
    if origin is typing.Literal:
        return "one of " + ", ".join(f"`{value}`" for value in typing.get_args(annotation))
    if origin is list:
        return "list of " + _type_text(typing.get_args(annotation)[0])
    if origin is dict:
        return "map of name to " + _type_text(typing.get_args(annotation)[1])
    if isinstance(annotation, types.UnionType) or origin is typing.Union:
        return " or ".join(_type_text(arg) for arg in typing.get_args(annotation))
    if _is_section(annotation):
        return "section"
    raise TypeError(f"no wording for the annotation {annotation!r}")


def _bare(value: Any) -> str:
    """A value as it is written in the manifest."""
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, timedelta):
        return str(_DURATION.dump_python(value, mode="json"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_bare(item) for item in value) + "]"
    return str(value)


def _value_text(value: Any) -> str:
    return f"`{_bare(value)}`"


def _limits(info: FieldInfo) -> str:
    parts = []
    for item in info.metadata:
        if isinstance(item, annotated_types.Ge):
            parts.append(f"at least {_bare(item.ge)}")
        elif isinstance(item, annotated_types.Gt):
            parts.append(f"above {_bare(item.gt)}")
        elif isinstance(item, annotated_types.Le):
            parts.append(f"at most {_bare(item.le)}")
        elif isinstance(item, annotated_types.Lt):
            parts.append(f"below {_bare(item.lt)}")
        elif getattr(item, "pattern", None):
            parts.append(f"matching `{item.pattern}`")
    return ", ".join(parts)


def _default(info: FieldInfo) -> str:
    if info.default_factory is not None:
        made = info.default_factory()  # type: ignore[call-arg]
        if isinstance(made, BaseModel) or made in ({}, []):
            return ""
        return _value_text(made)
    if info.default is PydanticUndefined or info.default is None:
        return ""
    return _value_text(info.default)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _rows(model: type[BaseModel], prefix: str, written_by: str | None) -> Iterator[Row]:
    for name, info in model.model_fields.items():
        if prefix == "" and name == "field_status":
            continue
        path = f"{prefix}{name}"
        meta = _meta(info)
        annotation = _plain(info.annotation)
        if meta is None:
            required = "no"
            owner = f"written by {written_by}" if written_by else "fixed"
        else:
            required = "no"
            if meta["required"]:
                when = meta["when"]
                required = f"yes, {CONDITIONS[when]}" if when else "yes"
            owner = meta["owner"]
        description = info.description or ""
        if name in WRITTEN_BY and written_by is None:
            description += f"; written by {WRITTEN_BY[name]}"
        if path in RECORDED_ONLY and "recorded only" not in description:
            description += "; recorded only: the build does not act on it"
        inner = written_by or WRITTEN_BY.get(name)
        limits = _limits(info)
        kind = _type_text(annotation)
        value = _map_value(annotation)
        if _is_section(value):
            key = KEY_NAMES.get(name, "key")
            kind = f"map of `<{key}>` to a section"
        if limits:
            kind = f"{kind}, {limits}"
        yield Row(path, kind, required, owner, _default(info), description)
        if _is_section(annotation):
            yield from _rows(annotation, f"{path}.", inner)
        elif _is_section(value):
            key = KEY_NAMES.get(name, "key")
            yield from _rows(value, f"{path}.<{key}>.", inner)


def _field_tables() -> list[str]:
    lines: list[str] = []
    for name, info in Manifest.model_fields.items():
        if name == "field_status":
            continue
        lines += ["", f"## {name}", ""]
        if info.description:
            lines += [f"{info.description}.", ""]
        lines += [
            "| Field | Type | Required | Owner | Default | Description |",
            "|---|---|---|---|---|---|",
        ]
        rows = [row for row in _rows(Manifest, "", None) if row.path.split(".")[0] == name]
        for row in rows:
            lines.append(
                f"| `{row.path}` | {_cell(row.kind)} | {row.required} | {_cell(row.owner)} "
                f"| {row.default} | {_cell(row.description)} |"
            )
    return lines


def _builtins() -> list[str]:
    lines = [
        "",
        "## Built-in functions",
        "",
        "The entries `functions.<function>.entry` may name when `kind` is `builtin`. A "
        "parameter is fixed in `functions.<function>.params` or given with each call; a "
        "required one must be given one way or the other.",
    ]
    for entry, builtin in BUILTINS.items():
        lines += ["", f"### {entry}", "", f"{builtin.description}.", ""]
        if not builtin.parameters:
            lines.append("It takes no parameter.")
            continue
        lines += [
            "| Parameter | Kind | Required | Default | Description |",
            "|---|---|---|---|---|",
        ]
        for parameter in builtin.parameters:
            kind = parameter.kind.value
            if parameter.choices:
                kind += ": " + ", ".join(f"`{choice}`" for choice in parameter.choices)
            if parameter.numeric:
                kind += ", numeric"
            default = "" if parameter.default is None else _value_text(parameter.default)
            lines.append(
                f"| `{parameter.name}` | {_cell(kind)} | {'yes' if parameter.required else 'no'} "
                f"| {default} | {_cell(parameter.description)} |"
            )
    return lines


def _families() -> list[str]:
    named = {family: name for name, family in BASELINES.items()}
    lines = [
        "",
        "## Model families",
        "",
        "What `models.<model>.candidates.<candidate>.family` may name, simplest first; among "
        "equally good candidates the simpler one is selected. A family marked as a baseline "
        "is what `models.<model>.acceptance.baseline` names.",
        "",
        "| Family | Baseline | Description |",
        "|---|---|---|",
    ]
    for name, family in FAMILIES.items():
        baseline = f"`{named[name]}`" if name in named else ""
        lines.append(f"| `{name}` | {baseline} | {_cell(family.description)} |")
    return lines


def render() -> str:
    """The reference as it must stand for the installed tool."""
    lines = [
        "# Manifest reference",
        "",
        "Every field of `manifest.yaml`, as the tool's schema defines it. A path holds a "
        "placeholder in angle brackets where you choose the key: `sources.<source>` is "
        "`sources.telemetry` for a source you named `telemetry`.",
        "",
        "- Required `yes`: the field is a gap until it has a value; `yes, when ...`: only "
        "under that condition; `no`: it may stay empty.",
        "- Owner `user`: only the user settles it; you propose and they confirm. Owner "
        "`agent`: you settle it. `written by ...`: the tool writes it; never write it by hand.",
        "- A required section that is missing is one gap; once it exists, each of its own "
        "required fields is.",
        "- `field_status` maps a field path to its status (`unknown`, `proposed`, `derived`, "
        "`confirmed`); the tool writes it.",
        "- Durations are ISO 8601: `PT30S`, `PT15M`, `PT1H`, `P1D`, `P7D`.",
    ]
    lines += _field_tables()
    lines += _builtins()
    lines += _families()
    return "\n".join(lines) + "\n"


def main() -> int:
    started = time.perf_counter()
    try:
        text = render()
        TARGET.write_text(text, encoding="utf-8")
    except Exception as exc:
        log.error("manifest reference not written", exc=exc, target=str(TARGET))
        raise
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    log.info("manifest reference written", target=str(TARGET), duration_ms=elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
