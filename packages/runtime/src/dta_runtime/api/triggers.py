"""Functions run on their triggers, and runs asked for under a key, as jobs.

A function whose trigger is `interval` is asked for by the scheduler once per boundary of
its interval (`POST /functions/{id}/run` with the key `function:<id>:<boundary>`); one
whose trigger is `on_commit` is run after every sync run that wrote a version, under the
key `commit:<id>:<head id>`. Both are the ordinary run of the function, as a job of the
compute lane, with what it gives to write written. A run started by a trigger always
leaves a `TwinFunctionRun`: whether it succeeded, the rows it wrote, how long it took and
why it failed. A run asked for by a request leaves one only when it wrote.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.store import LIVE_BRANCH, VersionRef

from ..protocol import answer_document
from . import predicting, registry, running
from .formats import rows as table_rows
from .formats import without_nan
from .jobs import JobsClosed, Outcome, Work
from .params import JSON_LIMIT_MAX
from .problems import ApiError
from .tables import version_json

if TYPE_CHECKING:
    from ..sync.report import RunReport
    from .services import Services

log = Log("api.triggers")

ON_REQUEST = running.ON_REQUEST
ON_COMMIT = "on_commit"
INTERVAL = "interval"
FUNCTION_KIND = "function"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def trigger_of(record: Mapping[str, Any], key: str | None) -> str:
    """A keyed run of a function whose trigger is `interval` is that trigger's run: the
    scheduler is what asks for one with a key. Any other run is on request."""
    if key is not None and record.get("trigger") == INTERVAL:
        return INTERVAL
    return ON_REQUEST


def _error_text(exc: BaseException) -> str:
    """What a run's record keeps of its failure: its code and message, never a stack."""
    if isinstance(exc, DtaError):
        return f"{exc.code}: {exc.message}"
    return f"{type(exc).__name__} was raised; the cause is in the log"


def _record(
    services: Services,
    record: Mapping[str, Any],
    version: VersionRef,
    call: Mapping[str, Any],
    *,
    trigger: str,
    status: str,
    rows_written: int,
    duration_ms: float,
    error: str | None,
) -> str | None:
    """Write the record of a triggered run that wrote nothing of its own; its uid, or
    None when it could not be written (the run's outcome stands)."""
    text = registry.as_json(dict(call))
    trace_id = predicting.trace_id()
    row = {
        "uid": running.run_uid(record["uid"], version, trace_id, text),
        "function": record["uid"],
        "trigger": trigger,
        "status": status,
        "started_at": version.t,
        "at_version": version.id,
        "params": text,
        "rows_written": rows_written,
        "duration_ms": round(duration_ms),
        "error": error,
        "trace_id": trace_id,
    }
    try:
        registry.commit(
            services,
            {registry.RUN_TYPE: [row]},
            note=f"run of {registry.bare(record['uid'])} ({trigger}): {status}",
            extra={"function": record["uid"], "trigger": trigger, "status": status},
            t_valid=version.t,
        )
    except DtaError as exc:
        log.error(
            "the record of a triggered run could not be written",
            exc=exc,
            code=exc.code,
            function=record["uid"],
            trigger=trigger,
            status=status,
        )
        return None
    log.info(
        "triggered run recorded",
        function=record["uid"],
        trigger=trigger,
        status=status,
        run=row["uid"],
        rows_written=rows_written,
    )
    return str(row["uid"])


def run_recorded(
    services: Services,
    record: Mapping[str, Any],
    version: VersionRef,
    call: Mapping[str, Any],
    *,
    trigger: str,
    known: VersionRef,
) -> running.Ran:
    """The ordinary run of a function, writing what it gives; a run started by a trigger
    is recorded whatever it came to."""
    started = time.perf_counter()
    try:
        ran = running.run(services, record, version, call, known=known, write=True, trigger=trigger)
    except Exception as exc:
        if trigger != ON_REQUEST:
            log.error(
                "a triggered run failed",
                exc=exc,
                function=record["uid"],
                trigger=trigger,
                seq=version.seq,
            )
            _record(
                services,
                record,
                version,
                call,
                trigger=trigger,
                status=FAILED,
                rows_written=0,
                duration_ms=_ms(started),
                error=_error_text(exc),
            )
        raise
    if trigger != ON_REQUEST and ran.wrote is None:
        _record(
            services,
            record,
            version,
            call,
            trigger=trigger,
            status=SUCCEEDED,
            rows_written=0,
            duration_ms=ran.duration_ms,
            error=None,
        )
    return ran


