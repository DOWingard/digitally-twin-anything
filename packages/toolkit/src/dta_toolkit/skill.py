"""The operator skill of one twin: what an agent reads to work with it.

The skill is rendered from the manifest alone, so it changes exactly when the
manifest does, and it records the manifest hash it was rendered from. It tells
the agent how to reach the twin, what the twin promises, and how to ask; it
holds no copy of the twin's types, which the agent reads from the twin itself,
and no analysis methods.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from typing import Any

import yaml
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from jinja2 import Environment, StrictUndefined

log = Log("skill")

SKILL_FORMAT = 1
# Machine-readable record of what the skill was rendered from.
MARKER_FILE = "twin.json"
SKILL_FILE = "SKILL.md"
_TEMPLATES = "templates/skill"
_FILES = (
    SKILL_FILE,
    "reference/api.md",
    "reference/recipes.md",
    "reference/errors.md",
)
FIDELITY_FILE = "reference/fidelity.md"
REMODEL_FILE = "reference/remodel.md"
# The listing an agent sees is cut at 1,536 characters; stay well inside it.
_DESCRIPTION_MAX = 900
_PURPOSE_MAX = 300
_QUESTION_MAX = 120


class SkillError(DtaError):
    """The manifest does not hold what a skill needs."""

    code = "skill.incomplete"


@dataclass(frozen=True)
class RenderedSkill:
    name: str
    files: dict[str, str]  # path relative to the skill's directory -> text
    manifest_hash: str


def skill_name(twin_id: str) -> str:
    """Directory and invocation name of a twin's skill: lower case, digits and hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", twin_id.lower()).strip("-")
    return f"twin-{slug}"


