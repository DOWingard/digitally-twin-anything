import os
import tempfile
import time
from datetime import UTC, date, datetime

import pytest
from dta_core.ontology import OntologyError, build_validator
from dta_core.ontology import validator as validator_module
from dta_core.schema import Ontology, RowValidator

JUNCTION = {
    "uid": "node:J1",
    "name": "J 1",
    "elevation": 12.5,
    "x": 1.0,
    "y": 2.0,
    "base_demand": 0.4,
    "demand": 0.5,
    "pressure": 30.0,
}
VALID = {
    "Junction": [JUNCTION],
    "Tank": [
        {
            "uid": "node:T1",
            "name": "T1",
            "elevation": 30,
            "min_level": 0,
            "max_level": 10,
            "level": 5,
            "volume_curve": [0, 10.5, 20],
        }
    ],
    "Pipe": [
        {
            "uid": "link:P1",
            "start_node": "node:J1",
            "end_node": "node:T1",
            "status": "open",
            "length": 100.0,
            "diameter": 150.0,
            "roughness": 100,
            "lined": True,
        }
    ],
    "Pump": [
        {
            "uid": "link:PU1",
            "start_node": "node:R1",
            "end_node": "node:J1",
            "curve": [{"flow": 0, "head": 50}, {"flow": 10, "head": 40}],
            "speed": 1.0,
        }
    ],
    "Sensor": [
        {
            "uid": "sensor:S1",
            "measures": "node:T1",
            "quantity": "level",
            "serial": "SN-000123",
            "installed_on": date(2024, 3, 1),
            "active": True,
            "sample_interval": 60,
            "tags": ["north", "critical"],
            "notes": "Replaced after the 2023 flood. " * 20,
        }
    ],
    "Reading": [
        {
            "uid": "reading:R1",
            "sensor": "sensor:S1",
            "observed_at": "2024-03-01T12:00:00Z",
            "value": 4.2,
            "quality": "good",
            "sequence": 1,
        },
        {
            "uid": "reading:R2",
            "sensor": "sensor:S1",
            "observed_at": datetime(2024, 3, 1, 12, 5, tzinfo=UTC),
            "value": 4.3,
        },
    ],
    "TwinSyncRun": [
        {
            "uid": "twin_sync_run:1",
            "started_at": datetime(2024, 3, 1, tzinfo=UTC),
            "status": "running",
            "sources": ["twin_source:scada"],
            "rows_read": 0,
        }
    ],
}


def _rules(violations):
    return [(v.rule, v.property) for v in violations]


def test_conforms_to_the_row_validator_protocol(water_validator):
    validator: RowValidator = water_validator
    assert validator.validate("Junction", [JUNCTION]) == []


@pytest.mark.parametrize("type_name", sorted(VALID))
def test_valid_rows_pass(water_validator, type_name):
    assert water_validator.validate(type_name, VALID[type_name]) == []


def _junction(**changes):
    row = {**JUNCTION, **changes}
    return {k: v for k, v in row.items() if v is not _ABSENT}


_ABSENT = object()


