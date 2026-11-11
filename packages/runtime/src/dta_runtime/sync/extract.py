"""Reads each dataset a run needs once, through its source's connector, into Arrow."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest.models import Source
from dta_core.sources import Connector, placeholders

from .plan import DatasetPlan, Plan, RelationKey

log = Log("sync.extract")

Connect = Callable[[str, Source, Mapping[str, str]], Connector]


def required_env(source: Source) -> list[str]:
    """Names of the variables a source needs: those it declares and those its address uses."""
    names: dict[str, None] = dict.fromkeys(source.env or ())
    for name in placeholders(source.location or ""):
        names.setdefault(name)
    return list(names)


def scrub(text: str, source: Source, env: Mapping[str, str]) -> str:
    """`text` with the value of every variable the source uses put back as `${NAME}`."""
    # Longest first, so a value that contains another is replaced whole.
    values = sorted(
        ((env.get(name) or "", name) for name in required_env(source)),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for value, name in values:
        if value:
            text = text.replace(value, "${" + name + "}")
    return text


@dataclass(frozen=True)
class DatasetRead:
    plan: DatasetPlan
    table: pa.Table
    # Only rows later than this were asked for; None when the dataset was read whole.
    since: datetime | None


class SourceFailure(Exception):
    """A source could not be read. The message is safe to store and to log."""

    def __init__(self, source_id: str, dataset: str | None, code: str, message: str) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.dataset = dataset
        self.code = code
        self.message = message


def _failure(
    source_id: str,
    dataset: str | None,
    source: Source,
    env: Mapping[str, str],
    exc: Exception,
) -> SourceFailure:
    if isinstance(exc, DtaError):
        code, message = exc.code, scrub(exc.message, source, env)
    else:
        # The text of an error that is not ours may quote an address or a
        # credential, so only its type is kept.
        code, message = "source.error", f"{type(exc).__name__} while reading the source"
    log.error(
        "source cannot be read; the run stops and records the failure",
        source=source_id,
        dataset=dataset,
        code=code,
        reason=message,
        error_type=type(exc).__name__,
        # Frames only: the exception's own text is left out for the same reason.
        frames="".join(traceback.format_tb(exc.__traceback__)),
    )
    return SourceFailure(source_id, dataset, code, message)


def _read(connector: Connector, dataset: DatasetPlan, since: datetime | None) -> pa.Table:
    if dataset.cursor_column is not None and since is not None:
        batches = list(
            connector.read(dataset.name, since=since, cursor_column=dataset.cursor_column)
        )
    else:
        batches = list(connector.read(dataset.name))
    if not batches:
        raise ValueError("the connector returned no table")
    return pa.concat_tables(batches, promote_options="default")


def extract(
    plan: Plan,
    sources: Mapping[str, Source],
    env: Mapping[str, str],
    connect: Connect,
    cursors: Mapping[RelationKey, datetime],
) -> dict[RelationKey, DatasetRead]:
    """Every dataset of the plan, read once. Raises `SourceFailure` at the first source
    that cannot be read."""
    result: dict[RelationKey, DatasetRead] = {}
    for source_id in plan.source_ids():
        source = sources[source_id]
        wanted = [dataset for dataset in plan.datasets if dataset.source_id == source_id]
        started = time.perf_counter()
        try:
            connector = connect(source_id, source, env)
        except Exception as exc:
            raise _failure(source_id, None, source, env, exc) from None
        try:
            for dataset in wanted:
                since = cursors.get(dataset.key) if dataset.cursor_column else None
                read_started = time.perf_counter()
                try:
                    table = _read(connector, dataset, since)
                except Exception as exc:
                    raise _failure(source_id, dataset.name, source, env, exc) from None
                result[dataset.key] = DatasetRead(dataset, table, since)
                log.info(
                    "dataset read",
                    source=source_id,
                    dataset=dataset.name,
                    rows=table.num_rows,
                    columns=table.num_columns,
                    incremental=since is not None,
                    elapsed_ms=round((time.perf_counter() - read_started) * 1000.0, 1),
                )
        finally:
            try:
                connector.close()
            except Exception as exc:
                log.warn(
                    "closing a connector failed; carrying on",
                    source=source_id,
                    error_type=type(exc).__name__,
                )
        log.debug(
            "source read",
            source=source_id,
            datasets=len(wanted),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
    return result