def _one_line(text: str | None, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _duration(value: timedelta | None) -> str | None:
    """`PT15M`-style text read aloud: 15 minutes."""
    if value is None:
        return None
    seconds = int(value.total_seconds())
    for size, unit in ((86_400, "day"), (3_600, "hour"), (60, "minute"), (1, "second")):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _tolerance(question: Any) -> str | None:
    tolerance = question.tolerance
    if tolerance is None or tolerance.value is None:
        return None
    metric = tolerance.metric.value if tolerance.metric is not None else "error"
    unit = f" {tolerance.unit}" if tolerance.unit else ""
    return f"{metric} ≤ {tolerance.value:g}{unit}"


def _method(function: Any) -> str | None:
    """How a question's answer is defined: the function's description and its settings."""
    if function is None or not function.description:
        return None
    text = _one_line(function.description, 300).rstrip(". ")
    settings = ", ".join(
        f"{key} = {value}" for key, value in sorted((function.params or {}).items())
    )
    return f"{text} ({settings})" if settings else text


def _number(value: Any) -> str:
    return f"{value:.4g}" if isinstance(value, int | float) else "–"


def _state(found: dict[str, Any] | None) -> dict[str, str] | None:
    """A question's state as the API reported it, as the skill shows it."""
    if found is None:
        return None
    reason = _one_line(found.get("reason"), 200).replace("|", "\\|")
    return {
        "status": str(found.get("status") or "unknown"),
        "basis": str(found.get("basis") or "–"),
        "measured_error": _number(found.get("measured_error")),
        "tolerance": _number(found.get("tolerance")),
        "reason": reason or "–",
    }


def _bounds(control: Any) -> str:
    """A control's bounds as one line: `0 ≤ value ≤ 10, step ≤ 2`."""
    bounds = control.bounds
    if bounds is None:
        return "-"
    parts = []
    low, high = bounds.minimum, bounds.maximum
    if low is not None and high is not None:
        parts.append(f"{low:g} ≤ value ≤ {high:g}")
    elif low is not None:
        parts.append(f"value ≥ {low:g}")
    elif high is not None:
        parts.append(f"value ≤ {high:g}")
    if bounds.allowed:
        parts.append("one of " + ", ".join(f"`{json.dumps(value)}`" for value in bounds.allowed))
    if bounds.max_step is not None:
        parts.append(f"a change of at most {bounds.max_step:g} at a time")
    return "; ".join(parts) or "-"


def _cell(text: str | None, limit: int = 200) -> str:
    return _one_line(text, limit).replace("|", "\\|") or "-"


def _acting(manifest: Manifest) -> dict[str, Any]:
    """What a live twin checks about itself and may do: empty for a static twin, whose
    skill offers none of it."""
    contract = manifest.contract
    none: dict[str, Any] = {
        "fidelity": None,
        "remodel": False,
        "controls": [],
        "actions": [],
        "loops": [],
        "objective": None,
        "executes": False,
        "notification": None,
    }
    if contract is None or contract.tier is None or contract.tier.value != "live":
        return none
    found = dict(none)
    fidelity = manifest.fidelity
    questions = contract.questions or {}
    if fidelity is not None and fidelity.interval is not None and fidelity.window is not None:
        correction = fidelity.correction
        scored = [
            {
                "id": qid,
                "horizon": _duration(question.horizon),
                "tolerance": _tolerance(question),
                "model": question.model,
            }
            for qid, question in sorted(questions.items())
            if question.kind is not None
            and question.kind.value == "predictive"
            and question.horizon is not None
            and question.model
        ]
        found["fidelity"] = {
            "interval": _duration(fidelity.interval),
            "window": _duration(fidelity.window),
            "recalibrate_attempts": correction.recalibrate_attempts,
            "reselect": correction.reselect,
            "agent_remodel": correction.agent_remodel,
            "retry_budget": correction.retry_budget,
            "remodel_timeout": _duration(correction.remodel_timeout),
            "scored": scored,
            "stale": any(question.staleness is not None for question in questions.values()),
        }
        found["remodel"] = bool(scored) and correction.agent_remodel
    addon = bool(contract.addons.executors)
    actions = manifest.actions or {}
    controls = contract.controls or {}
    found["controls"] = [
        {
            "id": cid,
            "description": _cell(control.description),
            "target": control.target or "-",
            "bounds": _bounds(control),
            "autonomy": control.autonomy.value if control.autonomy is not None else "-",
            "action": control.action,
        }
        for cid, control in sorted(controls.items())
    ]
    found["actions"] = [
        {
            "id": aid,
            "description": _cell(action.description),
            "control": action.control,
            "object_type": action.object_type or "-",
            "binding": action.binding or "-",
            "parameters": ", ".join(
                f"`{name}`" + (f" ({parameter.type})" if parameter.type else "")
                for name, parameter in sorted((action.parameters or {}).items())
            )
            or "-",
            # Only an executed action that sets a control is checked for its effect.
            "settle": (
                (_duration(action.settle) or "twice the sync interval")
                if action.binding == "executor" and action.control
                else "-"
            ),
        }
        for aid, action in sorted(actions.items())
    ]
    found["executes"] = addon and any(
        action.binding == "executor"
        and action.control in controls
        and controls[action.control].autonomy is not None
        and controls[action.control].autonomy.value != "advisory"
        for action in actions.values()
    )
    found["loops"] = [
        {
            "id": lid,
            "description": _cell(loop.description),
            "controls": ", ".join(f"`{cid}`" for cid in loop.controls or []) or "-",
            "policy": loop.policy or "-",
            "interval": _duration(loop.interval) or "-",
        }
        for lid, loop in sorted((manifest.control_loops or {}).items())
    ]
    objective = contract.objective
    if objective is not None and objective.description:
        found["objective"] = {
            "description": _one_line(objective.description, 300).rstrip(". "),
            "direction": objective.direction,
            "kpi": objective.kpi,
        }
    notification = contract.notification
    found["notification"] = (
        notification.kind if notification is not None and notification.kind else "log"
    )
    return found


def _context(
    manifest: Manifest, manifest_hash: str, states: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    twin, contract = manifest.twin, manifest.contract
    if twin is None or not twin.id:
        raise SkillError("the manifest names no twin id")
    if contract is None or not contract.purpose or contract.tier is None:
        raise SkillError("the manifest's contract has no purpose or tier")
    functions = manifest.functions or {}
    by_id = {found.get("id"): found for found in states or []}
    questions = [
        {
            "id": qid,
            "state": _state(by_id.get(qid)) if states is not None else None,
            "method": _method(functions.get(question.function) if question.function else None),
            # A vertical bar would end the table cell the text is shown in.
            "text": _one_line(question.text, 400).replace("|", "\\|"),
            "kind": question.kind.value if question.kind is not None else None,
            "tolerance": _tolerance(question),
            "staleness": _duration(question.staleness),
            "targets": list(question.targets or []),
        }
        for qid, question in sorted(
            (contract.questions or {}).items(),
            key=lambda item: (
                item[1].priority if item[1].priority is not None else 1_000_000,
                item[0],
            ),
        )
    ]
    interfaces = manifest.interfaces
    return {
        "name": skill_name(twin.id),
        "twin_id": twin.id,
        "twin_name": twin.name or twin.id,
        "purpose": " ".join(contract.purpose.split()).rstrip(". ") + ".",
        "tier": contract.tier.value,
        "live": contract.tier.value == "live",
        "interval": _duration(manifest.sync.interval if manifest.sync is not None else None),
        "questions": questions,
        "states_known": states is not None,
        "base_url_env": interfaces.base_url_env,
        "token_env": interfaces.token_env,
        "ui": interfaces.ui,
        "manifest_hash": manifest_hash,
        "short_hash": manifest_hash[:12],
        "sources": [
            {"id": source_id, "description": _one_line(source.description, 200)}
            for source_id, source in sorted((manifest.sources or {}).items())
        ],
        **_acting(manifest),
    }


def _description(context: dict[str, Any]) -> str:
    asked = "; ".join(
        _one_line(question["text"], _QUESTION_MAX).rstrip("?.! ")
        for question in context["questions"][:4]
    )
    parts = [
        f'Operate the digital twin "{context["twin_name"]}" through its HTTP API: '
        f"{_one_line(context['purpose'], _PURPOSE_MAX).rstrip('. ')}.",
        "Use it to read the twin's objects and links, follow connections, look at any past "
        "version, export tables for analysis or model training, and check the twin's health.",
    ]
    if context["live"]:
        parts.append(
            "The twin is live: use it also to check how current its data is, to see what "
            "its sync runs did, and to make it read its sources now."
        )
    if context["fidelity"]:
        parts.append(
            "It checks itself against the real system: read its fidelity state and incidents "
            "before trusting an answer."
        )
    if context["actions"]:
        parts.append("It can propose actions on the real system within agreed bounds.")
    if asked:
        parts.append(f"It answers: {asked}.")
    return _one_line(" ".join(parts), _DESCRIPTION_MAX)


def render_skill(
    manifest: Manifest,
    *,
    manifest_hash: str,
    questions: list[dict[str, Any]] | None = None,
) -> RenderedSkill:
    """The skill of the twin this manifest describes, as files to write side by side.

    `questions` are the questions' states as the API reported them when the skill was
    written; without them the skill says they were not known.
    """
    context = _context(manifest, manifest_hash, questions)
    environment = Environment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True, trim_blocks=True
    )
    root = resources.files("dta_toolkit").joinpath(_TEMPLATES)
    files: dict[str, str] = {}
    wanted = list(_FILES)
    if context["fidelity"]:
        wanted.append(FIDELITY_FILE)
    if context["remodel"]:
        wanted.append(REMODEL_FILE)
    for relative in wanted:
        template = root.joinpath(f"{relative}.j2").read_text(encoding="utf-8")
        files[relative] = environment.from_string(template).render(**context)
    # The front matter is written by a YAML writer, so no text of the manifest can break it.
    front = yaml.safe_dump(
        {"name": context["name"], "description": _description(context)},
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    )
    files[SKILL_FILE] = f"---\n{front}---\n\n{files[SKILL_FILE].lstrip()}"
    files[MARKER_FILE] = (
        json.dumps(
            {
                "format": SKILL_FORMAT,
                "skill": context["name"],
                "twin_id": context["twin_id"],
                "manifest_hash": manifest_hash,
            },
            indent=2,
        )
        + "\n"
    )
    log.info(
        "skill rendered",
        skill=context["name"],
        files=sorted(files),
        manifest_hash=manifest_hash,
        questions=len(context["questions"]),
    )
    return RenderedSkill(name=context["name"], files=files, manifest_hash=manifest_hash)
