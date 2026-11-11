"""What every connector to a data source provides, whatever kind of source it is.

A connector reads and never writes. It hands data back as Arrow tables, so
profiling and sync work on columns and are the same for a database, a folder
of files and an HTTP interface.

A source's address is kept in the manifest with `${NAME}` placeholders in
place of anything secret. The placeholders are filled from the process
environment only at the moment a connector is opened; the filled-in address
is never logged, the placeholder form may be.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .config import require_env
from .errors import DtaError

if TYPE_CHECKING:
    import pyarrow as pa

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Largest table a connector yields from `read`; larger datasets come in several.
BATCH_ROWS = 50_000


class SourceError(DtaError):
    """Base class of every error a connector raises."""

    code = "source.error"


class SourceUnavailable(SourceError):
    """The source cannot be reached or did not answer in time. Worth retrying."""

    code = "source.unavailable"


class SourceAuthFailed(SourceError):
    """The source refused the credentials it was given."""

    code = "source.auth_failed"


class SourceRateLimited(SourceError):
    """The source asked for fewer requests; `detail["retry_after_s"]` says how long to wait."""

    code = "source.rate_limited"


class SourceConfigError(SourceError):
    """The source's entry in the manifest cannot be used as written."""

    code = "source.invalid_config"


class UnknownDataset(SourceError):
    """The source has no dataset of that name."""

    code = "source.unknown_dataset"


class SourceReadError(SourceError):
    """The source answered, but what it returned cannot be read as a table."""

    code = "source.read_failed"


def placeholders(text: str) -> list[str]:
    """Names of the variables a text refers to, in order of first appearance."""
    seen: dict[str, None] = {}
    for name in PLACEHOLDER.findall(text):
        seen.setdefault(name)
    return list(seen)


def resolve_placeholders(text: str, env: Mapping[str, str]) -> str:
    """Fill in every `${NAME}`; raises naming every variable that is missing or empty."""
    values = require_env(placeholders(text), env)
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


@runtime_checkable
class Connector(Protocol):
    """A read-only handle on one source. Use as a context manager, or call `close`."""

    source_id: str

    def datasets(self) -> list[str]:
        """Names of the datasets the source offers, sorted."""
        ...

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        """Rows of a dataset as tables of at most `BATCH_ROWS` rows, all with one schema.

        `columns` keeps only those columns. With `cursor_column` and `since`,
        only rows whose cursor value is greater than `since` are returned,
        filtered at the source where it can be. `limit` stops after that many
        rows. A dataset with no rows yields one empty table carrying the schema.

        Every timestamp column comes back as microseconds in UTC. Decimal
        columns come back as 64-bit floats and dictionary-encoded text as text.
        """
        ...

    def close(self) -> None:
        """Release connections and temporary files. Safe to call twice."""
        ...

    def __enter__(self) -> Connector: ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class TimeSource(Protocol):
    """A connector whose source tells the time.

    A twin whose clock is a source's clock reads `now` here: the time of the
    system the data comes from, which for a simulated or replayed system is
    not the time on the wall.
    """

    def now(self) -> datetime:
        """The source's current time, with an offset; a `SourceError` when it cannot be read."""
        ...
