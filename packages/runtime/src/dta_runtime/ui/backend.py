"""What the interface keeps on the server: the API client and what it has read that never changes.

A version id names an immutable state of the twin, so the schema read at a
version id is kept (bounded) and read again only when that version is
evicted. The API's own OpenAPI document supplies the values of its
enumerated parameters, so the filters offer what the API accepts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from dta_core.log import Log

from ..settings import UiSettings
from .client import ApiClient, ApiProblem
from .paging import PagedReader
from .schema import Schema

log = Log("ui.backend")

SCHEMAS_KEPT = 32


class Backend:
    def __init__(self, settings: UiSettings, *, client: ApiClient | None = None) -> None:
        self.client = client or ApiClient(settings.api_url, settings.api_token)
        self.pages = PagedReader(self.client)
        self._schemas: OrderedDict[str, Schema] = OrderedDict()
        self._openapi: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def schema(self, version_id: str) -> Schema:
        """The ontology in force at a version, read once per version id."""
        with self._lock:
            found = self._schemas.get(version_id)
            if found is not None:
                self._schemas.move_to_end(version_id)
                return found
        started = time.perf_counter()
        document = self.client.get("/meta/schema", params={"at": version_id})
        schema = Schema.from_document(version_id, document)
        with self._lock:
            self._schemas[version_id] = schema
            while len(self._schemas) > SCHEMAS_KEPT:
                self._schemas.popitem(last=False)
        log.debug(
            "schema read",
            version=version_id,
            types=len(schema.types),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return schema

    def enum(self, route: str, parameter: str) -> list[str]:
        """Allowed values of a query parameter of a GET route, from the API's OpenAPI document.

        An empty list when the document does not enumerate them; the filter is
        then offered as free text.
        """
        document = self._openapi_document()
        operation = document.get("paths", {}).get(f"/api/v1{route}", {}).get("get", {})
        for entry in operation.get("parameters", []):
            if entry.get("name") == parameter:
                return _enum_values(document, entry.get("schema", {}))
        log.warn("the API does not describe a parameter", route=route, parameter=parameter)
        return []

    def _openapi_document(self) -> dict[str, Any]:
        with self._lock:
            if self._openapi is not None:
                return self._openapi
        document = self.client.get("/openapi.json")
        with self._lock:
            self._openapi = document
        log.info("API description read", paths=len(document.get("paths", {})))
        return document


def _enum_values(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = document.get("components", {}).get("schemas", {}).get(name, {})
    if "enum" in schema:
        return [str(value) for value in schema["enum"]]
    for option in schema.get("anyOf", []):
        found = _enum_values(document, option)
        if found:
            return found
    return []


__all__ = ["ApiProblem", "Backend"]
