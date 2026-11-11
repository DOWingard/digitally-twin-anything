"""Read-only connector to a folder of CSV, Parquet or JSON Lines files.

The folder is any fsspec location: a local directory, a web server's
directory index, or an object store whose driver is installed. Each dataset is
a glob inside the folder; its files are read in name order and must agree on
the columns that are read.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
import fsspec
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.dataset as pads
import pyarrow.fs as pafs
import pyarrow.json as pajson
from fsspec.implementations.local import LocalFileSystem
from fsspec.registry import known_implementations

from ..sources import (
    BATCH_ROWS,
    SourceAuthFailed,
    SourceConfigError,
    SourceError,
    SourceRateLimited,
    SourceReadError,
    SourceUnavailable,
)
from . import options as opt
from .base import BaseConnector, Piece, ReadRequest, Redactor, _elapsed_ms
from .normalize import SchemaMismatch, as_utc_datetime, filter_after, local_naive, normalize_table

FORMATS = ("csv", "parquet", "jsonl")
OPTION_KEYS = ("datasets",)
DATASET_KEYS = ("glob", "format", "types", "timezone")
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class FileDataset:
    name: str
    glob: str
    format: str
    types: dict[str, pa.DataType] = field(default_factory=dict)
    timezone: str = "UTC"


def parse_options(raw: Mapping[str, Any]) -> dict[str, FileDataset]:
    opt.mapping(raw, "options")
    opt.only_keys(raw, OPTION_KEYS, "")
    if raw.get("datasets") is None:
        raise opt.invalid("datasets", "a file source must declare its datasets")
    declared = opt.mapping(raw["datasets"], "datasets")
    if not declared:
        raise opt.invalid("datasets", "must declare at least one dataset")
    datasets: dict[str, FileDataset] = {}
    for name, entry in declared.items():
        path = f"datasets.{name}"
        entry = opt.mapping(entry, path)
        opt.only_keys(entry, DATASET_KEYS, path)
        glob = _check_glob(opt.text(entry.get("glob"), f"{path}.glob"), f"{path}.glob")
        kind = opt.text(entry.get("format"), f"{path}.format")
        if kind not in FORMATS:
            raise opt.invalid(f"{path}.format", f"must be one of {', '.join(FORMATS)}")
        types: dict[str, pa.DataType] = {}
        if entry.get("types") is not None:
            for column, type_name in opt.mapping(entry["types"], f"{path}.types").items():
                types[column] = opt.arrow_type(type_name, f"{path}.types.{column}")
        timezone = opt.timezone(entry.get("timezone", "UTC"), f"{path}.timezone")
        datasets[name] = FileDataset(name, glob, kind, types, timezone)
    return datasets


def _check_glob(glob: str, path: str) -> str:
    """A pattern that stays inside the location."""
    glob = glob.strip()
    if glob.startswith("/") or "://" in glob or ".." in glob.split("/"):
        raise opt.invalid(path, "must be relative to the location and stay inside it")
    return glob


class _MissingColumns(Exception):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(", ".join(missing))
        self.missing = missing


class FileConnector(BaseConnector):
    kind = "file"

    def __init__(
        self,
        source_id: str,
        location: str,
        redactor: Redactor,
        datasets: dict[str, FileDataset],
    ) -> None:
        super().__init__(source_id, location, redactor)
        self._datasets = datasets
        self._fs: fsspec.AbstractFileSystem | None = None
        self._arrow_fs: pafs.FileSystem | None = None
        self._root = ""
        self._local = False

    # -- connecting ----------------------------------------------------------

    def _connect(self, resolved: str) -> None:
        protocol = fsspec.core.split_protocol(resolved)[0] or "file"
        started = time.perf_counter()
        try:
            fs, root = fsspec.core.url_to_fs(resolved, **_storage_options(protocol))
        except ImportError as exc:
            implementation = known_implementations.get(protocol, {}).get("class", "")
            driver = implementation.split(".")[0] or protocol
            raise self._error(
                SourceConfigError,
                f"no fsspec driver for {protocol}:// locations is installed (needs {driver})",
                cause=exc,
                protocol=protocol,
                driver=driver,
            ) from None
        except ValueError as exc:
            raise self._error(
                SourceConfigError,
                f"the location scheme {protocol!r} is not known",
                cause=exc,
                protocol=protocol,
            ) from None
        self._local = isinstance(fs, LocalFileSystem)
        try:
            info = fs.info(root)
        except FileNotFoundError:
            raise self._error(SourceUnavailable, "the location does not exist") from None
        except Exception as exc:
            raise self._translate(exc, None, f"opening source {self.source_id!r}") from None
        if self._local and info.get("type") != "directory":
            raise self._error(SourceConfigError, "the location is not a directory")
        self._fs = fs
        self._root = root.rstrip("/")
        self._arrow_fs = (
            pafs.LocalFileSystem() if self._local else pafs.PyFileSystem(pafs.FSSpecHandler(fs))
        )
        self._log.info(
            "connector opened",
            source=self.source_id,
            kind=self.kind,
            location=self.location,
            protocol=protocol,
            datasets=len(self._datasets),
            elapsed_ms=_elapsed_ms(started),
        )

    @property
    def _filesystem(self) -> fsspec.AbstractFileSystem:
        if self._fs is None:
            raise self._error(SourceError, "the connector is not connected")
        return self._fs

    # -- datasets ------------------------------------------------------------

    def _list_datasets(self) -> list[str]:
        return list(self._datasets)

    def _has_dataset(self, name: str) -> bool:
        return name in self._datasets

    def files(self, dataset: str) -> list[str]:
        """Names of a dataset's files relative to the location, in the order they are read."""
        self._check_open()
        if dataset not in self._datasets:
            raise self._unknown(dataset)
        try:
            return [self._relative(path) for path in self._match(self._datasets[dataset])]
        except SourceError:
            raise
        except Exception as exc:
            raise self._translate(exc, dataset, f"listing the files of {dataset!r}") from None

    def _match(self, spec: FileDataset) -> list[str]:
        started = time.perf_counter()
        found = self._filesystem.glob(f"{self._root}/{spec.glob}", detail=True)
        paths = sorted(name for name, info in found.items() if info.get("type") != "directory")
        self._log.debug(
            "files matched",
            source=self.source_id,
            dataset=spec.name,
            glob=spec.glob,
            count=len(paths),
            elapsed_ms=_elapsed_ms(started),
        )
        return paths

    def _relative(self, path: str) -> str:
        # Only the part below the location is ever shown: the location itself may
        # have been filled in from secrets.
        if path.startswith(self._root):
            return path[len(self._root) :].lstrip("/")
        return path.rsplit("/", 1)[-1]

    # -- reading -------------------------------------------------------------

    def _pieces(self, request: ReadRequest) -> Iterator[Piece]:
        spec = self._datasets[request.dataset]
        paths = self._match(spec)
        if not paths:
            raise self._error(
                SourceReadError,
                f"no file of dataset {spec.name!r} matches {spec.glob!r}",
                dataset=spec.name,
                glob=spec.glob,
            )
        readers = {"csv": self._read_csv, "parquet": self._read_parquet, "jsonl": self._read_jsonl}
        read = readers[spec.format]
        for index, path in enumerate(paths):
            name = self._relative(path)
            started = time.perf_counter()
            rows = 0
            try:
                for raw in read(spec, path, request):
                    table = self._finish(spec, raw, request, name)
                    rows += table.num_rows
                    yield Piece(table, name)
            except _MissingColumns as exc:
                if index == 0:
                    raise self._error(
                        SourceConfigError,
                        f"dataset {spec.name!r} has no column {exc}",
                        dataset=spec.name,
                        file=name,
                        missing_columns=exc.missing,
                    ) from None
                raise self._mismatch(
                    spec.name, Piece(pa.table({}), name), SchemaMismatch(exc.missing, [], {})
                ) from None
            except SourceError:
                raise
            except Exception as exc:
                raise self._translate(exc, spec.name, f"reading file {name!r}") from None
            self._log.debug(
                "file read",
                source=self.source_id,
                dataset=spec.name,
                file=name,
                rows=rows,
                elapsed_ms=_elapsed_ms(started),
            )

    def _finish(
        self, spec: FileDataset, table: pa.Table, request: ReadRequest, name: str
    ) -> pa.Table:
        needed = request.needed_columns()
        if needed is not None:
            missing = [column for column in needed if column not in table.column_names]
            if missing:
                raise _MissingColumns(missing)
        elif request.cursor_column is not None and request.cursor_column not in table.column_names:
            raise _MissingColumns([request.cursor_column])
        if spec.format == "parquet" and spec.types:
            table = self._apply_types(spec, table, name)
        table = normalize_table(
            table,
            timezone=spec.timezone,
            detect_iso_text=spec.format != "parquet",
            keep_text=spec.types.keys(),
        )
        if request.cursor_column is not None and request.since is not None:
            try:
                table = filter_after(table, request.cursor_column, request.since)
            except ValueError as exc:
                raise self._error(
                    SourceConfigError,
                    f"since cannot be compared with column {request.cursor_column!r}: {exc}",
                    dataset=spec.name,
                ) from None
        if request.columns is not None:
            table = table.select(list(request.columns))
        return table

    def _apply_types(self, spec: FileDataset, table: pa.Table, name: str) -> pa.Table:
        for column, kind in spec.types.items():
            if column not in table.column_names:
                continue
            index = table.column_names.index(column)
            try:
                table = table.set_column(index, column, table.column(index).cast(kind))
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
                raise self._error(
                    SourceReadError,
                    f"column {column!r} of {name!r} cannot be read as {kind}",
                    dataset=spec.name,
                    file=name,
                    columns=[column],
                    cause=exc,
                ) from None
        return table

    def _read_csv(self, spec: FileDataset, path: str, request: ReadRequest) -> Iterator[pa.Table]:
        convert = pacsv.ConvertOptions(
            column_types=spec.types or None,
            # An empty field is a missing value, so a timestamp column with gaps is
            # still recognised as timestamps.
            strings_can_be_null=True,
        )
        if self._local:
            yield pacsv.read_csv(path, convert_options=convert)
            return
        with self._filesystem.open(path, "rb") as handle:
            table = pacsv.read_csv(handle, convert_options=convert)
        yield table

    def _read_jsonl(self, spec: FileDataset, path: str, request: ReadRequest) -> Iterator[pa.Table]:
        data: Any = path if self._local else self._filesystem.cat_file(path)

        def source() -> Any:
            return data if self._local else pa.BufferReader(data)

        declared = [pa.field(column, kind) for column, kind in spec.types.items()]
        parse = pajson.ParseOptions(
            explicit_schema=pa.schema(declared) if declared else None,
            unexpected_field_behavior="infer",
        )
        table = pajson.read_json(source(), parse_options=parse)
        inferred = [
            f.name
            for f in table.schema
            if pa.types.is_timestamp(f.type) and f.name not in spec.types
        ]
        if inferred:
            # The JSON reader converts zoned timestamps to UTC and then drops the
            # zone, which cannot be told apart from local time; those columns are
            # read again as text and parsed with their zones.
            as_text = [*declared, *(pa.field(column, pa.string()) for column in inferred)]
            parse = pajson.ParseOptions(
                explicit_schema=pa.schema(as_text), unexpected_field_behavior="infer"
            )
            table = pajson.read_json(source(), parse_options=parse)
        yield table

    def _read_parquet(
        self, spec: FileDataset, path: str, request: ReadRequest
    ) -> Iterator[pa.Table]:
        dataset = pads.dataset(path, filesystem=self._arrow_fs, format="parquet")
        names = dataset.schema.names
        needed = request.needed_columns()
        wanted = list(needed) if needed is not None else None
        missing = [column for column in (wanted or ()) if column not in names]
        if request.cursor_column is not None and request.cursor_column not in names:
            missing.append(request.cursor_column)
        if missing:
            raise _MissingColumns(list(dict.fromkeys(missing)))
        condition = None
        if request.cursor_column is not None and request.since is not None:
            condition = self._pushdown(
                spec, dataset.schema.field(request.cursor_column), request.since
            )
        scanner = dataset.scanner(columns=wanted, filter=condition, batch_size=BATCH_ROWS)
        yielded = False
        for batch in scanner.to_batches():
            yielded = True
            yield pa.Table.from_batches([batch])
        if not yielded:
            yield scanner.projected_schema.empty_table()

    def _pushdown(self, spec: FileDataset, column: pa.Field, since: Any) -> pads.Expression | None:
        """A filter the Parquet reader applies to row groups and rows.

        It is deliberately a little wider than `since`: the exact comparison is
        made after normalisation, when every timestamp is in UTC. Widening by a
        day keeps rows in a daylight-saving overlap and rows lost to rounding
        into a coarser unit.
        """
        kind = column.type
        try:
            if pa.types.is_timestamp(kind):
                moment = as_utc_datetime(since) - timedelta(days=1)
                if kind.tz is None:
                    value = pa.scalar(local_naive(moment, spec.timezone), type=pa.timestamp("us"))
                else:
                    value = pa.scalar(moment, type=pa.timestamp("us", tz=kind.tz))
                return pads.field(column.name) > value.cast(kind, safe=False)
            if pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_string(kind):
                return pads.field(column.name) > pa.scalar(since).cast(kind)
        except (ValueError, TypeError, pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
            self._log.debug(
                "filter not pushed into the Parquet reader",
                source=self.source_id,
                dataset=spec.name,
                column=column.name,
                reason=str(exc).splitlines()[0],
            )
        return None

    # -- errors --------------------------------------------------------------

    def _classify(self, exc: BaseException) -> type[SourceError]:
        return classify_error(exc)

    def _detail_for(self, exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status == 429:
            return {"retry_after_s": _retry_after(exc)}
        return {}


def classify_error(exc: BaseException) -> type[SourceError]:
    if isinstance(exc, aiohttp.ClientResponseError):
        if exc.status in (401, 403):
            return SourceAuthFailed
        if exc.status == 429:
            return SourceRateLimited
        if exc.status >= 500:
            return SourceUnavailable
        return SourceReadError
    if isinstance(exc, PermissionError):
        return SourceAuthFailed
    if isinstance(exc, FileNotFoundError):
        return SourceReadError
    if isinstance(exc, aiohttp.ClientError | ConnectionError | TimeoutError):
        return SourceUnavailable
    return SourceReadError


def _retry_after(exc: aiohttp.ClientResponseError) -> float | None:
    value = (exc.headers or {}).get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _storage_options(protocol: str) -> dict[str, Any]:
    if protocol in ("http", "https"):
        return {
            # A source changes between reads; a cached instance or listing would hide that.
            "skip_instance_cache": True,
            "use_listings_cache": False,
            "client_kwargs": {
                "timeout": aiohttp.ClientTimeout(
                    total=None, sock_connect=CONNECT_TIMEOUT_S, sock_read=READ_TIMEOUT_S
                )
            },
        }
    return {}


def open_files(
    source_id: str,
    location: str,
    resolved: str,
    datasets: dict[str, FileDataset],
    redactor: Redactor,
) -> FileConnector:
    connector = FileConnector(source_id, location, redactor, datasets)
    connector._connect(resolved)
    return connector
