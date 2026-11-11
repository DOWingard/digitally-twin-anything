"""Each rejected construct is reported with its rule code, class and slot."""

import copy

import pytest
from dta_core.errors import DtaError
from dta_core.ontology import INVALID, UNREADABLE, OntologyError, load_ontology

UID = {"identifier": True, "required": True}
BASE = {
    "id": "https://example.org/case",
    "name": "case",
    "prefixes": {"linkml": "https://w3id.org/linkml/"},
    "default_range": "string",
    "imports": ["linkml:types"],
    "classes": {
        "Node": {"abstract": True, "attributes": {"uid": dict(UID)}},
        "Junction": {
            "is_a": "Node",
            "attributes": {"elevation": {"range": "float"}, "label": {}},
        },
        "Pipe": {
            "annotations": {"link_source": "start_node", "link_target": "end_node"},
            "attributes": {
                "uid": dict(UID),
                "start_node": {"range": "Node", "required": True},
                "end_node": {"range": "Node", "required": True},
                "label": {},
            },
        },
    },
    "enums": {"Status": {"permissible_values": {"on": {}, "off": {}}}},
}


def _classes(document):
    return document["classes"]


def _attrs(document, cls):
    return document["classes"][cls]["attributes"]


def _set(path, value):
    """A mutation setting a nested key of the document."""

    def apply(document):
        target = document
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value

    return apply


def _add_class(name, body):
    return _set(("classes", name), body)


def _junction_attr(name, body):
    return _set(("classes", "Junction", "attributes", name), body)


def _pipe_annotations(**annotations):
    return _set(("classes", "Pipe", "annotations"), annotations)


def _junction_check(expression, name="check_it"):
    return _set(("classes", "Junction", "annotations"), {f"dta_check_{name}": expression})


def _remove_pipe_required(document):
    _attrs(document, "Pipe")["end_node"]["required"] = False


def _forbidden_import(document):
    document["imports"] = ["linkml:types", "other_schema"]


def _value_type_with_reference(document):
    _classes(document)["Point"] = {
        "attributes": {"node": {"range": "Node"}, "x": {"range": "float"}}
    }
    _attrs(document, "Junction")["points"] = {
        "range": "Point",
        "multivalued": True,
        "inlined_as_list": True,
    }


def _reference_without_identifier(document):
    # Without a declared inlining, a class lacking uid is an object type missing its
    # identifier, never silently a JSON value.
    _classes(document)["Gauge"] = {"attributes": {"v": {"range": "float"}}}
    _attrs(document, "Junction")["gauge"] = {"range": "Gauge"}


def _domain_is_a_meta(document):
    _classes(document)["Feed"] = {"is_a": "TwinSource", "attributes": {"rate": {"range": "float"}}}


def _is_a_mixin(document):
    _classes(document)["Located"] = {"mixin": True, "attributes": {"x": {"range": "float"}}}
    _classes(document)["Hydrant"] = {"is_a": "Located", "attributes": {"uid": dict(UID)}}


