"""Checks that every id, variable name and property reference points at something real.

A reference that is not set yet is a gap, not a finding; these checks only judge
values that are present.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...families import FAMILIES
from ...functions import BUILTINS, REGISTERED_ENTRY, ParamKind, shape_problem
from .filters import clause_problem, edge_clause_problem
from .findings import Issue, Severity, check
from .view import FORECAST_KINDS, ManifestView, is_property_ref


@check("ref.question_function")
def question_function(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        if question.function is not None and question.function not in view.functions:
            yield Issue(
                f"contract.questions.{qid}.function",
                f"function '{question.function}' is not declared in functions",
            )


@check("ref.function_entry")
def function_entry(view: ManifestView) -> Iterator[Issue]:
    """A built-in has to exist and its fixed parameters have to be ones it takes; a function
    supplied as code is named as `module:function`."""
    for fid, function in sorted(view.functions.items()):
        if function.kind is None or function.entry is None:
            continue
        base = f"functions.{fid}"
        if function.kind == "registered":
            if not REGISTERED_ENTRY.match(function.entry):
                yield Issue(
                    f"{base}.entry",
                    f"'{function.entry}' does not name code: write module:function",
                )
            continue
        builtin = BUILTINS.get(function.entry)
        if builtin is None:
            yield Issue(
                f"{base}.entry",
                f"'{function.entry}' is not a built-in function; the built-ins are "
                f"{', '.join(sorted(BUILTINS))}",
            )
            continue
        for name, value in sorted(function.params.items()):
            parameter = builtin.parameter(name)
            if parameter is None:
                takes = ", ".join(found.name for found in builtin.parameters)
                yield Issue(
                    f"{base}.params.{name}",
                    f"{function.entry} takes no parameter '{name}'; it takes {takes}",
                )
                continue
            problem = shape_problem(parameter, value)
            if problem is not None:
                yield Issue(f"{base}.params.{name}", f"{name} {problem}")
                continue
            judge = {
                ParamKind.filters: clause_problem,
                ParamKind.edge_filters: edge_clause_problem,
            }.get(parameter.kind)
            if judge is not None:
                for clause in value:
                    problem = judge(str(clause))
                    if problem is not None:
                        yield Issue(f"{base}.params.{name}", problem)


@check("ref.model_family")
def model_family(view: ManifestView) -> Iterator[Issue]:
    """A candidate is a family the runtime ships, or code named as `module:name`."""
    for mid, model in sorted(view.models.items()):
        for cid, candidate in sorted((model.candidates or {}).items()):
            family = candidate.family
            if family is None or family in FAMILIES or REGISTERED_ENTRY.match(family):
                continue
            yield Issue(
                f"models.{mid}.candidates.{cid}.family",
                f"'{family}' is neither a shipped family ({', '.join(FAMILIES)}) nor code "
                "named as module:name",
            )


@check("ref.code_declared")
def code_declared(view: ManifestView) -> Iterator[Issue]:
    """Code of the twin's own has to come from somewhere: `runtime.code`."""
    if view.manifest.runtime.code:
        return
    for fid, function in sorted(view.functions.items()):
        if function.kind == "registered":
            yield Issue(
                f"functions.{fid}.kind",
                "the function is supplied as code but runtime.code names no directory",
            )
    for mid, model in sorted(view.models.items()):
        for cid, candidate in sorted((model.candidates or {}).items()):
            family = candidate.family
            if family is not None and family not in FAMILIES and REGISTERED_ENTRY.match(family):
                yield Issue(
                    f"models.{mid}.candidates.{cid}.family",
                    f"'{family}' is code but runtime.code names no directory",
                )


@check("ref.question_model")
def question_model(view: ManifestView) -> Iterator[Issue]:
    for qid, question in sorted(view.questions.items()):
        if question.kind not in FORECAST_KINDS or question.model is None:
            continue
        path = f"contract.questions.{qid}.model"
        model = view.models.get(question.model)
        if model is None:
            yield Issue(path, f"model '{question.model}' is not declared in models")
        elif model.question != qid:
            yield Issue(
                path,
                f"model '{question.model}' serves question '{model.question}', not '{qid}'",
            )


@check("ref.model_question")
def model_question(view: ManifestView) -> Iterator[Issue]:
    for mid, model in sorted(view.models.items()):
        if model.question is not None and model.question not in view.questions:
            yield Issue(
                f"models.{mid}.question",
                f"question '{model.question}' is not declared in contract.questions",
            )


@check("ref.control_action")
def control_action(view: ManifestView) -> Iterator[Issue]:
    for cid, control in sorted(view.controls.items()):
        if control.action is None:
            continue
        path = f"contract.controls.{cid}.action"
        action = view.actions.get(control.action)
        if action is None:
            yield Issue(path, f"action '{control.action}' is not declared in actions")
        elif action.control != cid:
            yield Issue(
                path,
                f"action '{control.action}' carries out control '{action.control}', not '{cid}'",
            )