@pytest.mark.parametrize(
    ("type_name", "row", "rule", "prop"),
    [
        ("Junction", _junction(elevation="high"), "type", "elevation"),
        ("Junction", _junction(elevation=_ABSENT), "required", "elevation"),
        ("Junction", _junction(elevation=None), "required", "elevation"),
        ("Junction", _junction(name="J;1"), "pattern", "name"),
        ("Junction", _junction(base_demand=-0.1), "range", "base_demand"),
        ("Junction", _junction(colour="red"), "unknown_property", "colour"),
        ("Junction", _junction(uid="junction:J1"), "uid", "uid"),
        ("Junction", _junction(uid="node:"), "uid", "uid"),
        ("Junction", _junction(uid="node:" + "k" * 190), "uid", "uid"),
        ("Junction", _junction(uid=_ABSENT), "required", "uid"),
        ("Pipe", {**VALID["Pipe"][0], "status": "ajar"}, "enum", "status"),
        ("Pipe", {**VALID["Pipe"][0], "diameter": 6000}, "range", "diameter"),
        ("Pipe", {**VALID["Pipe"][0], "start_node": 5}, "type", "start_node"),
        ("Pipe", {**VALID["Pipe"][0], "lined": "perhaps"}, "type", "lined"),
        ("Tank", {**VALID["Tank"][0], "volume_curve": [1, "a"]}, "type", "volume_curve"),
        ("Tank", {**VALID["Tank"][0], "volume_curve": 3.0}, "type", "volume_curve"),
        ("Pump", {**VALID["Pump"][0], "curve": [{"flow": -1, "head": 2}]}, "range", "curve"),
        ("Pump", {**VALID["Pump"][0], "curve": [{"flow": 1}]}, "required", "curve"),
        (
            "Pump",
            {**VALID["Pump"][0], "curve": [{"flow": 1, "head": 2, "x": 0}]},
            "unknown_property",
            "curve",
        ),
        ("Sensor", {**VALID["Sensor"][0], "serial": "123"}, "pattern", "serial"),
        ("Sensor", {**VALID["Sensor"][0], "quantity": "colour"}, "enum", "quantity"),
        ("Sensor", {**VALID["Sensor"][0], "sample_interval": 0}, "range", "sample_interval"),
        ("Sensor", {**VALID["Sensor"][0], "sample_interval": 1.5}, "type", "sample_interval"),
        ("Sensor", {**VALID["Sensor"][0], "installed_on": "soon"}, "type", "installed_on"),
        ("Reading", {**VALID["Reading"][0], "observed_at": "yesterday"}, "type", "observed_at"),
        (
            "TwinArtifact",
            {
                "uid": "twin_artifact:a",
                "sha256": "xyz",
                "media_type": "text/plain",
                "size_bytes": 1,
                "location": "a",
            },
            "pattern",
            "sha256",
        ),
    ],
)
def test_each_rule_is_reported(water_validator, type_name, row, rule, prop):
    violations = water_validator.validate(type_name, [*VALID.get(type_name, [])[:1], row])
    assert (rule, prop) in _rules(violations), violations
    assert all(v.type == type_name for v in violations)


def test_violation_carries_row_uid_and_nested_path(water_validator):
    row = {**VALID["Pump"][0], "curve": [{"flow": 1, "head": 2}, {"flow": "lots", "head": 1}]}
    [violation] = water_validator.validate("Pump", [row])
    assert violation.uid == "link:PU1" and violation.property == "curve"
    assert violation.message.startswith("curve[1].flow:")


def test_unknown_and_abstract_types(water_validator):
    for name in ("Nope", "Node", "PumpCurvePoint", "Located"):
        violations = water_validator.validate(name, [JUNCTION])
        assert _rules(violations) == [("unknown_type", None)]
        assert violations[0].uid == "node:J1"


def test_bad_data_never_raises(water_validator):
    assert _rules(water_validator.validate("Junction", ["not a row"])) == [("type", None)]
    assert _rules(water_validator.validate("Junction", None)) == [("type", None)]
    weird = water_validator.validate("Junction", [_junction(name=object(), x=[1])])
    assert {v.property for v in weird} == {"name", "x"}
    assert water_validator.validate("Junction", []) == []


def test_partial_rows_check_only_present_properties(water_validator):
    update = {"uid": "node:J1", "demand": 0.7}
    assert water_validator.validate("Junction", [update], partial=True) == []
    full = water_validator.validate("Junction", [update])
    assert sorted(_rules(full)) == [("required", "elevation"), ("required", "name")]

    assert water_validator.validate("Junction", [{"demand": 1.0}], partial=True) == []
    assert _rules(
        water_validator.validate("Junction", [{"uid": "node:J1", "base_demand": -1}], partial=True)
    ) == [("range", "base_demand")]
    assert _rules(
        water_validator.validate("Junction", [{"uid": "node:J1", "colour": "red"}], partial=True)
    ) == [("unknown_property", "colour")]
    # Clearing a required property is still a violation.
    assert _rules(
        water_validator.validate("Junction", [{"uid": "node:J1", "elevation": None}], partial=True)
    ) == [("required", "elevation")]
    # A structured value that is present must be complete.
    assert _rules(
        water_validator.validate(
            "Pump", [{"uid": "link:PU1", "curve": [{"flow": 1}]}], partial=True
        )
    ) == [("required", "curve")]


def test_violations_are_in_row_order(water_validator):
    rows = [
        _junction(uid="node:A", elevation="x"),
        _junction(uid="junction:B"),
        _junction(uid="node:C", base_demand=-1),
    ]
    assert [v.uid for v in water_validator.validate("Junction", rows)] == [
        "node:A",
        "junction:B",
        "node:C",
    ]