CASES = [
    # (rule, class, slot, mutation)
    ("identifier.missing", "Gauge", None, _add_class("Gauge", {"attributes": {"v": {}}})),
    (
        "identifier.missing",
        "Gauge",
        "uid",
        _add_class("Gauge", {"attributes": {"uid": {"required": True}}}),
    ),
    (
        "identifier.name",
        "Gauge",
        "id",
        _add_class("Gauge", {"attributes": {"id": {"identifier": True}}}),
    ),
    (
        "identifier.range",
        "Gauge",
        "uid",
        _add_class("Gauge", {"attributes": {"uid": {**UID, "range": "integer"}}}),
    ),
    ("name.class", "gauge_type", None, _add_class("gauge_type", {"attributes": {"uid": UID}})),
    ("name.slot", "Junction", "Elevation2", _junction_attr("Elevation2", {"range": "float"})),
    ("name.reserved", "TwinGauge", None, _add_class("TwinGauge", {"attributes": {"uid": UID}})),
    ("name.redefined", "TwinSource", None, _add_class("TwinSource", {"attributes": {"uid": UID}})),
    (
        "name.redefined",
        "TwinRunStatus",
        None,
        _set(("enums", "TwinRunStatus"), {"permissible_values": {"x": {}}}),
    ),
    (
        "name.duplicate",
        "Junction",
        None,
        _set(("enums", "Junction"), {"permissible_values": {"x": {}}}),
    ),
    ("import.forbidden", None, None, _forbidden_import),
    (
        "reference.multivalued",
        "Junction",
        "neighbours",
        _junction_attr("neighbours", {"range": "Node", "multivalued": True}),
    ),
    (
        "link.missing",
        "Pipe",
        "finish",
        _pipe_annotations(link_source="start_node", link_target="finish"),
    ),
    (
        "link.not_reference",
        "Pipe",
        "label",
        _pipe_annotations(link_source="label", link_target="end_node"),
    ),
    ("link.not_required", "Pipe", "end_node", _remove_pipe_required),
    ("link.incomplete", "Pipe", None, _pipe_annotations(link_source="start_node")),
    (
        "link.same_slot",
        "Pipe",
        "start_node",
        _pipe_annotations(link_source="start_node", link_target="start_node"),
    ),
    (
        "reference.target",
        "Junction",
        "tagged_by",
        lambda d: (
            _add_class("Tagged", {"mixin": True, "attributes": {"uid": UID}})(d),
            _junction_attr("tagged_by", {"range": "Tagged"})(d),
        ),
    ),
    ("reference.target", "Junction", "gauge", _reference_without_identifier),
    ("identifier.missing", "Gauge", None, _reference_without_identifier),
    (
        "reference.abstract_empty",
        "Junction",
        "area",
        lambda d: (
            _add_class("Area", {"abstract": True, "attributes": {"uid": UID}})(d),
            _junction_attr("area", {"range": "Area"})(d),
        ),
    ),
    ("reference.in_value", "Point", "node", _value_type_with_reference),
    ("range.unsupported", "Junction", "opened", _junction_attr("opened", {"range": "time"})),
    ("range.unsupported", "Junction", "kind", _junction_attr("kind", {"range": "Nothing"})),
    (
        "range.unsupported",
        "Junction",
        "share",
        lambda d: (
            _set(("types", "Percent"), {"typeof": "float", "minimum_value": 0})(d),
            _junction_attr("share", {"range": "Percent"})(d),
        ),
    ),
    (
        "range.unsupported",
        "Junction",
        "either",
        _junction_attr("either", {"any_of": [{"range": "float"}, {"range": "string"}]}),
    ),
    (
        "range.unsupported",
        "Junction",
        "place",
        lambda d: (
            _add_class("Located", {"mixin": True, "attributes": {"x": {"range": "float"}}})(d),
            _junction_attr("place", {"range": "Located", "inlined": True})(d),
        ),
    ),
    (
        "role.unknown",
        "Junction",
        "level",
        _junction_attr("level", {"range": "float", "annotations": {"dta_role": "dynamic"}}),
    ),
    ("check.syntax", "Junction", None, _junction_check("elevation > 0; DROP TABLE junction")),
    ("check.syntax", "Junction", None, _junction_check("elevation > 0 -- trailing comment")),
    ("check.syntax", "Junction", None, _junction_check('label = "x"')),
    ("check.syntax", "Junction", None, _junction_check("(elevation > 0")),
    ("check.identifier", "Junction", None, _junction_check("height > 0")),
    ("check.identifier", "Junction", None, _junction_check("ABS(elevation) > 0")),
    ("check.name", "Junction", None, _junction_check("elevation > 0", name="Positive")),
    (
        "annotation.unknown",
        "Junction",
        "level",
        _junction_attr("level", {"range": "float", "annotations": {"dta_rol": "state"}}),
    ),
    (
        "annotation.unknown",
        "Junction",
        None,
        _set(("classes", "Junction", "annotations"), {"dta_events": True}),
    ),
    (
        "annotation.value",
        "Junction",
        "label",
        _junction_attr("label", {"annotations": {"dta_long": "maybe"}}),
    ),
    (
        "annotation.value",
        "Junction",
        "elevation",
        _junction_attr("elevation", {"range": "float", "annotations": {"dta_long": True}}),
    ),
    (
        "annotation.value",
        "Junction",
        None,
        _set(("classes", "Junction", "annotations"), {"dta_event": "often"}),
    ),
    ("bound.invalid", "Junction", "label", _junction_attr("label", {"minimum_value": 1})),
    (
        "bound.invalid",
        "Junction",
        "elevation",
        _junction_attr("elevation", {"range": "float", "minimum_value": 5, "maximum_value": 1}),
    ),
    (
        "bound.invalid",
        "Junction",
        "curve",
        _junction_attr("curve", {"range": "float", "multivalued": True, "minimum_value": 0}),
    ),
    ("pattern.invalid", "Junction", "label", _junction_attr("label", {"pattern": "(["})),
    (
        "pattern.invalid",
        "Junction",
        "elevation",
        _junction_attr("elevation", {"range": "float", "pattern": "^1"}),
    ),
    (
        "unit.invalid",
        "Junction",
        "elevation",
        _junction_attr("elevation", {"range": "float", "unit": {"symbol": "m"}}),
    ),
    ("enum.empty", "Empty", None, _set(("enums", "Empty"), {"permissible_values": {}})),
    ("parent.invalid", "Feed", None, _domain_is_a_meta),
    ("parent.invalid", "Hydrant", None, _is_a_mixin),
    (
        "constraint.unsupported",
        "Junction",
        "label",
        _junction_attr("label", {"ifabsent": "string(x)"}),
    ),
    (
        "constraint.unsupported",
        "Junction",
        None,
        _set(("classes", "Junction", "unique_keys"), {"by_label": {"unique_key_slots": ["label"]}}),
    ),
]


