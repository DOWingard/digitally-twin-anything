"""The API answers as its OpenAPI document says, for generated requests to every operation
that reads.

Only GET operations are exercised: the operations that write (starting a sync run) would
start work in the twin under test with every generated request. They are covered by the
tests of the sync routes, which check their answers against the same models.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import schemathesis
from api_testkit import WaterHistory
from compute_testkit import World
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


schema = schemathesis.pytest.from_fixture("api_schema").include(method="GET")


@schema.parametrize()
@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
def test_api_conforms_to_its_document(case: schemathesis.Case, auth: dict[str, str]) -> None:
    case.call_and_validate(headers=auth, checks=CHECKS)


COMPUTED = "^/api/v1/(functions|models|questions|scenarios|search)"


@pytest.fixture(scope="module")
def computed_schema(world: World, evaluated: dict[str, Any]) -> Iterator[BaseSchema]:
    """The document over the computed twin, whose functions, models, questions and one
    scenario exist: an application of its own on the twin the served one holds open."""
    made = world.post(
        "/scenarios",
        json={"name": "for the contract", "horizon": "PT1H", "models": [KNOWN_MODEL]},
    )
    assert made.status_code in (200, 201), made.text
    KNOWN["/api/v1/scenarios/{id}"] = made.json()["data"]["id"]
    KNOWN["/api/v1/scenarios/{id}/compare"] = made.json()["data"]["id"]
    app = create_app(world.settings, world.twin)
    loaded = schemathesis.openapi.from_dict(build_openapi(app))
    loaded.app = app
    with TestClient(app):
        yield loaded


KNOWN_MODEL = "twin_model:whatif/plant"
# A path whose one parameter names a record: the record every other generated request is
# sent for, so that answers about records that exist are checked too.
KNOWN = {
    "/api/v1/functions/{id}": "tank_levels",
    "/api/v1/models/{uid}": "twin_model:level/naive",
    "/api/v1/models/{uid}/backtests": "twin_model:level/naive",
    "/api/v1/questions/{id}": "level_ahead",
}
computed = schemathesis.pytest.from_fixture("computed_schema").include(
    method="GET", path_regex=COMPUTED
)
_sent = {"count": 0}


@computed.parametrize()
@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
def test_the_computed_twin_answers_as_its_document_says(
    case: schemathesis.Case, auth: dict[str, str]
) -> None:
    known = KNOWN.get(case.operation.path)
    _sent["count"] += 1
    if known is not None and _sent["count"] % 2 == 0:
        name = next(iter(case.path_parameters))
        case.path_parameters[name] = known
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
        "/api/v1/objects",
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
        "/api/v1/lifecycle/jobs",
        "/api/v1/lifecycle/jobs/{id}",
        "/api/v1/sync/runs",
        "/api/v1/sync/runs/{uid}",
        "/api/v1/sync/runs/{uid}/rejects",
        "/api/v1/sync/sources",
        "/api/v1/artifacts",
        "/api/v1/artifacts/{sha256}",
        "/api/v1/lifecycle/register",
        "/api/v1/functions",
        "/api/v1/functions/{id}",
        "/api/v1/functions/{id}/run",
        "/api/v1/models",
        "/api/v1/models/evaluate",
        "/api/v1/models/{uid}",
        "/api/v1/models/{uid}/backtests",
        "/api/v1/models/{uid}/predict",
        "/api/v1/questions",
        "/api/v1/questions/measure",
        "/api/v1/questions/{id}",
        "/api/v1/questions/{id}/answer",
        "/api/v1/scenarios",
        "/api/v1/scenarios/{id}",
        "/api/v1/scenarios/{id}/run",
        "/api/v1/scenarios/{id}/compare",
        "/api/v1/derived",
        "/api/v1/block/training-set",
        "/api/v1/search",
        "/api/v1/fidelity",
        "/api/v1/fidelity/checks",
        "/api/v1/fidelity/checks/{uid}",
        "/api/v1/fidelity/corrections",
        "/api/v1/fidelity/questions/{id}/errors",
        "/api/v1/fidelity/incidents",
        "/api/v1/fidelity/incidents/{id}",
        "/api/v1/fidelity/incidents/{id}/retry",
        "/api/v1/fidelity/incidents/{id}/resolve",
        "/api/v1/fidelity/signals",
        "/api/v1/fidelity/signals/{uid}/clear",
        "/api/v1/lifecycle/schedule",
        "/api/v1/lifecycle/notifications",
        "/api/v1/actions",
        "/api/v1/actions/{id}",
        "/api/v1/actions/{id}/propose",
        "/api/v1/actions/records",
        "/api/v1/actions/records/{uid}",
        "/api/v1/actions/records/{uid}/approve",
        "/api/v1/actions/records/{uid}/reject",
        "/api/v1/actions/reconcile",
        "/api/v1/control/loops",
        "/api/v1/control/loops/{id}",
        "/api/v1/control/loops/{id}/enable",
        "/api/v1/control/loops/{id}/disable",
        "/api/v1/control/loops/{id}/run",
    }
    proposed = document["paths"]["/api/v1/actions/{id}/propose"]["post"]
    assert {"200", "201", "409", "422", "503"} <= set(proposed["responses"])
    assert {"200", "202", "404", "409"} <= set(
        document["paths"]["/api/v1/control/loops/{id}/run"]["post"]["responses"]
    )
    assert "202" in document["paths"]["/api/v1/functions/{id}/run"]["post"]["responses"]
    assert {"actions", "loops"} <= set(document["components"]["schemas"]["Registered"]["properties"])
    for path, operations in document["paths"].items():
        if path.startswith(("/api/v1/actions", "/api/v1/control")):
            for operation in operations.values():
                assert operation.get("description"), path
    checked = document["paths"]["/api/v1/fidelity/checks"]["post"]
    assert {"200", "202", "409", "503"} <= set(checked["responses"])
    assert {"200", "202", "400", "404", "409"} <= set(
        document["paths"]["/api/v1/fidelity/corrections"]["post"]["responses"]
    )
    for step in ("retry", "resolve"):
        stepped = document["paths"][f"/api/v1/fidelity/incidents/{{id}}/{step}"]["post"]
        assert {"200", "400", "404", "409"} <= set(stepped["responses"])
        assert stepped["description"] and stepped["requestBody"]
    assert {"fidelity", "fidelity_detail"} <= set(
        document["components"]["schemas"]["TwinHealth"]["properties"]
    )
    for path, operations in document["paths"].items():
        if path.startswith(("/api/v1/fidelity", "/api/v1/lifecycle/schedule")):
            for operation in operations.values():
                assert operation.get("description"), path
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), (method, path)
            assert operation["responses"], (method, path)
    for path in ("/api/v1/models/evaluate", "/api/v1/questions/measure"):
        started = document["paths"][path]["post"]
        assert {"200", "202", "400", "404", "409"} <= set(started["responses"]), path
    run = document["paths"]["/api/v1/functions/{id}/run"]["post"]
    assert {"200", "400", "404", "409", "422", "500", "502", "503", "504"} <= set(run["responses"])
    written = document["paths"]["/api/v1/derived"]["post"]
    assert {"201", "400", "403", "404", "409", "413"} <= set(written["responses"])
    assert set(document["paths"]["/api/v1/scenarios/{id}"]) == {"get", "delete"}
    assert "410" in document["paths"]["/api/v1/scenarios/{id}/compare"]["get"]["responses"]
    assert "compute" in document["components"]["schemas"]["TwinHealth"]["properties"]
    kept = document["paths"]["/api/v1/artifacts"]["post"]
    assert {"200", "201", "400", "413"} <= set(kept["responses"])
    assert kept["requestBody"]["content"]["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    assert set(document["paths"]["/api/v1/sync/runs"]) == {"get", "post"}
    started = document["paths"]["/api/v1/sync/runs"]["post"]
    assert {"200", "202", "400", "409"} <= set(started["responses"])
    assert started["requestBody"]["content"]["application/json"]["schema"]
    health = document["components"]["schemas"]["TwinHealth"]["properties"]
    assert {"clock", "sync"} <= set(health)
    assert document["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert document["paths"]["/healthz"]["get"]["security"] == []
    for path, operations in document["paths"].items():
        for operation in operations.values():
            # A 422 is one the route states, never the framework's validation error.
            unusable = operation["responses"].get("422")
            if unusable is not None:
                assert set(unusable["content"]) == {"application/problem+json"}, path
            if path != "/healthz":
                problem = operation["responses"]["401"]["content"]
                assert "application/problem+json" in problem, path