@check("ref.loop")
def loop(view: ManifestView) -> Iterator[Issue]:
    for lid, control_loop in sorted(view.loops.items()):
        for cid in control_loop.controls or []:
            if cid not in view.controls:
                yield Issue(
                    f"control_loops.{lid}.controls",
                    f"control '{cid}' is not declared in contract.controls",
                )
        if control_loop.policy is not None and control_loop.policy not in view.functions:
            yield Issue(
                f"control_loops.{lid}.policy",
                f"function '{control_loop.policy}' is not declared in functions",
            )


@check("ref.objective_kpi")
def objective_kpi(view: ManifestView) -> Iterator[Issue]:
    objective = view.contract.objective if view.contract else None
    if objective is not None and objective.kpi is not None and objective.kpi not in view.functions:
        yield Issue(
            "contract.objective.kpi", f"function '{objective.kpi}' is not declared in functions"
        )


@check("ref.env_declared")
def env_declared(view: ManifestView) -> Iterator[Issue]:
    reported: set[tuple[str, str]] = set()
    for name, path in view.env_uses():
        if name not in view.env and (name, path) not in reported:
            reported.add((name, path))
            yield Issue(path, f"variable '{name}' is used but not declared in env")


@check("ref.env_unused", Severity.warning)
def env_unused(view: ManifestView) -> Iterator[Issue]:
    used = {name for name, _path in view.env_uses()}
    for name in sorted(view.env):
        if name not in used:
            yield Issue(f"env.{name}", f"variable '{name}' is declared but nothing uses it")


@check("ref.clock_source")
def clock_source(view: ManifestView) -> Iterator[Issue]:
    """A twin on a source's clock needs that source, and the source has to tell the time."""
    clock = view.manifest.runtime.clock
    if not clock.startswith("source:"):
        return
    source_id = clock.removeprefix("source:")
    source = view.sources.get(source_id)
    if source is None:
        yield Issue("runtime.clock", f"source '{source_id}' is not defined")
    elif not isinstance(source.options.get("clock"), dict):
        yield Issue(
            "runtime.clock",
            f"source '{source_id}' has no clock option saying where its time is read",
        )


@check("ref.incremental")
def incremental(view: ManifestView) -> Iterator[Issue]:
    """A dataset read on by its time column has to be a dataset, and has to have the column."""
    for sid, source in sorted(view.sources.items()):
        for name, column in sorted(source.incremental.items()):
            path = f"sources.{sid}.incremental.{name}"
            if name in (source.views or {}):
                yield Issue(path, f"'{name}' is a view; name the dataset it reads instead")
                continue
            if source.profile is None:
                continue
            dataset = source.profile.datasets.get(name)
            if dataset is None:
                yield Issue(path, f"dataset '{name}' is not in the profile of source '{sid}'")
            elif column not in dataset.columns:
                yield Issue(path, f"column '{column}' is not in dataset '{name}'")


def _property_refs(view: ManifestView) -> Iterator[tuple[str, str]]:
    for qid, question in sorted(view.questions.items()):
        for ref in question.targets or []:
            yield ref, f"contract.questions.{qid}.targets"
    for cid, control in sorted(view.controls.items()):
        if control.target is not None:
            yield control.target, f"contract.controls.{cid}.target"
    boundary = view.contract.boundary if view.contract else None
    if boundary is not None:
        for role in ("state", "controls", "drivers", "observations"):
            for ref in getattr(boundary, role) or []:
                yield ref, f"contract.boundary.{role}"
    for mid, model in sorted(view.models.items()):
        if model.target is not None:
            yield model.target, f"models.{mid}.target"
        for ref in model.features or []:
            yield ref, f"models.{mid}.features"


@check("ref.property_format")
def property_format(view: ManifestView) -> Iterator[Issue]:
    for ref, path in _property_refs(view):
        if not is_property_ref(ref):
            yield Issue(path, f"'{ref}' is not a property reference of the form Type.property")


@check("ref.mapping_columns")
def mapping_columns(view: ManifestView) -> Iterator[Issue]:
    for sid, source in sorted(view.sources.items()):
        if source.profile is None:
            continue
        datasets = source.profile.datasets
        for mid, mapping in sorted((source.mappings or {}).items()):
            base = f"sources.{sid}.mappings.{mid}"
            if mapping.dataset is None:
                continue
            if mapping.dataset in (source.views or {}):
                # A view's columns exist only once its query has run: the profile
                # measures what the source holds, and the load checks the view.
                continue
            dataset = datasets.get(mapping.dataset)
            if dataset is None:
                yield Issue(
                    f"{base}.dataset",
                    f"dataset '{mapping.dataset}' is not in the profile of source '{sid}'",
                )
                continue
            columns = dataset.columns
            for column in mapping.identity or []:
                if column not in columns:
                    yield Issue(
                        f"{base}.identity",
                        f"column '{column}' is not in dataset '{mapping.dataset}'",
                    )
            if mapping.valid_time is not None and mapping.valid_time not in columns:
                yield Issue(
                    f"{base}.valid_time",
                    f"column '{mapping.valid_time}' is not in dataset '{mapping.dataset}'",
                )
            for prop, mapped in sorted((mapping.properties or {}).items()):
                if mapped.column is not None and mapped.column not in columns:
                    yield Issue(
                        f"{base}.properties.{prop}.column",
                        f"column '{mapped.column}' is not in dataset '{mapping.dataset}'",
                    )
