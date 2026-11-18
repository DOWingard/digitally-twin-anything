"""`dta manifest validate` refuses what the build's load refuses, and nothing it accepts.

The toolkit has no SQL engine and judges views by their tokens; the load parses
them with DuckDB. Both are run here on the same manifests: every `sync.mapping_invalid`
finding of validate must be one of the load's problems, word for word, and for the
problems validate takes on (views, snapshot mappings, a mapping's kind, a property's
column or expression) it must find all of them.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks
from dta_core.ontology import load_ontology
from dta_runtime.sync.errors import MappingInvalid
from dta_runtime.sync.plan import build_plan

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY = load_ontology(ROOT / "packages/core/tests/ontology/fixtures/water_network.yaml")
CODE = "sync.mapping_invalid"

BASE: dict[str, Any] = {
    "twin": {"id": "water", "name": "Water"},
    "contract": {"tier": "static"},
    "sources": {
        "scada": {
            "connector": "sql",
            "location": "postgresql://${SCADA_HOST}/plant",
            "env": ["SCADA_HOST"],
            "views": {},
            "mappings": {
                "levels": {
                    "dataset": "v_levels",
                    "object_type": "Tank",
                    "identity": ["tank_id"],
                    "valid_time": "ts",
                    "properties": {"level": {"column": "level", "unit": "m"}},
                },
            },
        },
        "registry": {
            "connector": "sql",
            "location": "postgresql://${REGISTRY_HOST}/registry",
            "env": ["REGISTRY_HOST"],
            "mappings": {
                "junctions": {
                    "dataset": "junction",
                    "object_type": "Junction",
                    "identity": ["junction_id"],
                    "properties": {"elevation": {"column": "elevation_m", "unit": "m"}},
                },
                "tanks": {
                    "dataset": "tank",
                    "object_type": "Tank",
                    "identity": ["tank_id"],
                    "properties": {
                        "elevation": {"column": "elevation_m", "unit": "m"},
                        "min_level": {"column": "min_m", "unit": "m"},
                        "max_level": {"column": "max_m", "unit": "m"},
                    },
                },
            },
        },
    },
    "sync": {"backfill_interval": "PT1H"},
}

VIEWS = [
    'SELECT tank_id, ts, level FROM "scada"."readings"',
    "SELECT tank_id, ts, level FROM readings",
    'SELECT * FROM "files"."readings"',
    'SELECT * FROM "scada"."readings", tanks',
    'SELECT r.tank_id, r.ts, r.level FROM "scada"."readings" r '
    'JOIN "scada"."sensors" s ON s.sensor_id = r.sensor_id WHERE s.quantity = \'level\'',
    "SELECT r.tank_id, r.ts, r.level FROM scada.readings r LEFT JOIN sensors s USING (sensor_id)",
    'SELECT tank_id, ts, extract(epoch FROM ts) AS e, level FROM "scada"."readings"',
    'SELECT tank_id, ts, substring(tank_id FROM 2 FOR 3) AS t, level FROM "scada"."readings"',
    'SELECT tank_id, ts, level FROM "scada"."readings" WHERE level IS DISTINCT FROM 0',
    'WITH x AS (SELECT * FROM "scada"."readings") SELECT tank_id, ts, level FROM x',
    'SELECT * FROM "scada"."readings" WHERE tank_id IN (SELECT tank_id FROM tanks)',
    'SELECT * FROM (SELECT * FROM "scada"."readings") AS r',
    'FROM "scada"."readings" SELECT tank_id, ts, level',
    'SELECT tank_id, ts, level FROM "scada"."readings" WHERE note <> \'FROM weather\'',
    'SELECT * FROM "scada"."readings" UNION ALL SELECT * FROM archive',
    "SELECT * FROM c.scada.readings",
    'SELECT 1 AS tank_id; SELECT * FROM "scada"."readings"',
    "SELECT range AS tank_id, now() AS ts, 1.0 AS level FROM range(3)",
]

MAPPINGS = [
    ("registry", "tanks", {"mode": "snapshot"}),
    ("registry", "junctions", {"mode": "snapshot"}),
    ("scada", "levels", {"mode": "snapshot"}),
    ("registry", "junctions", {"kind": "simulated"}),
    ("registry", "junctions", {"properties": {"elevation": {"unit": "m"}}}),
    (
        "registry",
        "junctions",
        {"properties": {"elevation": {"column": "e", "expression": "e * 1", "unit": "m"}}},
    ),
]


def _validate(document: dict[str, Any]) -> set[tuple[str, str]]:
    findings = run_checks(parse_manifest(document))
    return {(f.path, f.message) for f in findings if f.code == CODE}


def _load(document: dict[str, Any]) -> set[tuple[str, str]]:
    try:
        build_plan(parse_manifest(document), ONTOLOGY)
    except MappingInvalid as exc:
        return {(p.where, p.message) for p in exc.problems if p.code == CODE}
    return set()


def _taken_on(where: str) -> bool:
    return ".views." in where or where.endswith((".mode", ".kind")) or ".properties." in where


@pytest.mark.parametrize("sql", VIEWS)
def test_a_view_is_judged_as_the_load_judges_it(sql: str) -> None:
    document = copy.deepcopy(BASE)
    document["sources"]["scada"]["views"]["v_levels"] = sql
    validated, loaded = _validate(document), _load(document)
    assert validated <= loaded
    assert validated == {found for found in loaded if _taken_on(found[0])}


@pytest.mark.parametrize(("source", "mapping", "change"), MAPPINGS)
def test_a_mapping_is_judged_as_the_load_judges_it(
    source: str, mapping: str, change: dict[str, Any]
) -> None:
    document = copy.deepcopy(BASE)
    document["sources"]["scada"]["views"]["v_levels"] = (
        'SELECT tank_id, ts, level FROM "scada"."readings"'
    )
    document["sources"][source]["mappings"][mapping].update(change)
    validated, loaded = _validate(document), _load(document)
    assert validated <= loaded
    assert validated == {found for found in loaded if _taken_on(found[0])}
