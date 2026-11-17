"""`dta register`, `dta models`, `dta questions` and `dta scenarios`: what the twin
computes, asked of its running API.

Each command reads the workspace's manifest and `.env`, talks to the API on the
host, and prints what the API answered. A refusal is shown with the API's own
code and detail; no value of `.env` is ever part of what is printed.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any
from urllib.parse import quote

import typer
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest

from ..acceptance import TwinApi
from ..output import command, emit, session
from . import computeops
from .common import scrub_values
from .sync import _api_stack, _scrub_error
from .syncjobs import refused

log = Log("commands.compute")

BAD_ARGUMENT = "compute.bad_argument"
_CELL = 40


@contextlib.contextmanager
def _api(ctx: typer.Context) -> Iterator[tuple[Manifest, TwinApi, dict[str, str]]]:
    """The manifest, a client of the twin's API and the values never to show."""
    workspace = session(ctx).workspace
    manifest, _stack, values, secrets = _api_stack(workspace)
    try:
        with TwinApi(manifest, values) as api:
            yield manifest, api, secrets
    except DtaError as exc:
        _scrub_error(exc, secrets)
        raise


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= _CELL else text[: _CELL - 1] + "…"


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
    """Rows as a plain text table, one column per key."""
    if not rows:
        return "(no rows)"
    names = list(columns or dict.fromkeys(key for row in rows for key in row))
    cells = [[_cell(row.get(name)) for name in names] for row in rows]
    widths = [max(len(name), *(len(line[i]) for line in cells)) for i, name in enumerate(names)]
    lines = ["  ".join(name.ljust(widths[i]) for i, name in enumerate(names)).rstrip()]
    lines += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(line)).rstrip() for line in cells]
    return "\n".join(lines)


# -- dta register ---------------------------------------------------------------------


def _counts_text(registered: Mapping[str, Any]) -> list[str]:
    lines = []
    for section, counts in (registered.get("counts") or {}).items():
        lines.append(
            f"{section:<9} created {counts['created']}, changed {counts['changed']}, "
            f"unchanged {counts['unchanged']}, orphaned {counts['orphaned']}"
        )
    return lines


@command
def register(ctx: typer.Context) -> None:
    """Register the manifest's functions, models and questions with the running twin.

    The twin's own code is packed and uploaded first when the manifest names it
    (runtime.code); nothing is written when nothing differs.
    """
    workspace = session(ctx).workspace
    with _api(ctx) as (manifest, api, secrets):
        uploaded = None
        if manifest.runtime.code:
            packed = computeops.pack_code(workspace, manifest)
            uploaded = computeops.upload_code(api, packed)
        registered = computeops.register(api, uploaded["sha256"] if uploaded else None)
    result = scrub_values({"code": uploaded, **registered}, secrets)
    lines = []
    if uploaded:
        lines.append(
            f"code      {uploaded['sha256'][:12]} "
            f"{'uploaded' if uploaded['uploaded'] else 'already held'} "
            f"({uploaded['files']} file(s), {uploaded['size_bytes']} bytes)"
        )
    lines += _counts_text(registered)
    version = registered.get("version") or {}
    lines.append(
        f"Registered at version {version.get('seq')}."
        if registered["changed_anything"]
        else "Nothing differed; nothing was written."
    )
    emit(ctx, result, "\n".join(lines))


# -- dta models ---------------------------------------------------------------------


def _evaluation_text(results: Sequence[Mapping[str, Any]], waited: bool) -> str:
    if not waited:
        return "\n".join(
            f"{entry['spec'] or 'every specification'}: job {entry['job'].get('id')} "
            f"{entry['job'].get('state')}"
            for entry in results
        )
    lines = []
    for summary in results:
        lines.append(
            f"{summary['spec']}: selected {summary['selected'] or '-'}; question "
            f"{summary['question']} {summary['question_status']}"
        )
        rows = [
            {
                "candidate": item["candidate"],
                "family": item["family"],
                "status": item["status"],
                "error": item["error"],
                "baseline": next(iter((item.get("baseline_scores") or {}).values()), None),
                "accepted": item["accepted"],
                "reasons": ", ".join(item["reasons"]) or None,
            }
            for item in summary["candidates"]
        ]
        lines += ["  " + line for line in table(rows).splitlines()]
    for entry in computeops.unaccepted(results):
        lines.append(f"warning: {entry['spec']} has no accepted candidate ({entry['reason']})")
    return "\n".join(lines)


@command
def models_evaluate(
    ctx: typer.Context,
    spec: Annotated[
        list[str] | None,
        typer.Option("--spec", help="A model specification to evaluate; every one when none."),
    ] = None,
    wait: Annotated[
        bool, typer.Option("--wait/--no-wait", help="Wait for each evaluation to end.")
    ] = True,
    timeout: Annotated[
        float, typer.Option("--timeout", min=1.0, help="Seconds to wait for each evaluation.")
    ] = computeops.EVALUATE_WAIT_S,
) -> None:
    """Backtest the candidates of model specifications and select the best accepted one."""
    with _api(ctx) as (manifest, api, secrets):
        names = list(dict.fromkeys(spec or []))
        unknown = sorted(set(names) - set(manifest.models or {}))
        if unknown:
            raise DtaError(
                f"the manifest has no model specification {', '.join(unknown)}",
                code=BAD_ARGUMENT,
                detail={"unknown": unknown, "specs": sorted(manifest.models or {})},
            )
        asked: list[str | None] = list(names) or [None]
        results = computeops.evaluate(api, asked, limit_s=timeout, wait=wait)
    result = scrub_values(
        {
            "specs": results,
            "waited": wait,
            "unaccepted": computeops.unaccepted(results) if wait else [],
        },
        secrets,
    )
    emit(ctx, result, _evaluation_text(results, wait))


