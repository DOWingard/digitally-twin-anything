"""What every connector shares: argument checks, the read loop that enforces one
schema, the row limit and the batch size, and errors that never carry a secret.

A connector supplies tables (already normalised, projected and filtered) from
`_pieces`; the loop here turns them into what the `Connector` protocol promises.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, NamedTuple
from urllib.parse import quote, quote_plus, unquote

import pyarrow as pa

from ..log import Log
from ..sources import BATCH_ROWS, SourceConfigError, SourceError, SourceReadError, UnknownDataset
from .normalize import SchemaMismatch, conform

_MASK = "***"
_CAUSE_CHARS = 500


class Redactor:
    """Removes the values that filled a location's placeholders from any text.

    URL-quoted forms are removed as well, because drivers report addresses in
    either form.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        variants: set[str] = set()
        for secret in secrets:
            if secret:
                variants.update(
                    {secret, quote(secret, safe=""), quote_plus(secret), unquote(secret)}
                )
        self._variants = sorted((v for v in variants if v), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for variant in self._variants:
            text = text.replace(variant, _MASK)
        return text

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value


class RedactedError(Exception):
    """Stands in for an exception whose text may hold a secret, keeping its traceback."""


@dataclass(frozen=True)
class ReadRequest:
    dataset: str
    columns: tuple[str, ...] | None
    since: Any
    cursor_column: str | None
    limit: int | None

    def needed_columns(self) -> tuple[str, ...] | None:
        """Columns to fetch: the requested ones plus the cursor, which filtering needs."""
        if self.columns is None:
            return None
        if self.cursor_column is None or self.cursor_column in self.columns:
            return self.columns
        return (*self.columns, self.cursor_column)


class Piece(NamedTuple):
    table: pa.Table
    origin: str | None = None  # a file name relative to the location, for messages


class BaseConnector:
    kind: ClassVar[str] = "base"

    def __init__(self, source_id: str, location: str, redactor: Redactor) -> None:
        self.source_id = source_id
        self.location = location  # placeholder form; safe to log
        self._redact = redactor
        self._log = Log(f"connect.{self.kind}")
        self._closed = False

    # -- protocol ------------------------------------------------------------

    def datasets(self) -> list[str]:
        self._check_open()
        started = time.perf_counter()
        try:
            names = sorted(self._list_datasets())
        except SourceError:
            raise
        except Exception as exc:
            raise self._translate(exc, None, "listing datasets") from None
        self._log.debug(
            "datasets listed",
            source=self.source_id,
            count=len(names),
            elapsed_ms=_elapsed_ms(started),
        )
        return names

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        self._check_open()
        request = self._request(dataset, columns, since, cursor_column, limit)
        if not self._has_dataset(request.dataset):
            raise self._unknown(request.dataset)
        self._log.debug(
            "reading dataset",
            source=self.source_id,
            dataset=request.dataset,
            columns=list(request.columns) if request.columns else None,
            cursor_column=cursor_column,
            filtered=since is not None,
            limit=limit,
        )
        return self._stream(request)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._release()
        except Exception as exc:
            self._log.warn(
                "releasing the connector failed; continuing",
                source=self.source_id,
                error=self._redact(f"{type(exc).__name__}: {exc}")[:_CAUSE_CHARS],
            )
        self._log.debug("connector closed", source=self.source_id)

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- hooks ---------------------------------------------------------------

    def _list_datasets(self) -> list[str]:
        raise NotImplementedError

    def _has_dataset(self, name: str) -> bool:
        return name in self.datasets()

    def _pieces(self, request: ReadRequest) -> Iterator[Piece]:
        raise NotImplementedError

    def _classify(self, exc: BaseException) -> type[SourceError]:
        return SourceReadError

    def _detail_for(self, exc: BaseException) -> dict[str, Any]:
        """Extra `detail` entries an error of this kind carries, such as a retry delay."""
        return {}

    def _release(self) -> None:
        return None

    # -- errors --------------------------------------------------------------

    def _error(
        self,
        cls: type[SourceError],
        message: str,
        *,
        dataset: str | None = None,
        cause: BaseException | None = None,
        **detail: Any,
    ) -> SourceError:
        """Build, log and return an error whose message and detail hold no secret."""
        message = self._redact(message)
        info: dict[str, Any] = {"source": self.source_id, "location": self.location}
        if dataset is not None:
            info["dataset"] = dataset
        if cause is not None:
            info["cause"] = self._describe(cause)
        info.update(self._redact.value(detail))
        error = cls(message, detail=info)
        self._log.error(
            message,
            exc=self._redacted(cause) if cause is not None else None,
            code=error.code,
            **info,
        )
        return error

    def _translate(self, exc: BaseException, dataset: str | None, action: str) -> SourceError:
        if isinstance(exc, SourceError):
            return exc
        cls = self._classify(exc)
        summary = self._describe(exc).splitlines()[0]
        return self._error(
            cls,
            f"{action} failed: {summary}",
            dataset=dataset,
            cause=exc,
            **self._detail_for(exc),
        )

    def _describe(self, exc: BaseException) -> str:
        return self._redact(f"{type(exc).__name__}: {exc}")[:_CAUSE_CHARS]

    def _redacted(self, exc: BaseException) -> RedactedError:
        clean = RedactedError(self._describe(exc))
        return clean.with_traceback(exc.__traceback__)

    def _unknown(self, dataset: str) -> SourceError:
        return self._error(
            UnknownDataset,
            f"source {self.source_id!r} has no dataset {dataset!r}",
            dataset=dataset,
        )

    def _argument(self, name: str, problem: str) -> SourceError:
        return self._error(SourceConfigError, f"{name} {problem}", argument=name)

    def _check_open(self) -> None:
        if self._closed:
            raise self._error(SourceError, "the connector is closed")

    # -- reading -------------------------------------------------------------

    def _request(
        self,
        dataset: Any,
        columns: Sequence[str] | None,
        since: Any,
        cursor_column: str | None,
        limit: int | None,
    ) -> ReadRequest:
        if not isinstance(dataset, str):
            raise self._argument("dataset", "must be a dataset name")
        selected: tuple[str, ...] | None = None
        if columns is not None:
            if isinstance(columns, str) or not all(isinstance(c, str) for c in columns):
                raise self._argument("columns", "must be a sequence of column names")
            selected = tuple(dict.fromkeys(columns))
            if not selected:
                raise self._argument("columns", "must name at least one column")
        if cursor_column is not None and not isinstance(cursor_column, str):
            raise self._argument("cursor_column", "must be a column name")
        if since is not None and cursor_column is None:
            raise self._argument("since", "needs cursor_column")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise self._argument("limit", "must be a non-negative integer")
        return ReadRequest(dataset, selected, since, cursor_column, limit)

    def _stream(self, request: ReadRequest) -> Iterator[pa.Table]:
        started = time.perf_counter()
        rows = batches = 0
        schema: pa.Schema | None = None
        remaining = request.limit
        finished = False
        try:
            pieces = self._pieces(request)
            try:
                for piece in pieces:
                    table = piece.table
                    if request.columns is not None and table.column_names != list(request.columns):
                        table = table.select(list(request.columns))
                    if schema is None:
                        schema = table.schema
                    else:
                        try:
                            table = conform(table, schema)
                        except SchemaMismatch as exc:
                            raise self._mismatch(request.dataset, piece, exc) from None
                    if remaining is not None:
                        table = table.slice(0, remaining)
                        remaining -= table.num_rows
                    for offset in range(0, table.num_rows, BATCH_ROWS):
                        part = table.slice(offset, BATCH_ROWS)
                        rows += part.num_rows
                        batches += 1
                        yield part
                    if remaining == 0:
                        break
            finally:
                pieces.close()
            if rows == 0:
                if schema is None:
                    raise self._error(
                        SourceReadError,
                        "the dataset produced no table to take a schema from",
                        dataset=request.dataset,
                    )
                batches += 1
                yield schema.empty_table()
            finished = True
        except SourceError:
            raise
        except Exception as exc:
            raise self._translate(
                exc, request.dataset, f"reading dataset {request.dataset!r}"
            ) from None
        finally:
            if finished:
                self._log.info(
                    "dataset read",
                    source=self.source_id,
                    dataset=request.dataset,
                    rows=rows,
                    batches=batches,
                    elapsed_ms=_elapsed_ms(started),
                )
            else:
                self._log.debug(
                    "dataset read ended early",
                    source=self.source_id,
                    dataset=request.dataset,
                    rows=rows,
                    batches=batches,
                    elapsed_ms=_elapsed_ms(started),
                )

    def _mismatch(self, dataset: str, piece: Piece, exc: SchemaMismatch) -> SourceError:
        where = f" in {piece.origin}" if piece.origin else ""
        return self._error(
            SourceReadError,
            f"dataset {dataset!r} changes schema{where}: {exc}",
            dataset=dataset,
            file=piece.origin,
            columns=exc.columns,
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)
