"""`paths` over the links of the story (the kit's docstring has the network)."""

from __future__ import annotations

from typing import Any

import pytest
from functions_testkit import FUNCTION, History, ask, link, node, refusal

J1_J3 = {"source": node("J1"), "target": node("J3")}


def _pipe(key: str, start: str, end: str) -> dict[str, Any]:
    return {"link_uid": link(key), "edge_type": "Pipe", "source": node(start), "target": node(end)}


DIRECT = {
    "hops": 1,
    "weight": None,
    "nodes": [node("J1"), node("J3")],
    "edges": [_pipe("P5", "J1", "J3")],
}
BY_J2 = {
    "hops": 2,
    "weight": None,
    "nodes": [node("J1"), node("J2"), node("J3")],
    "edges": [_pipe("P1", "J1", "J2"), _pipe("P2", "J2", "J3")],
}
BY_T1 = {
    "hops": 2,
    "weight": None,
    "nodes": [node("J1"), node("T1"), node("J3")],
    "edges": [_pipe("P3", "J1", "T1"), _pipe("P4", "T1", "J3")],
}


def test_the_path_with_the_fewest_links(story: History):
    answer = ask(story, "paths", J1_J3)
    assert answer.unit is None
    # One path unless more are asked for.
    assert answer.value == [DIRECT]


def test_at_most_k_paths_the_fewest_links_first(story: History):
    assert ask(story, "paths", {**J1_J3, "k": 2}).value == [DIRECT, BY_J2]
    assert ask(story, "paths", {**J1_J3, "k": 3}).value == [DIRECT, BY_J2, BY_T1]
    # There are no more than three.
    assert ask(story, "paths", {**J1_J3, "k": 10}).value == [DIRECT, BY_J2, BY_T1]
    assert ask(story, "paths", {**J1_J3, "k": 10, "max_depth": 1}).value == [DIRECT]


def test_only_links_that_pass_the_filter_are_followed(story: History):
    for clause in ("Pipe.status:eq:open", "Link.status:eq:open"):
        answer = ask(story, "paths", {**J1_J3, "k": 5, "edge_where": [clause]})
        assert answer.value == [BY_T1], clause
    closed = ask(story, "paths", {**J1_J3, "k": 5, "edge_where": ["Pipe.status:eq:closed"]})
    assert closed.value == [DIRECT]
    # A filter fixed by the manifest: the question "which path supplies J3 over open pipes".
    fixed = ask(
        story,
        "paths",
        {"target": node("J3")},
        fixed={"source": node("R1"), "edge_where": ["Pipe.status:ne:closed"]},
    )
    assert fixed.value == []
    # P6, from the reservoir, has no status, which no comparison passes.
    known = ask(
        story,
        "paths",
        {"target": node("J3")},
        fixed={"source": node("J1"), "edge_where": ["Pipe.status:ne:closed"]},
    )
    assert known.value == [BY_T1]


def test_the_least_total_weight_when_a_weight_is_named(story: History):
    answer = ask(story, "paths", {**J1_J3, "k": 3, "weight": "length"})
    # The unit of the weights added up.
    assert answer.unit == "m"
    assert answer.value == [
        {**BY_T1, "weight": 10.0},
        {**BY_J2, "weight": 20.0},
        {**DIRECT, "weight": 100.0},
    ]
    assert ask(story, "paths", {**J1_J3, "weight": "length"}).value == [{**BY_T1, "weight": 10.0}]


def test_a_link_without_the_weight_is_not_followed_unless_given_one(story: History):
    to_tank = {"source": node("J1"), "target": node("T2"), "weight": "setting"}
    # Only valves have a setting, and V1's is empty.
    assert ask(story, "paths", to_tank).value == []
    answer = ask(story, "paths", {**to_tank, "default_weight": 2.5})
    assert answer.value == [
        {
            "hops": 2,
            "weight": 5.0,
            "nodes": [node("J1"), node("J2"), node("T2")],
            "edges": [
                _pipe("P1", "J1", "J2"),
                {
                    "link_uid": link("V1"),
                    "edge_type": "Valve",
                    "source": node("J2"),
                    "target": node("T2"),
                },
            ],
        }
    ]


