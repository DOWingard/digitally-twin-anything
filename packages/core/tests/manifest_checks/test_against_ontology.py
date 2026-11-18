"""A manifest is checked against the types and properties of the ontology it names."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import GateInputs, ontology_findings
from dta_core.manifest.checks.against_ontology import CODES
from dta_core.ontology import load_ontology, type_signature

ONTOLOGY = load_ontology(
    Path(__file__).resolve().parents[1] / "ontology/fixtures/water_network.yaml"
)
SIGNATURE = type_signature(ONTOLOGY)

# Fits the water-network ontology exactly: every finding below comes from one change to it.
BASE: dict[str, Any] = {
    "twin": {"id": "water", "name": "Water"},
    "contract": {
        "tier": "static",
        "questions": {
            "q_level": {"targets": ["Tank.level"]},
            "q_height": {"targets": ["Node.elevation"]},
        },
        "boundary": {
            "state": ["Tank.level"],
            "observations": ["Tank.level", "Junction.demand"],
        },
    },
    "ontology": {"path": "ontology/water_network.yaml", "type_signature": SIGNATURE},
    "sources": {
        "registry": {
            "mappings": {
                "tanks": {
                    "dataset": "tank",
                    "object_type": "Tank",
                    "identity": ["tank_id"],
                    "properties": {
                        "name": {"column": "name"},
                        "elevation": {"column": "elevation_m", "unit": "m"},
                        "min_level": {"column": "min_level_m", "unit": "m"},
                        "max_level": {"column": "max_level_m", "unit": "m"},
                    },
                },
                "junctions": {
                    "dataset": "junction",
                    "object_type": "Junction",
                    "identity": ["junction_id"],
                    "properties": {
                        "name": {"column": "name"},
                        "elevation": {"column": "elevation_m", "unit": "m"},
                    },
                },
                "reservoirs": {
                    "dataset": "reservoir",
                    "object_type": "Reservoir",
                    "identity": ["reservoir_id"],
                    "properties": {
                        "name": {"column": "name"},
                        "elevation": {"column": "elevation_m", "unit": "m"},
                        "head": {"column": "head_m", "unit": "m"},
                    },
                },
            }
        },
        "scada": {
            "mappings": {
                "levels": {
                    "dataset": "readings",
                    "object_type": "Tank",
                    "identity": ["tank_id"],
                    "valid_time": "ts",
                    "properties": {"level": {"column": "level", "unit": "m"}},
                },
                "demands": {
                    "dataset": "demands",
                    "object_type": "Junction",
                    "identity": ["junction_id"],
                    "valid_time": "ts",
                    "properties": {"demand": {"column": "demand", "unit": "L/s"}},
                },
            }
        },
    },
}


def findings(change: Any = None) -> list[tuple[str, str, str]]:
    document = copy.deepcopy(BASE)
    if change is not None:
        change(document)
    manifest = parse_manifest(document, origin="test")
    found = ontology_findings(manifest, ONTOLOGY, SIGNATURE)
    assert {finding.code for finding in found} <= set(CODES)
    return [(finding.code, finding.severity.value, finding.path) for finding in found]


def test_a_manifest_that_fits_its_ontology_has_no_findings():
    assert findings() == []


def put(path: str, value: Any):
    def change(document: dict[str, Any]) -> None:
        node = document
        *parents, last = path.split("/")
        for part in parents:
            node = node[part]
        if value is None:
            del node[last]
        else:
            node[last] = value

    return change


TANKS = "sources/registry/mappings/tanks"
RESERVOIRS = "sources/registry/mappings/reservoirs"
LEVELS = "sources/scada/mappings/levels"

CASES = [
    (
        "signature",
        put("ontology/type_signature", "0" * 64),
        ("ontology.signature", "error", "ontology.type_signature"),
    ),
    (
        "target of an unknown type",
        put("contract/questions/q_level/targets", ["Basin.level"]),
        ("ontology.unknown_type", "error", "contract.questions.q_level.targets"),
    ),
    (
        "target with an unknown property",
        put("contract/questions/q_level/targets", ["Tank.depth"]),
        ("ontology.unknown_property", "error", "contract.questions.q_level.targets"),
    ),
    (
        "property that only some descendants of an abstract type have",
        put("contract/questions/q_height/targets", ["Node.level"]),
        ("ontology.unknown_property", "error", "contract.questions.q_height.targets"),
    ),
    (
        "boundary entry of an unknown type",
        put("contract/boundary/state", ["Basin.level"]),
        ("ontology.unknown_type", "error", "contract.boundary.state"),
    ),
    (
        "mapping onto an unknown type",
        put(f"{RESERVOIRS}/object_type", "Basin"),
        ("ontology.unknown_type", "error", "sources.registry.mappings.reservoirs.object_type"),
    ),
    (
        "mapping onto an abstract type",
        put(f"{RESERVOIRS}/object_type", "Node"),
        ("ontology.abstract_mapping", "error", "sources.registry.mappings.reservoirs.object_type"),
    ),
    (
        "mapping of an unknown property",
        put(f"{TANKS}/properties/depth", {"column": "depth"}),
        ("ontology.unknown_property", "error", "sources.registry.mappings.tanks.properties.depth"),
    ),
    (
        "required property no mapping fills",
        put(f"{TANKS}/properties/max_level", None),
        ("ontology.required_unmapped", "error", "sources.registry.mappings.tanks.properties"),
    ),
    (
        "observed property no mapping fills",
        put("contract/boundary/observations", ["Tank.level", "Pump.speed"]),
        ("ontology.unmapped_observation", "error", "contract.boundary.observations"),
    ),
    (
        "fixed property mapped as a time series",
        put(f"{LEVELS}/properties/min_level", {"column": "floor", "unit": "m"}),
        ("ontology.role", "warning", "sources.scada.mappings.levels.properties.min_level"),
    ),
    (
        "changing property mapped without a time",
        put(f"{TANKS}/properties/level", {"column": "level", "unit": "m"}),
        ("ontology.role", "warning", "sources.registry.mappings.tanks.properties.level"),
    ),
    (
        "no source unit for a property that has one",
        put(f"{LEVELS}/properties/level", {"column": "level"}),
        ("ontology.unit", "warning", "sources.scada.mappings.levels.properties.level"),
    ),
    (
        "source unit for a property without one",
        put(f"{TANKS}/properties/name", {"column": "name", "unit": "m"}),
        ("ontology.unit", "warning", "sources.registry.mappings.tanks.properties.name"),
    ),
]


def function(entry: str, **params: Any):
    return put("functions", {"f": {"kind": "builtin", "entry": entry, "params": params}})


CASES += [
    (
        "function over a property of an unknown type",
        function("latest", property="Basin.level"),
        ("ontology.unknown_type", "error", "functions.f.params.property"),
    ),
    (
        "function over an unknown property",
        function("latest", property="Tank.depth"),
        ("ontology.unknown_property", "error", "functions.f.params.property"),
    ),
    (
        "threshold on a property that holds no numbers",
        function("below", property="Pipe.status", threshold=1),
        ("ontology.function_param", "error", "functions.f.params.property"),
    ),
    (
        "paths over a type that is not a link type",
        function("paths", edge_types=["Pipe", "Tank"]),
        ("ontology.function_param", "error", "functions.f.params.edge_types"),
    ),
    (
        "paths over an unknown type",
        function("paths", edge_types=["Duct"]),
        ("ontology.unknown_type", "error", "functions.f.params.edge_types"),
    ),
    (
        "link filter on a property the link type does not have",
        function("paths", edge_where=["Pump.length:gt:1"]),
        ("ontology.unknown_property", "error", "functions.f.params.edge_where"),
    ),
    (
        "link filter on a type that is not a link type",
        function("paths", edge_where=["Tank.level:gt:1"]),
        ("ontology.function_param", "error", "functions.f.params.edge_where"),
    ),
    (
        "weight that one of the link types followed does not have",
        function("paths", edge_types=["Pipe", "Pump"], weight="length"),
        ("ontology.unknown_property", "error", "functions.f.params.weight"),
    ),
    (
        "weight that holds no numbers",
        function("paths", edge_types=["Pipe"], weight="status"),
        ("ontology.function_param", "error", "functions.f.params.weight"),
    ),
    (
        "grouping by a property the type does not have",
        function("aggregate", property="Tank.level", op="mean", group_by="zone"),
        ("ontology.unknown_property", "error", "functions.f.params.group_by"),
    ),
    (
        "object filter on a property the type does not have",
        function("latest", property="Tank.level", where=["depth:gt:1"]),
        ("ontology.unknown_property", "error", "functions.f.params.where"),
    ),
    (
        "object filter whose value is not of the property's kind",
        function("latest", property="Tank.level", where=["level:gt:high"]),
        ("ontology.function_param", "error", "functions.f.params.where"),
    ),
    (
        "object filter listing a value that is not of the property's kind",
        function("latest", property="Tank.level", where=["level:in:1,2,x"]),
        ("ontology.function_param", "error", "functions.f.params.where"),
    ),
    (
        "link filter whose value is not of the property's kind",
        function("paths", edge_where=["Pipe.length:lt:short"]),
        ("ontology.function_param", "error", "functions.f.params.edge_where"),
    ),
]


def test_a_clause_the_twin_cannot_read_is_left_to_the_manifest_s_own_checks():
    """Read as a property name, `mean_demand > 0` was reported as an unknown property."""
    assert findings(function("latest", property="Tank.level", where=["level > 0"])) == []


def test_functions_that_fit_the_ontology_have_no_findings():
    fitting = {
        "f_latest": {"kind": "builtin", "entry": "latest"},
        "f_low": {
            "kind": "builtin",
            "entry": "below",
            "params": {"property": "Junction.pressure", "threshold": 20, "unit": "m"},
        },
        "f_paths": {
            "kind": "builtin",
            "entry": "paths",
            "params": {
                "edge_types": ["Link"],
                "edge_where": ["Link.status:eq:open", "Pipe.length:lt:500"],
                "weight": "status",
            },
        },
        "f_height": {
            "kind": "builtin",
            "entry": "aggregate",
            "params": {
                "property": "Node.elevation",
                "op": "max",
                "group_by": "name",
                "where": ["uid:like:asset%", "elevation:gt:0"],
            },
        },
        # Judged by the manifest's own checks, not here.
        "f_unknown": {"kind": "builtin", "entry": "newest", "params": {"property": "X.y"}},
        "f_code": {"kind": "registered", "entry": "plant:simulate", "params": {"property": "X.y"}},
    }
    found = findings(put("functions", fitting))
    # `status` is on every link type but holds no numbers: the one finding.
    assert found == [("ontology.function_param", "error", "functions.f_paths.params.weight")] * 3


@pytest.mark.parametrize(
    ("change", "expected"), [case[1:] for case in CASES], ids=[c[0] for c in CASES]
)
def test_each_misfit_is_reported_once_under_its_code(change, expected):
    assert findings(change) == [expected]


def test_an_unconfirmed_signature_is_a_gap_not_a_finding():
    assert findings(put("ontology/type_signature", None)) == []


def test_an_observed_property_of_an_abstract_type_counts_when_a_descendant_is_mapped():
    def change(document: dict[str, Any]) -> None:
        document["contract"]["boundary"]["observations"] = ["Node.elevation"]

    assert findings(change) == []


def test_gates_count_findings_against_the_ontology():
    document = copy.deepcopy(BASE)
    document["sources"]["registry"]["mappings"]["reservoirs"]["object_type"] = "Basin"
    manifest = parse_manifest(document, origin="test")
    extra = ontology_findings(manifest, ONTOLOGY, SIGNATURE)
    without = GateInputs(manifest)
    with_extra = GateInputs(manifest, extra_findings=extra)
    assert len(with_extra.errors) == len(without.errors) + 1
    assert "ontology.unknown_type" in {finding.code for finding in with_extra.findings}
    paths = [(finding.path, finding.code) for finding in with_extra.findings]
    assert paths == sorted(paths)
