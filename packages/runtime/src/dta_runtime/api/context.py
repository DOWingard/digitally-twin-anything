"""What is known about the request being served, for the one log line written about it."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from dta_core.store import VersionRef


@dataclass
class RequestInfo:
    version: VersionRef | None = None


_current: contextvars.ContextVar[RequestInfo | None] = contextvars.ContextVar(
    "dta_api_request", default=None
)


def begin_request() -> tuple[RequestInfo, contextvars.Token[RequestInfo | None]]:
    info = RequestInfo()
    return info, _current.set(info)


def end_request(token: contextvars.Token[RequestInfo | None]) -> None:
    _current.reset(token)


def note_version(version: VersionRef) -> None:
    """Record the version a request reads.

    Handlers run in worker threads on a copy of the request's context; the
    object it holds is shared, so what is recorded here reaches the request log.
    """
    info = _current.get()
    if info is not None:
        info.version = version