def answer_result(ran: running.Ran, version: VersionRef, trigger: str) -> dict[str, Any]:
    """A run's answer as a job keeps it: a table as its rows, at most JSON_LIMIT_MAX."""
    answer = ran.answer
    value = answer.value
    plain = (
        answer
        if not isinstance(value, pa.Table)
        else type(answer)(
            value=None, unit=answer.unit, interval=answer.interval, notes=answer.notes
        )
    )
    document = answer_document(
        plain,
        version=version_json(version),
        function=ran.function,
        model=ran.model,
        provisional=ran.provisional,
        reasons=ran.reasons,
    )
    if isinstance(value, pa.Table):
        document["value"] = table_rows(without_nan(value.slice(0, JSON_LIMIT_MAX)))
        document["truncated"] = value.num_rows > JSON_LIMIT_MAX
    document["wrote"] = version_json(ran.wrote) if ran.wrote is not None else None
    document["rows_written"] = ran.rows_written
    document["trigger"] = trigger
    return document


def function_work(
    services: Services,
    function_id: str,
    call: Mapping[str, Any],
    *,
    at: str,
    branch: str,
    trigger: str,
) -> Work:
    """One run of a function as a job: the version resolved and the registry read when
    the job runs, so a run asked for at the head reads the head of its time."""

    def work() -> Outcome:
        started = time.perf_counter()
        version = services.resolve(at, branch)
        known = services.store.head()
        record = registry.function_or_404(services, function_id, known)
        ran = run_recorded(services, record, version, call, trigger=trigger, known=known)
        log.info(
            "function job finished",
            function=function_id,
            trigger=trigger,
            seq=version.seq,
            wrote=None if ran.wrote is None else ran.wrote.id,
            elapsed_ms=_ms(started),
        )
        return Outcome(result=answer_result(ran, version, trigger))

    return work


def start_run(
    services: Services,
    function_id: str,
    call: Mapping[str, Any],
    given: Mapping[str, Any],
    *,
    at: str,
    branch: str,
    key: str,
) -> tuple[dict[str, Any], bool]:
    """A keyed run of a function, queued as a job; what refuses it at once does so here."""
    known = services.store.head()
    record = registry.function_or_404(services, function_id, known)
    if given and record["kind"] != "builtin":
        raise ApiError(
            "the arguments of code of the twin's own are given in the body, as JSON",
            code="api.bad_request",
        )
    if record["status"] == "disabled":
        log.info("run refused: the function is disabled", function=function_id)
        raise ApiError(f"the function {function_id!r} is disabled", code="function.disabled")
    bare_id = str(registry.bare(record["uid"]))
    trigger = trigger_of(record, key)
    arguments = {**call, **given}
    return services.compute_jobs.start(
        FUNCTION_KIND,
        {
            "function": bare_id,
            "arguments": arguments,
            "at": at,
            "branch": branch,
            "trigger": trigger,
        },
        function_work(services, bare_id, arguments, at=at, branch=branch, trigger=trigger),
        key,
    )


def after_sync(services: Services, report: RunReport) -> list[dict[str, Any]]:
    """Start the run of every active `on_commit` function once a sync run wrote a
    version. Never raises: the sync run neither waits for these runs nor fails by them."""
    head = report.head
    if report.versions_written < 1 or head is None:
        return []
    started = time.perf_counter()
    try:
        found = registry.rows(services, registry.FUNCTION_TYPE, head, [("trigger", "=", ON_COMMIT)])
    except Exception as exc:
        log.error(
            "the functions run on a commit could not be read; none is started",
            exc=exc,
            head=head.id,
        )
        return []
    queued: list[dict[str, Any]] = []
    for record in found:
        if record.get("status") == "disabled":
            continue
        function_id = str(registry.bare(record["uid"]))
        key = f"commit:{function_id}:{head.id}"
        params = {
            "function": function_id,
            "arguments": {},
            "at": head.id,
            "branch": LIVE_BRANCH,
            "trigger": ON_COMMIT,
        }
        try:
            document, created = services.compute_jobs.start(
                FUNCTION_KIND,
                params,
                function_work(
                    services, function_id, {}, at=head.id, branch=LIVE_BRANCH, trigger=ON_COMMIT
                ),
                key,
            )
        except (JobsClosed, ApiError) as exc:
            log.error(
                "the run of a function on a commit could not be queued",
                exc=exc,
                code=exc.code,
                function=function_id,
                key=key,
            )
            continue
        except Exception as exc:
            log.error(
                "the run of a function on a commit could not be queued",
                exc=exc,
                function=function_id,
                key=key,
            )
            continue
        queued.append(
            {"function": function_id, "key": key, "job": document["id"], "created": created}
        )
    if queued:
        log.info(
            "functions run on a commit queued",
            head=head.id,
            seq=head.seq,
            functions=[item["function"] for item in queued],
            elapsed_ms=_ms(started),
        )
    return queued
