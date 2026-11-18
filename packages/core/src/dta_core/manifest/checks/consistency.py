"""Checks that the parts of the manifest agree with each other and with the tier."""

from __future__ import annotations

from collections.abc import Iterator

from ..models import Autonomy
from ..status import stale_status_entries
from .findings import Issue, Severity, check
from .secrets import credential_name, literal_value, url_problems
from .view import FORECAST_KINDS, ManifestView


@check("control.bounds")
def control_bounds(view: ManifestView) -> Iterator[Issue]:
    for cid, control in sorted(view.controls.items()):
        bounds = control.bounds
        if bounds is None:
            continue
        path = f"contract.controls.{cid}.bounds"
        if bounds.minimum is None and bounds.maximum is None and not bounds.allowed:
            yield Issue(path, "bounds give neither a minimum, a maximum nor allowed values")
        elif (
            bounds.minimum is not None
            and bounds.maximum is not None
            and bounds.minimum > bounds.maximum
        ):
            yield Issue(
                path, f"minimum {bounds.minimum:g} is greater than maximum {bounds.maximum:g}"
            )


@check("control.binding")
def control_binding(view: ManifestView) -> Iterator[Issue]:
    for cid, control in sorted(view.controls.items()):
        if control.autonomy not in (Autonomy.supervised, Autonomy.autonomous):
            continue
        action = view.actions.get(control.action) if control.action else None
        if action is None or action.binding is None:
            continue  # an unresolved action or a missing binding is reported elsewhere
        if action.binding != "executor":
            yield Issue(
                f"actions.{control.action}.binding",
                f"control '{cid}' is {control.autonomy.value}, so its action needs "
                f"binding: executor, not {action.binding}",
            )


@check("action.executor_addon")
def action_executor_addon(view: ManifestView) -> Iterator[Issue]:
    enabled = view.contract is not None and view.contract.addons.executors
    if enabled:
        return
    for aid, action in sorted(view.actions.items()):
        if action.binding == "executor":
            yield Issue(
                f"actions.{aid}.binding",
                "binding: executor requires contract.addons.executors: true",
            )


@check("tier.static_control")
def static_control(view: ManifestView) -> Iterator[Issue]:
    if not view.static:
        return
    for cid, control in sorted(view.controls.items()):
        if control.autonomy is not None and control.autonomy != Autonomy.advisory:
            yield Issue(
                f"contract.controls.{cid}.autonomy",
                f"a static twin may only advise, but control '{cid}' is {control.autonomy.value}",
            )


@check("tier.static_extras", Severity.warning)
def static_extras(view: ManifestView) -> Iterator[Issue]:
    if not view.static:
        return
    manifest = view.manifest
    # A static twin is loaded once, but its models and scenarios step on
    # sync.interval all the same; the bounds of the interval matter only to the
    # derivation of a grid that something steps on.
    sync = manifest.sync
    steps = bool(view.models) or any(
        question.kind in FORECAST_KINDS for question in view.questions.values()
    )
    bounds = sync is not None and any(
        value is not None for value in (sync.minimum_interval, sync.maximum_interval)
    )
    if bounds and not steps:
        yield Issue(
            "sync",
            "a static twin is loaded once and has no forecast to step: sync.minimum_interval "
            "and sync.maximum_interval bound nothing and will be ignored",
        )
    for section, present in (
        ("fidelity", manifest.fidelity is not None),
        ("control_loops", bool(manifest.control_loops)),
    ):
        if present:
            yield Issue(section, f"a static twin does not use {section}; it will be ignored")


@check("secret.inline")
def secret_inline(view: ManifestView) -> Iterator[Issue]:
    for sid, source in sorted(view.sources.items()):
        if source.location:
            for problem in url_problems(source.location):
                yield Issue(f"sources.{sid}.location", f"location carries {problem}")
    for aid, action in sorted(view.actions.items()):
        executor = action.executor
        if executor is None:
            continue
        base = f"actions.{aid}.executor"
        if executor.url:
            for problem in url_problems(executor.url):
                yield Issue(f"{base}.url", f"executor url carries {problem}")
        for header, value in sorted(executor.headers.items()):
            if credential_name(header) and literal_value(value):
                yield Issue(
                    f"{base}.headers.{header}",
                    f"header '{header}' carries a literal credential; use a ${{NAME}} placeholder",
                )


@check("function.interval")
def function_interval(view: ManifestView) -> Iterator[Issue]:
    for fid, function in sorted(view.functions.items()):
        if function.trigger == "interval" and function.interval is None:
            yield Issue(f"functions.{fid}.interval", "trigger: interval requires an interval")


@check("status.stale", Severity.warning)
def status_stale(view: ManifestView) -> Iterator[Issue]:
    for path in stale_status_entries(view.manifest):
        yield Issue(
            f"field_status.{path}",
            f"status entry for '{path}' no longer points at a required field with a value",
        )
