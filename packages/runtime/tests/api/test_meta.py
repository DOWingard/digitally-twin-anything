"""The twin's identity, manifest and schema."""

from __future__ import annotations

import yaml
from api_testkit import HEAD_SEQ, MIGRATION_SEQ, SCENARIO, Api, WaterHistory, grow_ontology
from dta_core.ontology import parse_ontology, type_signature


def test_health(api: Api, water_history: WaterHistory, water_text: str) -> None:
    body = api.ok("/api/v1/meta/health")
    assert body["twin_id"] == "water"
    assert body["name"] == "Water network"
    assert body["tier"] == "static"
    assert body["head"]["seq"] == HEAD_SEQ
    assert body["head"]["id"] == water_history.head.id
    assert body["versions"] == HEAD_SEQ + 1
    assert body["fidelity"] == "unknown"
    assert body["manifest"]["revision"] == 2
    grown = parse_ontology(grow_ontology(water_text)).ontology
    assert body["type_signature"] == type_signature(grown)
    assert body["ontology_hash"] == water_history.twin.ontology.hash


def test_health_of_a_branch(api: Api, water_history: WaterHistory) -> None:
    response = api.get("/api/v1/meta/health", params={"branch": SCENARIO})
    assert response.status_code == 200
    assert response.json()["head"]["id"] == water_history.scenario.id
    assert response.headers["x-dta-version-branch"] == SCENARIO


def test_manifest_as_of_a_version(api: Api) -> None:
    first = api.ok("/api/v1/meta/manifest", params={"at": "#5"})
    assert first["revision"] == 1
    assert first["manifest"]["twin"]["id"] == "water"
    assert first["version"]["seq"] == 5
    latest = api.ok("/api/v1/meta/manifest")
    assert latest["revision"] == 2
    assert latest["manifest_hash"] != first["manifest_hash"]


def test_manifest_before_registration_is_404(api: Api) -> None:
    response = api.get("/api/v1/meta/manifest", params={"at": "#0"})
    assert response.status_code == 404
    assert response.json()["code"] == "twin.not_registered"


def test_schema_follows_the_migration(api: Api) -> None:
    before = api.ok("/api/v1/meta/schema", params={"at": f"#{MIGRATION_SEQ - 1}"})
    after = api.ok("/api/v1/meta/schema")
    pipe_before = next(t for t in before["types"] if t["name"] == "Pipe")
    pipe_after = next(t for t in after["types"] if t["name"] == "Pipe")
    assert "material" not in {p["name"] for p in pipe_before["properties"]}
    assert "material" in {p["name"] for p in pipe_after["properties"]}
    assert pipe_after["kind"] == "link"
    assert pipe_after["link"] == {
        "source": "start_node",
        "target": "end_node",
        "source_type": "Node",
        "target_type": "Node",
    }
    assert set(after["abstract"]["Node"]) == {"Junction", "Tank", "Reservoir"}
    edges = {e["name"]: e for e in after["edge_types"]}
    assert {"Pipe", "Pump", "Valve", "Sensor.measures", "Reading.sensor"} <= set(edges)
    assert edges["Sensor.measures"]["kind"] == "reference"
    assert edges["Sensor.measures"]["target_types"] == ["Junction", "Reservoir", "Tank"]
    assert after["version"]["seq"] == HEAD_SEQ
    assert after["hash"] != before["hash"]
    elevation = next(
        p
        for p in next(t for t in after["types"] if t["name"] == "Junction")["properties"]
        if p["name"] == "elevation"
    )
    assert elevation["unit"] == "m" and elevation["required"] is True


def test_schema_as_linkml(api: Api, water_text: str) -> None:
    response = api.get("/api/v1/meta/schema/linkml", params={"at": "#3"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.headers["x-dta-version-seq"] == "3"
    assert response.text == water_text
    document = yaml.safe_load(api.get("/api/v1/meta/schema/linkml").text)
    assert "material" in document["classes"]["Pipe"]["attributes"]
