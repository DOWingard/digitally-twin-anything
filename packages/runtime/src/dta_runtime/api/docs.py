"""Pieces of the OpenAPI document shared by the routes, and its final touches."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .formats import ARROW_MEDIA, PARQUET_MEDIA
from .models import Problem
from .problems import PROBLEM_MEDIA

# Every route is declared with the full prefix: the request log reads the route
# template from the matched route, which keeps only the prefix it was declared with.
API_PREFIX = "/api/v1"
SECURITY_SCHEME = "bearer"
OPEN_PATHS = frozenset({"/healthz"})

_DESCRIPTIONS = {
    400: "The request is malformed: a parameter, selector, filter, cursor or limit",
    401: "No valid bearer token was given",
    404: "The version, branch, type, property or object does not exist",
    409: "The request conflicts with the state of the twin or of its jobs",
    413: "The body is larger than allowed",
    422: "The change breaks a constraint of the ontology",
    500: "The request failed on the server",
    503: "The store cannot be reached",
}

VERSION_HEADERS = {
    "x-dta-version-id": {
        "description": "Commit id of the version read",
        "schema": {"type": "string"},
    },
    "x-dta-version-seq": {"description": "Position of that version", "schema": {"type": "string"}},
    "x-dta-version-t": {
        "description": "Version time, ISO 8601 UTC",
        "schema": {"type": "string"},
    },
    "x-dta-version-branch": {"description": "Branch read", "schema": {"type": "string"}},
}
_BINARY = {"schema": {"type": "string", "format": "binary"}}


def errors(*statuses: int, notes: dict[int, str] | None = None) -> dict[int | str, dict[str, Any]]:
    """Error responses of a route; 401, 500 and 503 apply to every route. `notes` replaces
    the general description of a status with what it means on this route."""
    wanted = sorted({*statuses, 401, 500, 503})
    described = {**_DESCRIPTIONS, **(notes or {})}
    return {status: {"model": Problem, "description": described[status]} for status in wanted}


TOTAL_HEADER = "x-dta-total"


def tabular(*statuses: int, counted: bool = False) -> dict[int | str, dict[str, Any]]:
    """Responses of a route returning a table in JSON, Arrow or Parquet; `counted` for a
    listing that gives its total with `count=true`."""
    headers: dict[str, Any] = {
        **VERSION_HEADERS,
        "x-dta-next-cursor": {
            "description": "Cursor of the next page, for Arrow and Parquet",
            "schema": {"type": "string"},
        },
    }
    if counted:
        headers[TOTAL_HEADER] = {
            "description": "With `count=true`, in every format: rows of the whole listing, "
            "all pages",
            "schema": {"type": "string"},
        }
    return {
        200: {
            "description": "The table as JSON, an Arrow IPC stream or Parquet, chosen with "
            "`format` or `Accept`. Arrow and Parquet carry the version in headers and in the "
            "schema metadata (`dta:version`), and the next cursor in `x-dta-next-cursor`; "
            "columns holding JSON text are marked `dta:json` in their field metadata",
            "content": {ARROW_MEDIA: _BINARY, PARQUET_MEDIA: _BINARY},
            "headers": headers,
        },
        **errors(*statuses),
    }


def versioned(
    *statuses: int, notes: dict[int, str] | None = None
) -> dict[int | str, dict[str, Any]]:
    """Responses of a route returning JSON about one version."""
    return {200: {"headers": dict(VERSION_HEADERS)}, **errors(*statuses, notes=notes)}


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """The generated document with the bearer scheme on every operation, errors as
    problem documents, and no trace of FastAPI's own 422 validation response:
    invalid parameters are answered with 400."""
    if app.openapi_schema:
        return app.openapi_schema
    document = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = document.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    components["securitySchemes"] = {
        SECURITY_SCHEME: {
            "type": "http",
            "scheme": "bearer",
            "description": "The API token of the twin",
        }
    }
    document["security"] = [{SECURITY_SCHEME: []}]
    problem_ref = {"$ref": "#/components/schemas/Problem"}
    for path, operations in document.get("paths", {}).items():
        for operation in operations.values():
            responses = operation.get("responses", {})
            responses.pop("422", None)
            for status, response in responses.items():
                if status.startswith(("4", "5")):
                    response["content"] = {PROBLEM_MEDIA: {"schema": problem_ref}}
            if path in OPEN_PATHS:
                operation["security"] = []
                for status in ("401", "503"):
                    responses.pop(status, None)
    app.openapi_schema = document
    return document
