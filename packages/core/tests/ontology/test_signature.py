import re

import pytest
from dta_core.ontology import load_ontology, type_signature
from dta_core.schema import Ontology


def _variant(water_document, write_schema, change):
    change(water_document)
    return type_signature(load_ontology(write_schema(water_document)))


def _attrs(document, cls):
    return document["classes"][cls]["attributes"]


def test_signature_is_a_stable_sha256(water, water_path):
    signature = type_signature(water)
    assert re.fullmatch(r"[0-9a-f]{64}", signature)
    assert type_signature(load_ontology(water_path)) == signature
    assert type_signature(Ontology.from_json(water.to_json())) == signature


def _add_property(d):
    _attrs(d, "Junction")["colour"] = {}


def _change_property(d):
    _attrs(d, "Pipe")["diameter"] = {"range": "integer", "maximum_value": 900}
    _attrs(d, "Sensor")["measures"]["range"] = "Tank"  # a reference, not a link end
    d["enums"]["ValveType"]["permissible_values"]["gpv"] = {}
    d["classes"]["Junction"]["annotations"] = {"dta_check_demand_positive": "demand >= 0"}


def _rename_link_slot(d):
    _attrs(d, "Link")["from_node"] = _attrs(d, "Link").pop("start_node")
    d["classes"]["Link"]["annotations"]["link_source"] = "from_node"


@pytest.mark.parametrize(
    "change",
    [_add_property, _change_property, _rename_link_slot],
    ids=["add-property", "change-properties", "rename-link-slot"],
)
def test_properties_do_not_change_the_signature(water, water_document, write_schema, change):
    assert _variant(water_document, write_schema, change) == type_signature(water)


def _add_type(d):
    d["classes"]["Hydrant"] = {"is_a": "Node", "attributes": {"flow": {"range": "float"}}}


def _remove_type(d):
    del d["classes"]["Valve"]


def _reparent(d):
    d["classes"]["Storage"] = {"is_a": "Node", "abstract": True}
    d["classes"]["Tank"]["is_a"] = "Storage"


def _detach(d):
    del d["classes"]["Reservoir"]["is_a"]
    _attrs(d, "Reservoir")["uid"] = {"identifier": True, "required": True}


def _repoint_link(d):
    _attrs(d, "Link")["end_node"]["range"] = "Junction"


def _add_abstract_type(d):
    d["classes"]["Asset"] = {
        "abstract": True,
        "attributes": {"uid": {"identifier": True, "required": True}},
    }


@pytest.mark.parametrize(
    "change",
    [_add_type, _remove_type, _reparent, _detach, _repoint_link, _add_abstract_type],
    ids=["add-type", "remove-type", "reparent", "detach", "repoint-link", "add-abstract"],
)
def test_type_changes_change_the_signature(water, water_document, write_schema, change):
    assert _variant(water_document, write_schema, change) != type_signature(water)
