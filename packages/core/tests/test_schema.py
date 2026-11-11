import pytest
from dta_core.schema import Field, Kind, ObjectType, Ontology, Role, make_uid, snake_case


def _ontology():
    junction = ObjectType(
        "Junction",
        (Field("elevation", Kind.float, required=True, unit="m", minimum=0),),
        ancestors=("Node",),
    )
    tank = ObjectType(
        "Tank",
        (
            Field("level", Kind.float, role=Role.state, unit="m"),
            Field("curve", Kind.float, multivalued=True),
        ),
        ancestors=("Node",),
        checks=(("level_positive", "level >= 0"),),
    )
    pipe = ObjectType(
        "Pipe",
        (
            Field("start_node", Kind.ref, True, target="Node"),
            Field("end_node", Kind.ref, True, target="Node"),
        ),
        ancestors=("Link",),
        link=("start_node", "end_node"),
    )
    run = ObjectType(
        "TwinSyncRun", (Field("status", Kind.enum, True, target="TwinRunStatus"),), meta=True
    )
    return Ontology(
        types=(junction, tank, pipe, run),
        abstract={"Node": ("Junction", "Tank"), "Link": ("Pipe",)},
        enums={"TwinRunStatus": ("running", "succeeded", "failed")},
        source="classes: {}\n",
    )


def test_names_tables_and_scopes():
    assert (
        snake_case("PumpCurvePoint") == "pump_curve_point"
        and snake_case("HTTPSource") == "http_source"
    )
    ontology = _ontology()
    assert (
        ontology.type("Tank").table == "tank"
        and ontology.type("TwinSyncRun").table == "dta_sync_run"
    )
    assert ontology.type("Tank").scope == "node" and ontology.type("Pipe").scope == "link"
    assert make_uid(ontology.type("Junction"), "10") == "node:10"
    with pytest.raises(ValueError):
        make_uid(ontology.type("Junction"), "x" * 200)


def test_concrete_targets_of_a_reference():
    ontology = _ontology()
    assert ontology.concrete("Node") == ("Junction", "Tank")
    assert ontology.concrete("Tank") == ("Tank",)
    with pytest.raises(KeyError):
        ontology.concrete("Nothing")
    assert [t.name for t in ontology.link_types()] == ["Pipe"]
    assert [t.name for t in ontology.domain_types()] == ["Junction", "Tank", "Pipe"]
    assert ontology.type("Tank").field("curve").stored_as_json


def test_json_round_trip_keeps_equality_and_hash():
    ontology = _ontology()
    again = Ontology.from_json(ontology.to_json())
    assert again == ontology and again.hash == ontology.hash
    changed = Ontology(ontology.types[:-1], ontology.abstract, ontology.enums, ontology.source)
    assert changed.hash != ontology.hash
