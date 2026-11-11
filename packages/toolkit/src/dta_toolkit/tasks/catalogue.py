"""Which tasks a manifest gives rise to at each stage, and what each may read and write.

The catalogue is a table. Each row names a kind of task, the stage it belongs
to, what there is one task per, and templates for its id, the manifest paths it
reads, the manifest keys it owns and the tasks it needs. A new kind is a new
row (and a selector if it iterates over something new); the batch computation
never looks at kinds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import ConnectorKind
from pydantic import BaseModel, ConfigDict, field_validator

from .errors import TaskError
from .paths import is_path, parse_pattern

log = Log("tasks.catalogue")

# Same shape as the manifest's own ids. Nothing that could act as a path
# separator, a parent reference or a wildcard can appear in one.
# Always applied with fullmatch: `$` would also accept a trailing newline.
_SUBJECT_ID = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_TASK_ID = re.compile(r"[a-z][a-z0-9_-]{0,199}")
_HOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}")


def valid_task_id(text: Any) -> bool:
    return isinstance(text, str) and _TASK_ID.fullmatch(text) is not None


def require_task_id(text: Any) -> str:
    """Reject anything that is not a plain task id before it gets near a file path."""
    if not valid_task_id(text):
        log.warn("malformed task id refused", given=str(text)[:80])
        raise TaskError(
            "a task id is lowercase letters, digits, '_' and '-', starting with a letter",
            code="tasks.id.invalid",
            detail={"given": str(text)[:80]},
        )
    return text


def require_holder(text: Any) -> str:
    if not isinstance(text, str) or _HOLDER.fullmatch(text) is None:
        log.warn("malformed holder name refused", given=str(text)[:80])
        raise TaskError(
            "a holder name is 1 to 64 letters, digits or . _ : @ -, "
            "starting with a letter or digit",
            code="tasks.holder.invalid",
            detail={"given": str(text)[:80]},
        )
    return text


class TaskSpec(BaseModel):
    """What one task is, independent of any attempt to carry it out."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    stage: str
    subject: dict[str, str] = {}
    reads: tuple[str, ...] = ()  # manifest paths
    owns: tuple[str, ...] = ()  # manifest key patterns; the only shared state it may change
    needs: tuple[str, ...] = ()  # ids of tasks whose merged result it depends on

    @field_validator("id")
    @classmethod
    def _id_is_plain(cls, value: str) -> str:
        if not valid_task_id(value):
            raise ValueError("not a task id")
        return value

    @field_validator("reads")
    @classmethod
    def _reads_are_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for text in value:
            if not is_path(text):
                raise ValueError(f"{text!r} is not a manifest path")
        return value

    @field_validator("owns")
    @classmethod
    def _owns_are_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for text in value:
            parse_pattern(text)
        return value


Binding = dict[str, str]


def _sources(manifest: Manifest) -> list[Binding]:
    return [{"s": key} for key in (manifest.sources or {})]


def _custom_sources(manifest: Manifest) -> list[Binding]:
    return [
        {"s": key}
        for key, source in (manifest.sources or {}).items()
        if source.connector == ConnectorKind.custom
    ]


def _questions(manifest: Manifest) -> list[Binding]:
    contract = manifest.contract
    return [{"q": key} for key in ((contract.questions if contract else None) or {})]


def _model_candidates(manifest: Manifest) -> list[Binding]:
    found = []
    for model_id, model in manifest.models.items():
        for candidate_id in model.candidates or {}:
            binding = {"m": model_id, "c": candidate_id}
            if model.question:
                binding["q"] = model.question
            found.append(binding)
    return found


def _registered_functions(manifest: Manifest) -> list[Binding]:
    return [
        {"f": key}
        for key, function in (manifest.functions or {}).items()
        if function.kind == "registered"
    ]


def _questions_with_model(manifest: Manifest) -> list[Binding]:
    contract = manifest.contract
    return [
        {"q": key, "m": question.model}
        for key, question in ((contract.questions if contract else None) or {}).items()
        if question.model and question.model in manifest.models
    ]


# What a kind can have one task per. Each selector returns the variables its
# templates may use.
SELECTORS: dict[str, Callable[[Manifest], list[Binding]]] = {
    "source": _sources,
    "custom_source": _custom_sources,
    "question": _questions,
    "model_candidate": _model_candidates,
    "registered_function": _registered_functions,
    "question_with_model": _questions_with_model,
}


@dataclass(frozen=True)
class Kind:
    name: str
    stage: str
    per: str  # key of SELECTORS
    id: str
    reads: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()
    # Kept only when a task with that id exists at the same stage.
    needs: tuple[str, ...] = ()