def test_bulk_validation_is_fast(water_validator):
    rows = [
        {
            "uid": f"reading:{i}",
            "sensor": f"sensor:{i % 50}",
            "observed_at": datetime(2024, 1, 1, tzinfo=UTC),
            "value": i * 0.5,
            "quality": "good",
            "sequence": i,
        }
        for i in range(20_000)
    ]
    water_validator.validate("Reading", rows[:10])  # build the batch validator once
    started = time.perf_counter()
    violations = water_validator.validate("Reading", rows)
    elapsed = time.perf_counter() - started
    partial_started = time.perf_counter()
    water_validator.validate("Reading", [{"uid": r["uid"], "value": 1.0} for r in rows], True)
    partial_elapsed = time.perf_counter() - partial_started
    print(f"validated 20000 rows in {elapsed:.3f} s (partial {partial_elapsed:.3f} s)")
    assert violations == []
    assert elapsed < 2.0 and partial_elapsed < 2.0


# -- building and caching


def test_models_are_generated_by_linkml(water_validator):
    model = water_validator.model("Junction")
    assert model.__name__ == "Junction"
    assert {"uid", "name", "elevation", "base_demand"} <= set(model.model_fields)
    with pytest.raises(KeyError):
        water_validator.model("Node")


def test_disk_cache_is_reused(water, tmp_path, monkeypatch):
    cache = tmp_path / "models"
    monkeypatch.setattr(validator_module, "_MODULES", {})
    build_validator(water, cache_dir=cache)
    [cached] = list(cache.glob("models_*.py"))
    assert cached.name == f"models_{validator_module.cache_key(water)}.py"

    monkeypatch.setattr(validator_module, "_MODULES", {})

    def no_generation(_ontology):
        raise AssertionError("models should come from the cache")

    monkeypatch.setattr(validator_module, "_generate", no_generation)
    again = build_validator(water, cache_dir=cache)
    assert again.validate("Junction", [JUNCTION]) == []


def test_damaged_cache_is_regenerated(water, tmp_path, monkeypatch):
    cache = tmp_path / "models"
    monkeypatch.setattr(validator_module, "_MODULES", {})
    build_validator(water, cache_dir=cache)
    [cached] = list(cache.glob("models_*.py"))
    cached.write_text(cached.read_text() + "\nraise RuntimeError('tampered')\n")

    monkeypatch.setattr(validator_module, "_MODULES", {})
    rebuilt = build_validator(water, cache_dir=cache)
    assert rebuilt.validate("Junction", [JUNCTION]) == []
    assert "tampered" not in cached.read_text()


def test_shared_cache_directory_is_not_used(water, tmp_path, monkeypatch):
    cache = tmp_path / "shared"
    cache.mkdir()
    os.chmod(cache, 0o777)
    monkeypatch.setattr(validator_module, "_MODULES", {})
    validator = build_validator(water, cache_dir=cache)
    assert validator.validate("Junction", [JUNCTION]) == []
    assert list(cache.iterdir()) == []


def test_default_cache_is_a_private_temp_directory(water, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(validator_module, "_MODULES", {})
    build_validator(water)
    [root] = list(tmp_path.glob("dta-ontology-models-*"))
    assert list(root.glob("models_*.py"))
    assert root.stat().st_mode & 0o077 == 0


def test_cache_inside_the_package_is_refused(water, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(validator_module, "_MODULES", {})
    inside = validator_module._PACKAGE_DIR / "ontology" / "_cache_should_not_exist"
    build_validator(water, cache_dir=inside)
    assert not inside.exists()
    assert list(tmp_path.glob("dta-ontology-models-*/models_*.py"))


def test_build_and_cache_use_are_logged(water, tmp_path, monkeypatch, log_lines):
    monkeypatch.setattr(validator_module, "_MODULES", {})
    build_validator(water, cache_dir=tmp_path / "models")
    monkeypatch.setattr(validator_module, "_MODULES", {})
    build_validator(water, cache_dir=tmp_path / "models")
    lines = log_lines()
    generated = [line for line in lines if line["message"] == "validation models generated"]
    built = [line for line in lines if line["message"] == "validator built"]
    assert len(generated) == 1 and generated[0]["data"]["duration_ms"] >= 0
    assert [line["data"]["source"] for line in built] == ["generated", "cache"]
    assert all(line["context"] == "ontology.validator" for line in built)


def test_ontology_without_loadable_source_cannot_build(monkeypatch):
    monkeypatch.setattr(validator_module, "_MODULES", {})
    with pytest.raises(OntologyError):
        build_validator(Ontology(types=(), source="classes: [unclosed"))
