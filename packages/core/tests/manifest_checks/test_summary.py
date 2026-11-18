import pytest
from dta_core.errors import DtaError
from dta_core.manifest import contract_hash, manifest_hash, parse_manifest
from dta_core.manifest.checks import OntologyTypes, read_ontology_types, render_summary

DATA = {
    "twin": {"id": "demo", "name": "Demo"},
    "contract": {
        "purpose": "Watch | count tanks",
        "tier": "live",
        "questions": {
            "q_b": {"text": "Second", "kind": "descriptive", "priority": 2},
            "q_a": {
                "text": "First\nline",
                "kind": "predictive",
                "priority": 1,
                "tolerance": {"metric": "rmse", "value": 0.5, "unit": "m"},
                "staleness": "PT90M",
                "provisional": True,
            },
        },
        "controls": {
            "valve": {
                "description": "The inlet valve of tank T1",
                "target": "Valve.state",
                "bounds": {"allowed": ["open", "shut"]},
            },
        },
        "notification": {"kind": "log"},
        "addons": {"mcp": True, "sdk_python": True},
    },
    "sources": {"s": {"description": "Logger", "connector": "file", "refresh_period": "PT1M"}},
    "sync": {"interval": "PT5M"},
}


def test_summary_is_deterministic_and_independent_of_key_order():
    manifest = parse_manifest(DATA)
    reordered = parse_manifest(dict(reversed(list(DATA.items()))))
    assert render_summary(manifest) == render_summary(manifest) == render_summary(reordered)


def test_summary_content():
    manifest = parse_manifest(DATA)
    text = render_summary(manifest, OntologyTypes(objects=("Tank", "Valve"), links=()))
    lines = text.splitlines()
    assert lines[0] == "# Contract summary: Demo (`demo`)"
    assert "| Purpose | Watch \\| count tanks |" in lines
    # Questions are ordered by priority, multi-line text is flattened.
    q_a = lines.index("| q_a | First line | predictive | 1 | rmse <= 0.5 m | 1 h 30 min | yes |")
    q_b = lines.index("| q_b | Second | descriptive | 2 | not set | not set | no |")
    assert q_a < q_b
    # The description says which object the control is delegated for.
    assert "| valve | The inlet valve of tank T1 | Valve.state | one of open, shut | not set |" in (
        lines
    )
    assert "| s | Logger | file | 1 min |" in lines
    assert "Sync interval: 5 min" in lines
    assert "| Object types | Tank, Valve |" in lines and "| Link types | none |" in lines
    assert "| Add-ons | mcp, sdk_python |" in lines
    assert "| Notification | log |" in lines
    assert f"| Manifest | `{manifest_hash(manifest)}` |" in lines
    assert f"| Contract | `{contract_hash(manifest)}` |" in lines
    assert "None." in text.split("## Objective")[1].split("##")[0]


def test_a_static_twin_reads_its_sources_once_and_steps_its_forecasts_on_the_interval():
    """The bike export's file has readings every 18 s, but it is read once; its count
    tolerance has the unit 1, which is a plain number."""
    data = {
        "twin": {"id": "cykelbo", "name": "Bikes"},
        "contract": {
            "tier": "static",
            "questions": {
                "q": {
                    "text": "Bikes in an hour",
                    "kind": "predictive",
                    "priority": 1,
                    "tolerance": {"metric": "mae", "value": 2, "unit": "1"},
                }
            },
        },
        "sources": {
            "export": {"description": "Export", "connector": "file", "refresh_period": "PT18S"}
        },
        "sync": {"interval": "PT1H"},
    }
    lines = render_summary(parse_manifest(data)).splitlines()
    assert "| q | Bikes in an hour | predictive | 1 | mae <= 2 | not set | no |" in lines
    assert "| export | Export | file | none: read once |" in lines
    assert "Time step of forecasts (sync.interval): 1 h" in lines


def test_ontology_types_split_objects_from_links(tmp_path):
    path = tmp_path / "domain.yaml"
    path.write_text(
        "id: https://example.org/d\nname: d\nclasses:\n"
        "  Tank: {}\n"
        "  Feeds:\n    annotations: {link_source: a, link_target: b}\n"
        "  Half:\n    annotations: {link_source: a}\n"
    )
    assert read_ontology_types(path) == OntologyTypes(objects=("Half", "Tank"), links=("Feeds",))


def test_unreadable_ontology_is_reported(tmp_path):
    path = tmp_path / "domain.yaml"
    path.write_text("classes: {A: {}}\n")  # no id or name
    with pytest.raises(DtaError) as caught:
        read_ontology_types(path)
    assert caught.value.code == "ontology.unreadable"
