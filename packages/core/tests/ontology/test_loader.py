from pathlib import Path

from dta_core.ontology import load_ontology, parse_ontology
from dta_core.schema import Field, Kind, Ontology, Role

FIXTURES = Path(__file__).parent / "fixtures"
META_TYPES = ["TwinArtifact", "TwinSource", "TwinSyncRun"]


def test_types_are_concrete_meta_first_then_by_name(water):
    names = [t.name for t in water.types]
    assert names == [
        *META_TYPES,
        "Junction",
        "Pipe",
        "Pump",
        "Reading",
        "Reservoir",
        "Sensor",
        "Tank",
        "Valve",
    ]
    assert [t.meta for t in water.types] == [True] * 3 + [False] * 8
    # Abstract classes, mixins and value types get no table.
    for name in ("Node", "Link", "Located", "PumpCurvePoint"):
        assert not water.has_type(name)


def test_abstract_classes_map_to_concrete_descendants(water):
    assert water.abstract == {
        "Link": ("Pipe", "Pump", "Valve"),
        "Node": ("Junction", "Reservoir", "Tank"),
    }
    assert water.concrete("Node") == ("Junction", "Reservoir", "Tank")
    assert water.concrete("Sensor") == ("Sensor",)


def test_tables_scopes_and_ancestors(water):
    assert water.type("Junction").table == "junction"
    assert water.type("TwinSyncRun").table == "dta_sync_run"
    assert water.type("Junction").ancestors == ("Node",)  # the mixin is flattened, not an ancestor
    assert water.type("Tank").scope == "node"
    assert water.type("Valve").scope == "link"
    assert water.type("Reading").scope == "reading"
    assert water.type("TwinSource").scope == "twin_source"


def test_fields_are_induced_without_uid_in_root_first_order(water):
    junction = water.type("Junction")
    assert [f.name for f in junction.fields] == [
        "name",  # top-level slot used by Node
        "elevation",
        "x",  # from the Located mixin
        "y",
        "base_demand",
        "demand",
        "pressure",
    ]
    assert not junction.has_field("uid")
    pipe = water.type("Pipe")
    assert [f.name for f in pipe.fields][:3] == ["start_node", "end_node", "status"]


def test_field_mapping(water):
    junction, tank, sensor = water.type("Junction"), water.type("Tank"), water.type("Sensor")
    assert junction.field("name") == Field(
        "name",
        Kind.string,
        required=True,
        pattern="^[A-Za-z0-9 _-]+$",
        description="Human-readable name.",
    )
    assert junction.field("elevation") == Field("elevation", Kind.float, required=True, unit="m")
    assert (
        junction.field("base_demand").minimum == 0 and junction.field("base_demand").unit == "L/s"
    )
    assert junction.field("demand").role is Role.state
    assert junction.field("pressure").role is Role.derived
    assert junction.field("x").role is Role.static

    curve = tank.field("volume_curve")
    assert curve.kind is Kind.float and curve.multivalued and curve.stored_as_json

    assert sensor.field("measures") == Field("measures", Kind.ref, required=True, target="Node")
    assert sensor.field("quantity") == Field(
        "quantity", Kind.enum, required=True, target="Quantity"
    )
    assert sensor.field("installed_on").kind is Kind.date
    assert sensor.field("active").kind is Kind.boolean and sensor.field("active").required
    assert sensor.field("sample_interval") == Field(
        "sample_interval", Kind.integer, minimum=1, unit="s"
    )
    assert sensor.field("tags") == Field("tags", Kind.string, multivalued=True)
    assert sensor.field("notes").long_text and sensor.field("notes").kind is Kind.string
    assert sensor.field("serial").pattern == "^SN-[0-9]{6}$"

    diameter = water.type("Pipe").field("diameter")
    assert (diameter.minimum, diameter.maximum, diameter.unit) == (0, 5000, "mm")
    assert water.type("Reading").field("observed_at").kind is Kind.datetime


def test_inlined_structure_is_json_and_not_an_object_type(water):
    curve = water.type("Pump").field("curve")
    assert curve.kind is Kind.json and curve.stored_as_json
    assert curve.multivalued is False and curve.target is None


def test_links_events_and_checks(water):
    for name in ("Pipe", "Pump", "Valve"):
        assert water.type(name).link == ("start_node", "end_node")
    assert [t.name for t in water.link_types()] == ["Pipe", "Pump", "Valve"]
    assert water.type("Sensor").link is None
    assert water.type("Pipe").field("start_node").target == "Node"

    assert water.type("Reading").event and not water.type("Sensor").event
    assert water.type("TwinSyncRun").event

    assert water.type("Tank").checks == (
        ("bounds_ordered", "min_level <= max_level"),
        ("level_within_bounds", "level IS NULL OR (level >= min_level AND level <= max_level)"),
    )
    assert water.type("Junction").checks == ()


def test_enums_and_source(water, water_path):
    assert water.enums["ValveType"] == ("prv", "psv", "fcv", "tcv")
    assert water.enums["TwinRunStatus"] == ("running", "succeeded", "failed")
    assert water.source == water_path.read_text(encoding="utf-8")


def test_meta_model_types_are_merged(water):
    run = water.type("TwinSyncRun")
    assert run.meta and run.event
    assert run.field("status") == Field("status", Kind.enum, required=True, target="TwinRunStatus")
    assert run.field("sources") == Field(
        "sources", Kind.string, multivalued=True, description="Ids of the sources read in this run."
    )
    assert water.type("TwinSource").field("last_error").long_text
    assert water.type("TwinSource").field("status").role is Role.state
    assert water.type("TwinArtifact").field("sha256").pattern == "^[0-9a-f]{64}$"


def test_json_round_trip(water):
    again = Ontology.from_json(water.to_json())
    assert again == water and again.hash == water.hash


def test_loading_is_deterministic(water_path, water):
    assert load_ontology(water_path).hash == water.hash
    assert parse_ontology(water.source).ontology == water


def test_meta_model_ranges_do_not_follow_the_domain_default(water_document, write_schema):
    water_document["default_range"] = "integer"
    for body in water_document["classes"].values():
        for slot in body.get("attributes", {}).values():
            slot.setdefault("range", "string")
    water_document["slots"]["name"]["range"] = "string"
    ontology = load_ontology(write_schema(water_document))
    assert ontology.type("TwinSource").field("connector").kind is Kind.string


def test_spike_fixture_loads():
    ontology = load_ontology(FIXTURES / "spike_fixture.yaml")
    assert [t.name for t in ontology.domain_types()] == ["Asset", "Feeds", "Sensor", "Site"]
    feeds = ontology.type("Feeds")
    assert feeds.link == ("src", "dst") and feeds.field("src").target == "Asset"
    assert ontology.type("Asset").field("capacity").unit == "kW"
    assert ontology.type("Asset").field("status").target == "AssetStatus"
    assert ontology.enums["AssetStatus"] == ("ok", "degraded", "down")
    assert ontology.abstract == {}