def _ids():
    seen: dict[str, int] = {}
    ids = []
    for rule, *_ in CASES:
        seen[rule] = seen.get(rule, 0) + 1
        ids.append(f"{rule}-{seen[rule]}")
    return ids


def test_base_document_is_valid(write_schema):
    ontology = load_ontology(write_schema(copy.deepcopy(BASE)))
    assert ontology.type("Pipe").link == ("start_node", "end_node")


@pytest.mark.parametrize(("rule", "cls", "slot", "mutate"), CASES, ids=_ids())
def test_rejected_construct(write_schema, rule, cls, slot, mutate):
    document = copy.deepcopy(BASE)
    mutate(document)
    with pytest.raises(OntologyError) as caught:
        load_ontology(write_schema(document))
    error = caught.value
    assert isinstance(error, DtaError) and error.code == INVALID
    located = [(p.rule, p.cls, p.slot) for p in error.problems]
    assert (rule, cls, slot) in located, located
    assert {"rule": rule, "class": cls, "slot": slot} in [
        {k: v for k, v in entry.items() if k != "message"} for entry in error.detail["problems"]
    ]


def test_every_problem_is_reported_at_once(write_schema):
    document = copy.deepcopy(BASE)
    document["imports"].append("elsewhere")
    _classes(document)["TwinGauge"] = {"attributes": {"uid": UID}}
    _attrs(document, "Junction")["Bad"] = {"range": "float"}
    _attrs(document, "Junction")["neighbours"] = {"range": "Node", "multivalued": True}
    _attrs(document, "Junction")["level"] = {"range": "float", "annotations": {"dta_role": "x"}}
    _classes(document)["Junction"]["annotations"] = {"dta_check_c": "nope > 0"}
    _classes(document)["Pipe"]["annotations"] = {"link_source": "start_node"}
    _classes(document)["Gauge"] = {"attributes": {"value": {"range": "float"}}}
    with pytest.raises(OntologyError) as caught:
        load_ontology(write_schema(document))
    assert caught.value.rules() >= {
        "import.forbidden",
        "name.reserved",
        "name.slot",
        "reference.multivalued",
        "role.unknown",
        "check.identifier",
        "link.incomplete",
        "identifier.missing",
    }
    assert len(caught.value.detail["problems"]) == len(caught.value.problems) >= 8


def test_inherited_problem_is_reported_once_where_declared(write_schema):
    document = copy.deepcopy(BASE)
    _attrs(document, "Node")["opened"] = {"range": "time"}
    _classes(document)["Tank"] = {"is_a": "Node", "attributes": {"level": {"range": "float"}}}
    with pytest.raises(OntologyError) as caught:
        load_ontology(write_schema(document))
    assert [(p.rule, p.cls, p.slot) for p in caught.value.problems] == [
        ("range.unsupported", "Node", "opened")
    ]


def test_missing_identifier_is_reported_on_the_topmost_class(write_schema):
    document = copy.deepcopy(BASE)
    del _attrs(document, "Node")["uid"]
    _classes(document)["Pipe"]["attributes"]["start_node"] = {"range": "Junction", "required": True}
    _classes(document)["Pipe"]["attributes"]["end_node"] = {"range": "Junction", "required": True}
    with pytest.raises(OntologyError) as caught:
        load_ontology(write_schema(document))
    assert ("identifier.missing", "Node") in [(p.rule, p.cls) for p in caught.value.problems]
    assert ("identifier.missing", "Junction") not in [
        (p.rule, p.cls) for p in caught.value.problems
    ]


@pytest.mark.parametrize(
    "text",
    [
        "classes: [unclosed",  # YAML syntax
        "- just\n- a list\n",  # not a mapping
        "name: x\nclasses: {}\n",  # no id
        "id: https://x.org/a\nname: a\nclasses:\n  A:\n    attributes:\n      x: {rnage: float}\n",
        "id: https://x.org/a\nname: a\nclasses:\n  A:\n    is_a: Missing\n",
        "id: https://x.org/a\nname: a\nclasses:\n  A: {is_a: B}\n  B: {is_a: A}\n",
    ],
    ids=["yaml", "not-mapping", "no-id", "unknown-key", "dangling-parent", "cycle"],
)
def test_unreadable_documents(tmp_path, text):
    path = tmp_path / "broken.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(OntologyError) as caught:
        load_ontology(path)
    assert caught.value.code == UNREADABLE
    assert caught.value.detail["reason"]


def test_missing_file_is_unreadable(tmp_path):
    with pytest.raises(OntologyError) as caught:
        load_ontology(tmp_path / "absent.yaml")
    assert caught.value.code == UNREADABLE
