"""Running a built-in: the entry, its resolved parameters, the pinned version."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log

from ..protocol import Answer
from . import paths as paths_module
from . import pointwise, spans
from .context import Context
from .errors import PARAM_MISSING, PARAM_UNKNOWN, UNAVAILABLE, refused
from .params import builtin_of
from .reads import Reads

log = Log("functions.run")


def _threshold(direction: str, reads: Reads, params: Mapping[str, Any]) -> Answer:
    target = reads.target("property", params["property"], numeric=True)
    first, last = params.get("from"), params.get("to")
    if (first is None) != (last is None):
        absent = "from" if first is None else "to"
        raise reads.bad(absent, "a range has both from and to; only one of them is given")
    if first is None:
        return pointwise.threshold_at(reads, direction, target, params)
    start, end = reads.span(first, last)
    return spans.threshold_over(reads, direction, target, start, end, params)


def _forecast(reads: Reads, params: Mapping[str, Any]) -> Answer:
    context = reads.context
    if context.forecast is None:
        raise refused(
            UNAVAILABLE,
            f"{reads.function}: a forecast was asked for, and nothing here can make one",
            function=reads.function,
            entry=reads.entry,
        )
    return context.forecast(params, context)


_ENTRIES: dict[str, Callable[[Reads, Mapping[str, Any]], Answer]] = {
    "latest": pointwise.latest,
    "below": partial(_threshold, "below"),
    "above": partial(_threshold, "above"),
    "integrate": spans.integrate,
    "paths": paths_module.paths,
    "aggregate": pointwise.aggregate,
    "series": spans.series,
    "forecast": _forecast,
}


def _size(answer: Answer) -> int | None:
    value = answer.value
    if isinstance(value, pa.Table):
        return value.num_rows
    return len(value) if isinstance(value, list) else None


def run(function_id: str, entry: str, params: Mapping[str, Any], context: Context) -> Answer:
    """The answer of the built-in `entry` with resolved parameters, at `context.version`.

    Refuses what only the ontology decides with `function.bad_param` and
    `function.bad_unit`, gives up at the deadline with `function.timeout` without
    starting another read, and hands a forecast to `context.forecast`
    (`function.unavailable` when there is none).
    """
    started = time.perf_counter()
    builtin = builtin_of(function_id, entry)
    reads = Reads(function_id, entry, context)
    fields = {
        "function": function_id,
        "entry": entry,
        "version": context.version.id,
        "seq": context.version.seq,
        "branch": context.version.branch,
    }
    try:
        reads.guard()
        for name in params:
            if builtin.parameter(name) is None:
                raise refused(
                    PARAM_UNKNOWN,
                    f"{function_id}: {entry} has no parameter {name!r}",
                    function=function_id,
                    parameter=name,
                )
        for name in builtin.required:
            if name not in params:
                raise refused(
                    PARAM_MISSING,
                    f"{function_id}: {entry} needs {name}",
                    function=function_id,
                    parameter=name,
                )
        # What the table gives a parameter that was left out.
        values = {
            parameter.name: parameter.default
            for parameter in builtin.parameters
            if parameter.default is not None
        }
        values.update(params)
        answer = _ENTRIES[entry](reads, values)
    except DtaError as exc:
        log.warn(
            "function refused",
            code=exc.code,
            reason=exc.message,
            detail=exc.detail,
            reads=reads.count,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            **fields,
        )
        raise
    except Exception as exc:
        log.error(
            "function failed",
            exc=exc,
            parameters=sorted(params),
            reads=reads.count,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
            **fields,
        )
        raise
    log.info(
        "function run",
        rows=_size(answer),
        reads=reads.count,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
        **fields,
    )
    return answer
