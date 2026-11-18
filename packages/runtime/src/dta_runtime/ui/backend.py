"""What the interface keeps on the server: the API client and what it has read that never changes.

A version id names an immutable state of the twin, so the schema read at a
version id is kept (bounded) and read again only when that version is
evicted. The API's own OpenAPI document supplies the values of its
enumerated parameters, so the filters offer what the API accepts.

What a grid of the page reads its rows from, when those rows were worked out
for one request (an answer to a question, a comparison of scenarios), is held
here too, a few at a time, under the key the grid carries in its id: the rows
stay on the server and only the block a grid asks for reaches the browser.
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
from .parts import LIVE_BRANCH, branch_of
from .schema import Schema

log = Log("ui.backend")

SCHEMAS_KEPT = 32
HELD_KEPT = 32


class Held[T]:
    """The latest few things worked out for the page, by key; the oldest is let go."""

    def __init__(self, what: str, kept: int = HELD_KEPT) -> None:
        self._what = what
        self._kept = kept
        self._items: OrderedDict[str, T] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, item: T) -> None:
        with self._lock:
            self._items[key] = item
            self._items.move_to_end(key)
            while len(self._items) > self._kept:
                gone, _ = self._items.popitem(last=False)
                log.debug("a held result was let go", what=self._what, key=gone[:80])

    def get(self, key: str) -> T | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found


class Backend:
    def __init__(self, settings: UiSettings, *, client: ApiClient | None = None) -> None:
        self.client = client or ApiClient(settings.api_url, settings.api_token)
        self.pages = PagedReader(self.client)
        # Answers to questions and comparisons of scenarios, by the key of their grid.
        self.answers: Held[Any] = Held("answer")
        self.comparisons: Held[Any] = Held("comparison")
        # The manifest in force at a version of the live twin, by version id.
        self._manifests: Held[dict[str, Any]] = Held("manifest")
        self._schemas: OrderedDict[str, Schema] = OrderedDict()
        self._openapi: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def schema(self, version_id: str, branch: str = LIVE_BRANCH) -> Schema:
        """The ontology in force at a version, read once per version id. A version of a
        scenario is read on its own branch."""
        with self._lock:
            found = self._schemas.get(version_id)
            if found is not None:
                self._schemas.move_to_end(version_id)
                return found
        started = time.perf_counter()
        document = self.client.get("/meta/schema", params={"at": version_id, "branch": branch})
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

    def schema_at(self, version: dict[str, Any]) -> Schema:
        """The ontology in force at a selected version, on its branch."""
        return self.schema(str(version["id"]), branch_of(version))

    def manifest(self, version_id: str) -> dict[str, Any]:
        """The manifest in force at a version of the live twin, read once per version id
        while it is held."""
        found = self._manifests.get(version_id)
        if found is not None:
            return found
        started = time.perf_counter()
        document = self.client.get("/meta/manifest", params={"at": version_id})
        manifest = dict(document.get("manifest") or {})
        self._manifests.put(version_id, manifest)
        log.debug(
            "manifest read",
            version=version_id,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return manifest

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
