"""What the build's load refuses in views and mappings, validate refuses first, under the
load's own code `sync.mapping_invalid` and with its words.

The manifests are reduced copies of two that passed validate and then failed at the
build's load step."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks

CODE = "sync.mapping_invalid"

# A bike-sharing export: station readings joined to the weather in a view.
BIKES: dict[str, Any] = {
    "twin": {"id": "cykelbo", "name": "Bike stations"},
    "contract": {"tier": "static"},
    "sources": {
        "export": {
            "connector": "file",
            "location": "file://${CYKELBO_EXPORT_DIR}",
            "env": ["CYKELBO_EXPORT_DIR"],
            "options": {
                "datasets": {
                    "status": {"glob": "status/*.csv", "format": "csv"},
                    "weather": {"glob": "weather.csv", "format": "csv"},
                }
            },
            "views": {
                "v_weather": "SELECT 'city' AS city_id, t, temp_c FROM weather",
            },
            "mappings": {
                "weather": {
                    "dataset": "v_weather",
                    "object_type": "City",
                    "identity": ["city_id"],
                    "valid_time": "t",
                    "properties": {"temperature": {"column": "temp_c", "unit": "Cel"}},
                },
            },
        }
    },
    "env": {"CYKELBO_EXPORT_DIR": {"description": "Export folder", "secret": False}},
}

# A water utility: a register read whole, and telemetry over the same classes.
WATER: dict[str, Any] = {
    "twin": {"id": "vattholm", "name": "Water network"},
    "contract": {"tier": "live"},
    "sources": {
        "telemetry": {
            "connector": "http",
            "location": "${VATTHOLM_TELEMETRY_URL}",
            "env": ["VATTHOLM_TELEMETRY_URL"],
            "views": {
                "v_pressure": (
                    'SELECT s.asset_id, r."timestamp", r.value FROM "telemetry"."readings" r '
                    'JOIN "telemetry"."sensors" s ON s.sensor_id = r.sensor_id '
                    "WHERE s.quantity = 'pressure' AND s.asset_type = 'junction'"
                ),
            },
            "mappings": {
                "junction_pressure": {
                    "dataset": "v_pressure",
                    "object_type": "Junction",
                    "identity": ["asset_id"],
                    "valid_time": "timestamp",
                    "cursor": "timestamp",
                    "properties": {"pressure": {"column": "value", "unit": "m"}},
                },
            },
        },
        "registry": {
            "connector": "sql",
            "location": "postgresql://${VATTHOLM_REGISTRY_HOST}/registry",
            "env": ["VATTHOLM_REGISTRY_HOST"],
            "mappings": {
                "zones": {
                    "dataset": "zone",
                    "object_type": "Zone",
                    "identity": ["zone_id"],
                    "mode": "snapshot",
                    "filter": "decommissioned_at IS NULL",
                    "properties": {"name": {"column": "name"}},
                },
                "junctions": {
                    "dataset": "junction",
                    "object_type": "Junction",
                    "identity": ["junction_id"],
                    "mode": "snapshot",
                    "filter": "decommissioned_at IS NULL",
                    "properties": {"mean_demand": {"column": "mean_demand_lps", "unit": "L/s"}},
                },
            },
        },
    },
    "env": {
        "VATTHOLM_TELEMETRY_URL": {"description": "Telemetry address", "secret": False},
        "VATTHOLM_REGISTRY_HOST": {"description": "Register host", "secret": False},
    },
}


def _invalid(document: dict[str, Any]) -> list[tuple[str, str]]:
    findings = run_checks(parse_manifest(document))
    for finding in findings:
        if finding.code == CODE:
            assert finding.severity.value == "error"
    return [(f.path, f.message) for f in findings if f.code == CODE]


def test_a_view_naming_a_dataset_without_its_source_is_refused():
    assert _invalid(BIKES) == [
        (
            "sources.export.views.v_weather",
            'refers to weather without its source; write "<source>"."weather"',
        )
    ]


def test_the_view_as_it_was_mended_passes():
    document = copy.deepcopy(BIKES)
    document["sources"]["export"]["views"]["v_weather"] = (
        'SELECT \'city\' AS city_id, t, temp_c FROM "export"."weather"'
    )
    assert _invalid(document) == []


def test_a_view_naming_a_source_the_manifest_lacks_is_refused():
    document = copy.deepcopy(BIKES)
    document["sources"]["export"]["views"]["v_weather"] = 'SELECT * FROM "files"."weather"'
    assert _invalid(document) == [
        (
            "sources.export.views.v_weather",
            "refers to source 'files', which the manifest does not have",
        )
    ]


def test_snapshot_on_a_class_that_other_mappings_feed_is_refused():
    assert _invalid(WATER) == [
        (
            "sources.registry.mappings.junctions.mode",
            "snapshot needs to be the only mapping of Junction, which 2 mappings feed: an "
            "object absent from one of them may come from another",
        )
    ]


def test_the_register_as_it_was_mended_passes():
    document = copy.deepcopy(WATER)
    document["sources"]["registry"]["mappings"]["junctions"]["mode"] = "upsert"
    assert _invalid(document) == []


@pytest.mark.parametrize(
    ("mapping", "path", "message"),
    [
        (
            {"mode": "snapshot", "valid_time": "ts"},
            "sources.registry.mappings.zones.mode",
            "snapshot cannot be used with valid_time: rows absent from a time series are not "
            "deletions",
        ),
        (
            {"kind": "simulated"},
            "sources.registry.mappings.zones.kind",
            "simulated facts cannot be loaded from a source",
        ),
        (
            {"properties": {"name": {"column": "name", "expression": "upper(name)"}}},
            "sources.registry.mappings.zones.properties.name",
            "give exactly one of column and expression",
        ),
    ],
)
def test_other_refusals_of_the_load_are_found_by_validate(mapping, path, message):
    document = copy.deepcopy(WATER)
    document["sources"]["registry"]["mappings"]["junctions"]["mode"] = "upsert"
    document["sources"]["registry"]["mappings"]["zones"].update(mapping)
    assert _invalid(document) == [(path, message)]