@command
def models_list(ctx: typer.Context) -> None:
    """Every model of the twin: its specification, family, status and fitted span."""
    with _api(ctx) as (_manifest, api, secrets):
        found = computeops.read_all(api, computeops.MODELS)
    rows = [
        {
            "uid": model.get("uid"),
            "spec": model.get("spec"),
            "candidate": model.get("candidate"),
            "family": model.get("family"),
            "status": model.get("status"),
            "question": model.get("question"),
            "interventional": model.get("interventional"),
            "fitted": model.get("fitted"),
            "fitted_from": model.get("fitted_from"),
            "fitted_to": model.get("fitted_to"),
        }
        for model in found
    ]
    result = scrub_values({"models": rows}, secrets)
    emit(
        ctx,
        result,
        table(rows, ["uid", "status", "family", "question", "fitted", "fitted_to"]),
    )


# -- dta questions --------------------------------------------------------------------

_QUESTION_COLUMNS = ["id", "status", "basis", "measured_error", "tolerance", "metric", "reason"]


@command
def questions_list(ctx: typer.Context) -> None:
    """Each question and what it may claim now: status, basis, measured error, tolerance."""
    with _api(ctx) as (_manifest, api, secrets):
        found = computeops.questions(api)
    result = scrub_values({"questions": found}, secrets)
    emit(ctx, result, table(found, _QUESTION_COLUMNS))


@command
def questions_measure(
    ctx: typer.Context,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the job to end.")] = True,
    timeout: Annotated[
        float, typer.Option("--timeout", min=1.0, help="Seconds to wait for the measurement.")
    ] = computeops.MEASURE_WAIT_S,
) -> None:
    """Measure every question against its tolerance, as the twin's data stands now."""
    with _api(ctx) as (_manifest, api, secrets):
        ran = computeops.measure(api, limit_s=timeout, wait=wait)
        found = computeops.questions(api) if wait else []
    result = scrub_values({**ran, "questions": found, "waited": wait}, secrets)
    if wait:
        text = f"Measured {len(ran['measured'])} question(s).\n" + table(found, _QUESTION_COLUMNS)
    else:
        text = f"Job {ran['job'].get('id')} {ran['job'].get('state')}."
    emit(ctx, result, text)


def _argument(text: str) -> tuple[str, Any]:
    name, sep, raw = text.partition("=")
    if not sep or not name.strip():
        raise DtaError(
            f"--arg takes name=value, got {text!r}", code=BAD_ARGUMENT, detail={"arg": text}
        )
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw  # a plain word is a string
    return name.strip(), value


def _answer_text(answer: Mapping[str, Any]) -> str:
    value = answer.get("value")
    lines = []
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        lines.append(table(value))
    else:
        unit = f" {answer['unit']}" if answer.get("unit") else ""
        lines.append(f"{json.dumps(value, default=str)}{unit}")
    version = answer.get("version") or {}
    lines.append(f"version  {version.get('seq')} ({version.get('id')})")
    if answer.get("provisional"):
        lines.append(f"provisional: {'; '.join(answer.get('reasons') or []) or 'yes'}")
    if answer.get("notes"):
        lines.append(f"notes    {', '.join(answer['notes'])}")
    if answer.get("next_cursor"):
        lines.append("more rows: ask the API with the cursor in next_cursor")
    return "\n".join(lines)


@command
def questions_answer(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="The question's id.")],
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", help="An argument as name=value; a JSON value is read as such."),
    ] = None,
    at: Annotated[
        str, typer.Option("--at", help="The version to answer at: head, a seq, an id or a time.")
    ] = "head",
) -> None:
    """Ask the twin a question and print its answer; a table answer is shown as rows."""
    arguments = dict(_argument(text) for text in arg or [])
    path = f"{computeops.QUESTIONS}/{quote(question, safe='')}/answer"
    with _api(ctx) as (_manifest, api, secrets):
        response = api.request("POST", path, params={"at": at}, json=arguments)
        if response.status_code != 200:
            error = refused(response, f"POST {computeops.QUESTIONS}/{{id}}/answer")
            log.warn("the API refused to answer", question=question, code=error.code)
            raise error
        answer = response.json()
    result = scrub_values({"question": question, "arguments": arguments, **answer}, secrets)
    log.info("question answered", question=question, provisional=answer.get("provisional"))
    emit(ctx, result, _answer_text(answer))


# -- dta scenarios --------------------------------------------------------------------


@command
def scenarios_list(ctx: typer.Context) -> None:
    """Every scenario of the twin: its branch, what it changes and whether it was run."""
    with _api(ctx) as (_manifest, api, secrets):
        found = computeops.read_all(api, computeops.SCENARIOS)
    result = scrub_values({"scenarios": found}, secrets)
    emit(ctx, result, table(found, ["id", "name", "status", "branch", "horizon", "models"]))


__all__ = [
    "models_evaluate",
    "models_list",
    "questions_answer",
    "questions_list",
    "questions_measure",
    "register",
    "scenarios_list",
    "table",
]