def test_links_are_followed_in_the_direction_asked_for(story: History):
    back = {"source": node("J3"), "target": node("J1")}
    # Every pipe leads away from J1.
    assert ask(story, "paths", {**back, "direction": "out"}).value == []
    upstream = ask(story, "paths", {**back, "direction": "in"}).value
    # The edge is reported the way it is stored.
    assert upstream == [{**DIRECT, "nodes": [node("J3"), node("J1")]}]
    assert ask(story, "paths", {**J1_J3, "direction": "out"}).value == [DIRECT]
    assert ask(story, "paths", {**J1_J3, "direction": "in"}).value == []


def test_every_link_type_is_followed_and_no_reference_unless_named(story: History):
    watched = {"source": "sensor:S1", "target": node("J1")}
    # A sensor refers to its node; that is no link.
    assert ask(story, "paths", watched).value == []
    assert ask(story, "paths", {**watched, "edge_types": ["Sensor.measures"]}).value == [
        {
            "hops": 1,
            "weight": None,
            "nodes": ["sensor:S1", node("J1")],
            "edges": [
                {
                    "link_uid": None,
                    "edge_type": "Sensor.measures",
                    "source": "sensor:S1",
                    "target": node("J1"),
                }
            ],
        }
    ]
    assert ask(story, "paths", {**J1_J3, "edge_types": ["Valve"]}).value == []
    assert ask(story, "paths", {**J1_J3, "edge_types": "Pipe,Valve", "k": 3}).value == [
        DIRECT,
        BY_J2,
        BY_T1,
    ]
    # A class stands for the link types under it.
    assert ask(story, "paths", {**J1_J3, "edge_types": ["Link"], "k": 3}).value == [
        DIRECT,
        BY_J2,
        BY_T1,
    ]


def test_an_object_is_joined_to_itself_by_no_link(story: History):
    answer = ask(story, "paths", {"source": node("J1"), "target": node("J1")})
    assert answer.value == [{"hops": 0, "weight": None, "nodes": [node("J1")], "edges": []}]


def test_paths_are_those_of_the_pinned_version(story: History):
    # T4 is joined to nothing; before #4 it is not there at all.
    lonely = {"source": node("J1"), "target": node("T4")}
    assert ask(story, "paths", lonely).value == []
    error = refusal(story, "paths", lonely, version=3)
    assert error.code == "function.bad_param"
    assert error.detail == {"function": FUNCTION, "parameter": "target", "uid": node("T4")}


@pytest.mark.parametrize(
    ("call", "code", "parameter"),
    [
        ({"source": node("X1"), "target": node("J3")}, "function.bad_param", "source"),
        ({"source": node("J1"), "target": "valve:V9"}, "function.bad_param", "target"),
        ({**J1_J3, "edge_types": ["Canal"]}, "store.unknown_type", None),
        # A type that is no link is no edge type.
        ({**J1_J3, "edge_types": ["Junction"]}, "store.unknown_type", None),
        ({**J1_J3, "edge_types": ["Node"]}, "function.bad_param", "edge_types"),
        ({**J1_J3, "edge_where": ["Junction.elevation:gt:1"]}, "function.bad_param", "edge_where"),
        ({**J1_J3, "edge_where": ["Pipe.length:gt:long"]}, "function.bad_param", "edge_where"),
        ({**J1_J3, "edge_where": ["status:eq:open"]}, "function.bad_param", "edge_where"),
        ({**J1_J3, "edge_where": ["Pipe.colour:eq:red"]}, "store.unknown_property", None),
        ({**J1_J3, "edge_where": ["Canal.status:eq:open"]}, "store.unknown_type", None),
        ({**J1_J3, "weight": "status"}, "function.bad_param", "weight"),
        ({**J1_J3, "weight": "colour"}, "store.unknown_property", None),
        ({**J1_J3, "default_weight": 1}, "function.bad_param", "default_weight"),
        ({**J1_J3, "k": 101}, "function.bad_param", "k"),
        ({**J1_J3, "max_depth": 65}, "function.bad_param", "max_depth"),
    ],
)
def test_what_a_path_search_refuses(story: History, call, code, parameter):
    error = refusal(story, "paths", call)
    assert error.code == code, error.message
    if parameter is not None:
        assert error.detail["function"] == FUNCTION
        assert error.detail["parameter"] == parameter


def test_the_graph_of_a_version_is_built_once(story: History):
    ask(story, "paths", J1_J3)
    builds = story.graphs.builds
    ask(story, "paths", {**J1_J3, "k": 3, "weight": "length"})
    assert story.graphs.builds == builds
