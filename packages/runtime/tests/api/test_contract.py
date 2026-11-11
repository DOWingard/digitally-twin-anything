"""The API answers as its OpenAPI document says, for generated requests to every operation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import schemathesis
from api_testkit import WaterHistory
from dta_runtime.api import create_app
from dta_runtime.api.docs import build_openapi
from dta_runtime.settings import RuntimeSettings
from hypothesis import HealthCheck, settings
from schemathesis.checks import (
    content_type_conformance,
    not_a_server_error,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from schemathesis.schemas import BaseSchema
from starlette.testclient import TestClient

CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
    response_headers_conformance,
]


@pytest.fixture(scope="module")
def api_schema(
    runtime_settings: RuntimeSettings, water_history: WaterHistory
) -> Iterator[BaseSchema]:
    # Schemathesis runs the application's lifespan around every call, so it gets
    # an application of its own, serving a twin that its lifespan leaves open.
    app = create_app(runtime_settings, water_history.twin)
    loaded = schemathesis.openapi.from_dict(build_openapi(app))
    loaded.app = app
    with TestClient(app):
        yield loaded


schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
def test_api_conforms_to_its_document(case: schemathesis.Case, auth: dict[str, str]) -> None:
    case.call_and_validate(headers=auth, checks=CHECKS)


def test_the_document_describes_every_route(app_client: TestClient, auth: dict[str, str]) -> None:
    document = app_client.get("/api/v1/openapi.json", headers=auth).json()
    assert set(document["paths"]) == {
        "/healthz",
        "/api/v1/meta/health",
        "/api/v1/meta/manifest",
        "/api/v1/meta/schema",
        "/api/v1/meta/schema/linkml",
        "/api/v1/history/versions",
        "/api/v1/history/versions/{selector}",
        "/api/v1/history/diff",
        "/api/v1/history/branches",
        "/api/v1/objects/{type}",
        "/api/v1/objects/{type}/{uid}",
        "/api/v1/object/{uid}",
        "/api/v1/objects/{type}/{uid}/links",
        "/api/v1/objects/{type}/{uid}/history",
        "/api/v1/graph/neighbors",
        "/api/v1/graph/paths",
        "/api/v1/graph/subgraph",
        "/api/v1/graph/edges",
        "/api/v1/block/nodes/{type}",
        "/api/v1/block/edges/{edge_type}",
        "/api/v1/block/slice/{type}",
        "/api/v1/lifecycle/logs",
    }
    assert document["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert document["paths"]["/healthz"]["get"]["security"] == []
    for path, operations in document["paths"].items():
        for operation in operations.values():
            assert "422" not in operation["responses"], path
            if path != "/healthz":
                problem = operation["responses"]["401"]["content"]
                assert "application/problem+json" in problem, path
