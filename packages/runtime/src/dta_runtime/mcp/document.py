"""The API's OpenAPI document as the adapter reads it, and which operations become tools.

The document is a function of the runtime's code alone: the routes do not depend on
the twin, its store or its token. The adapter runs from the same image as the API,
so it assembles the API's application here, never serves it, and asks it for its
document; the adapter needs no token to know what the API offers.

A tool's arguments and its result travel as JSON. An operation whose request body
or whose every success answer can only be binary (an artifact uploaded or fetched
as bytes) cannot be carried by a tool and is left out. Every other operation is a
tool named by its operation id; operations that also answer with Arrow or Parquet
tables are offered without the `format` parameter, and the adapter asks the API for
JSON on every call, so no tool ever answers with an Arrow or Parquet body.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dta_core.log import Log
from dta_core.store import StoreSettings

log = Log("mcp.document")

FORMAT_PARAMETER = "format"
_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_TEXT_MEDIA = frozenset({"application/json", "application/yaml", "application/x-yaml"})


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    # Why the operation is not a tool; None when it is one.
    excluded: str | None
    # Whether the API can also answer it with an Arrow or Parquet body.
    tabular: bool


def _carried_as_text(media: str) -> bool:
    media = media.split(";", 1)[0].strip().lower()
    return media in _TEXT_MEDIA or media.endswith("+json") or media.startswith("text/")


def _success_media(operation: Mapping[str, Any]) -> list[list[str]]:
    """Media types of each success answer; an empty list for an answer without a body."""
    answers = []
    for status, response in (operation.get("responses") or {}).items():
        if str(status).startswith("2"):
            answers.append(list(((response or {}).get("content") or {}).keys()))
    return answers


def excluded_reason(operation: Mapping[str, Any]) -> str | None:
    """Why an operation of the document is not offered as a tool, or None."""
    body = (operation.get("requestBody") or {}).get("content") or {}
    if body and not any(_carried_as_text(media) for media in body):
        return "its request body is binary only"
    answers = _success_media(operation)
    if answers and all(media and not any(_carried_as_text(m) for m in media) for media in answers):
        return "every success answer is binary only"
    return None


def _tabular(operation: Mapping[str, Any]) -> bool:
    names = {parameter.get("name") for parameter in operation.get("parameters") or []}
    media = {m for answer in _success_media(operation) for m in answer}
    return FORMAT_PARAMETER in names and any(not _carried_as_text(m) for m in media)


def operations(document: Mapping[str, Any]) -> Iterator[Operation]:
    """Every operation of the document, in its order, with whether it is a tool."""
    for path, item in (document.get("paths") or {}).items():
        for method, operation in (item or {}).items():
            if method.lower() not in _METHODS or not isinstance(operation, dict):
                continue
            identifier = operation.get("operationId")
            if not identifier:
                # Without an id an operation has no name a tool could carry.
                log.warn("operation without an id is not offered", method=method, path=path)
                continue
            yield Operation(
                operation_id=identifier,
                method=method.upper(),
                path=path,
                excluded=excluded_reason(operation),
                tabular=_tabular(operation),
            )


def tool_names(document: Mapping[str, Any]) -> list[str]:
    """The names of the tools the adapter offers for this document, in document order."""
    return [found.operation_id for found in operations(document) if found.excluded is None]


def api_document() -> dict[str, Any]:
    """The OpenAPI document the twin's API serves at `/api/v1/openapi.json`."""
    from ..api import create_app
    from ..api.docs import build_openapi
    from ..settings import RuntimeSettings

    started = time.perf_counter()
    # The application is assembled to read its routes and never served: nothing here
    # is used to reach a store, and the placeholder token is a random value nobody holds.
    settings = RuntimeSettings(
        store=StoreSettings(
            host="unused", port=3306, user="unused", password="", database="unused"
        ),
        api_token=secrets.token_urlsafe(32),
        artifacts_dir=Path("/nonexistent"),
        logs_dir=Path("/nonexistent"),
    )
    document = build_openapi(create_app(settings))
    log.debug(
        "API document assembled",
        paths=len(document.get("paths") or {}),
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return document
