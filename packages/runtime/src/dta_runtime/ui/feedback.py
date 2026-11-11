"""How callbacks report trouble to the user, and the wrapper every callback runs in.

A failed read is shown where it happened as the problem's title, code, detail
and trace id; never a stack trace, and the rest of the page keeps working.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from typing import Any

from dash import html, no_update
from dash.exceptions import PreventUpdate
from dta_core.log import Log, new_trace_id, trace

from .client import ApiProblem

log = Log("ui.callback")


def problem_box(problem: ApiProblem) -> html.Div:
    parts: list[Any] = [
        html.Strong(problem.title, className="problem-title"),
        " ",
        html.Code(problem.code, className="problem-code"),
    ]
    if problem.detail:
        parts += [html.Div(problem.detail, className="problem-detail")]
    if problem.trace_id:
        parts += [html.Div(["trace id ", html.Code(problem.trace_id)], className="problem-trace")]
    return html.Div(parts, className="problem", role="alert")


def note(text: str, *, warn: bool = False) -> html.Div:
    return html.Div(text, className="note warn" if warn else "note")


def internal_problem(trace_id: str) -> ApiProblem:
    return ApiProblem(
        code="ui.internal",
        title="The interface failed",
        detail="this part of the page could not be shown; quote the trace id when reporting it",
        trace_id=trace_id,
    )


def guarded(outputs: int, *, fallback: Sequence[Any] | None = None) -> Callable[..., Any]:
    """Wrap a callback whose last output is its feedback area.

    The wrapped function returns its other `outputs` (one value, or a tuple of
    them). The wrapper binds a trace id, which the API client sends on, clears
    the feedback area on success and on failure fills it with the problem,
    leaving the other outputs as they were, or set to `fallback`.
    """

    def wrap(function: Callable[..., Any]) -> Callable[..., Any]:
        name = function.__name__

        @functools.wraps(function)
        def run(*args: Any, **kwargs: Any) -> Any:
            with trace(new_trace_id()) as trace_id:
                started = time.perf_counter()
                try:
                    result = function(*args, **kwargs)
                except PreventUpdate:
                    raise
                except ApiProblem as problem:
                    log.warn(
                        "callback shows an API problem",
                        callback=name,
                        code=problem.code,
                        status=problem.status,
                        duration_ms=_ms(started),
                    )
                    return _failed(outputs, fallback, problem)
                except Exception as exc:
                    log.error("callback failed", exc=exc, callback=name, duration_ms=_ms(started))
                    return _failed(outputs, fallback, internal_problem(trace_id))
                log.debug("callback served", callback=name, duration_ms=_ms(started))
                values = list(result) if outputs > 1 else [result]
                return [*values, None]

        return run

    return wrap


def _failed(outputs: int, fallback: Sequence[Any] | None, problem: ApiProblem) -> list[Any]:
    values = list(fallback) if fallback is not None else [no_update] * outputs
    return [*values, problem_box(problem)]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)