CATALOGUE: tuple[Kind, ...] = (
    Kind(
        name="profile",
        stage="3",
        per="source",
        id="profile-{s}",
        reads=("sources.{s}",),
        owns=(
            "sources.{s}.profile",
            "sources.{s}.refresh_period",
            "sources.{s}.rate_limit_period",
        ),
    ),
    Kind(
        name="mapping",
        stage="4",
        per="source",
        id="mapping-{s}",
        reads=("ontology", "sources.{s}"),
        owns=("sources.{s}.mappings",),
    ),
    Kind(
        name="question",
        stage="4",
        per="question",
        id="question-{q}",
        reads=("ontology", "contract.questions.{q}", "contract.controls", "contract.objective"),
        owns=(
            "contract.questions.{q}.targets",
            "contract.questions.{q}.horizon",
            "contract.questions.{q}.function",
            "contract.questions.{q}.model",
            "models.{q}",
            "functions.{q}",
            "functions.{q}-*",
        ),
    ),
    Kind(
        name="connector",
        stage="6",
        per="custom_source",
        id="connector-{s}",
        reads=("ontology", "sources.{s}"),
    ),
    Kind(
        name="stage",
        stage="6",
        per="source",
        id="stage-{s}",
        reads=("ontology", "sources.{s}"),
        needs=("connector-{s}",),
    ),
    Kind(
        name="model",
        stage="6",
        per="model_candidate",
        id="model-{m}-{c}",
        reads=("ontology", "models.{m}", "contract.questions.{q}"),
    ),
    Kind(
        name="function",
        stage="6",
        per="registered_function",
        id="function-{f}",
        reads=("ontology", "functions.{f}"),
    ),
    Kind(
        name="remodel",
        stage="operate",
        per="question_with_model",
        id="remodel-{q}",
        reads=("ontology", "contract.questions.{q}", "models.{m}"),
        owns=("models.{m}.candidates", "models.{m}.features", "models.{m}.backtest"),
    ),
)


def stages(catalogue: tuple[Kind, ...] = CATALOGUE) -> list[str]:
    """Stages that have delegable work, in the order the catalogue introduces them."""
    return list(dict.fromkeys(kind.stage for kind in catalogue))


def _render(templates: tuple[str, ...], binding: Binding) -> tuple[str, ...]:
    """Fill the templates; one that refers to a variable the subject lacks is left out."""
    rendered = []
    for template in templates:
        try:
            rendered.append(template.format(**binding))
        except KeyError:
            continue
    return tuple(rendered)


def tasks_for(
    manifest: Manifest, stage: str, catalogue: tuple[Kind, ...] = CATALOGUE
) -> list[TaskSpec]:
    """Every task the manifest calls for at one stage, sorted by id."""
    known = stages(catalogue)
    if stage not in known:
        raise TaskError(
            f"no delegable work is defined for stage {stage!r}; choose one of {', '.join(known)}",
            code="tasks.stage.unknown",
            detail={"stage": stage, "stages": known},
        )
    drafts: dict[str, tuple[Kind, Binding]] = {}
    for kind in catalogue:
        if kind.stage != stage:
            continue
        for binding in SELECTORS[kind.per](manifest):
            for name, value in binding.items():
                if not _SUBJECT_ID.fullmatch(value):
                    log.error("manifest id cannot name a task", kind=kind.name, value=value[:80])
                    raise TaskError(
                        f"{value!r} cannot be used in a task id or a manifest path; ids are "
                        "lowercase letters, digits, '_' and '-', starting with a letter",
                        code="tasks.subject.invalid",
                        detail={"kind": kind.name, "variable": name, "value": value[:80]},
                    )
            task_id = kind.id.format(**binding)
            if task_id in drafts:
                other = drafts[task_id][1]
                log.error("two subjects produce one task id", id=task_id)
                raise TaskError(
                    f"two different subjects both produce the task id {task_id!r}; rename one",
                    code="tasks.plan.duplicate",
                    detail={"id": task_id, "subjects": [other, binding]},
                )
            drafts[task_id] = (kind, binding)

    specs = []
    for task_id, (kind, binding) in drafts.items():
        needs = tuple(need for need in _render(kind.needs, binding) if need in drafts)
        specs.append(
            TaskSpec(
                id=task_id,
                kind=kind.name,
                stage=kind.stage,
                subject=binding,
                reads=_render(kind.reads, binding),
                owns=_render(kind.owns, binding),
                needs=needs,
            )
        )
    return sorted(specs, key=lambda spec: spec.id)
