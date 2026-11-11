import pytest
from dta_core.manifest import parse_manifest, subtree_hash, to_plain
from dta_toolkit.tasks.errors import TaskError
from dta_toolkit.tasks.paths import (
    covers,
    overlaps,
    owned_paths,
    parse_pattern,
    pinned_paths,
    split_path,
)


def _overlap(first, second):
    forward = overlaps(parse_pattern(first), parse_pattern(second))
    assert forward == overlaps(parse_pattern(second), parse_pattern(first))
    return forward


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("sources.a", "sources.a", True),
        ("sources.a", "sources.a.profile", True),  # a key and something inside it
        ("sources", "sources.a.profile.datasets", True),
        ("sources.a", "sources.ab", False),  # whole segments, not characters
        ("sources.a.profile", "sources.ab.profile", False),
        ("sources.a.profile", "sources.a.mappings", False),
        ("models.q1", "functions.q1", False),
        ("functions.q1-*", "functions.q1-kpi", True),
        ("functions.q1-*", "functions.q1-kpi.params", True),
        ("functions.q1-*", "functions.q1", False),
        ("functions.q1-*", "functions.q10", False),
        ("functions.q1-*", "functions", True),
        ("functions.q1-*", "functions.q1-a-*", True),  # a pattern inside a pattern
        ("functions.q1-*", "functions.q2-*", False),
        ("functions.q1-*", "functions.q*", True),
        ("functions.*", "functions.anything.at.all", True),
        ("contract.questions.q1.targets", "contract.questions.q1", True),
        ("contract.questions.q1.targets", "contract.questions.q11.targets", False),
    ],
)
def test_overlap_is_decided_by_whole_segments(first, second, expected):
    assert _overlap(first, second) is expected


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("sources.a.profile", "sources.a.profile", True),
        ("sources.a.profile", "sources.a.profile.datasets.t", True),
        ("sources.a.profile", "sources.a", False),  # the parent of an owned key is not owned
        ("sources.a.profile", "sources.a.profiles", False),
        ("sources.a.profile", "sources.ab.profile", False),
        ("functions.q1-*", "functions.q1-kpi", True),
        ("functions.q1-*", "functions.q1-kpi.entry", True),
        ("functions.q1-*", "functions.q1", False),
        ("functions.q1-*", "functions", False),
        ("functions.q1-*", "models.q1-kpi", False),
    ],
)
def test_a_path_is_inside_a_pattern_only_at_or_below_it(pattern, path, expected):
    assert covers(parse_pattern(pattern), split_path(path)) is expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        ".",
        "a..b",
        "a.",
        ".a",
        "a.b*c",
        "a.**",
        "a.*b",
        "a/b",
        "a b",
        "a.b\n",
        "../x",
        "a.*.b*c",
    ],
)
def test_malformed_patterns_are_rejected(text):
    with pytest.raises(TaskError) as caught:
        parse_pattern(text)
    assert caught.value.code == "tasks.path.invalid"


@pytest.mark.parametrize("text", ["a.*", "a.b-*", "", "a..b", "a/b"])
def test_a_plain_path_has_no_wildcards(text):
    with pytest.raises(TaskError):
        split_path(text)


def _manifest(**extra):
    return parse_manifest(
        {
            "twin": {"id": "demo"},
            "sources": {
                "a": {"connector": "file", "location": "a.csv"},
                "ab": {"connector": "file", "location": "ab.csv"},
            },
            **extra,
        }
    )


_PROFILE_OWNS = ["sources.a.profile", "sources.a.refresh_period", "sources.a.rate_limit_period"]


def test_owned_keys_are_cut_out_of_what_a_task_reads():
    plain = to_plain(_manifest())
    paths = pinned_paths(plain, ["sources.a"], _PROFILE_OWNS)
    assert "sources.a.location" in paths and "sources.a.connector" in paths
    assert not any(path.startswith("sources.a.profile") for path in paths)
    assert not any(path.startswith("sources.ab") for path in paths)
    # Nothing owned lies inside the path: it is pinned whole.
    assert pinned_paths(plain, ["sources.a"], ["models.q1"]) == ["sources.a"]
    # The path itself is owned: nothing of it is pinned.
    assert pinned_paths(plain, ["sources.a.profile"], _PROFILE_OWNS) == []


def test_pin_ignores_own_keys_and_sees_everything_else():
    def pin(manifest):
        return subtree_hash(
            manifest, pinned_paths(to_plain(manifest), ["sources.a"], _PROFILE_OWNS)
        )

    before = _manifest()
    own_write = before.model_copy(deep=True)
    own_write.sources["a"].refresh_period = "PT1H"
    assert pin(own_write) == pin(before)

    other_field = before.model_copy(deep=True)
    other_field.sources["a"].location = "moved.csv"
    assert pin(other_field) != pin(before)

    new_field = before.model_copy(deep=True)
    new_field.sources["a"].description = "now described"
    assert pin(new_field) != pin(before)

    neighbour = before.model_copy(deep=True)
    neighbour.sources["ab"].location = "moved.csv"
    assert pin(neighbour) == pin(before)


def test_pinned_paths_do_not_move_when_own_keys_appear_under_an_empty_parent():
    owns = ["models.demand.candidates"]
    before = {"models": {}}
    after = {"models": {"demand": {"candidates": {"c": {"family": "arima"}}}}}
    assert pinned_paths(before, ["models.demand"], owns) == pinned_paths(
        after, ["models.demand"], owns
    )
    foreign = {"models": {"demand": {"target": "Pump.flow"}}}
    assert pinned_paths(foreign, ["models.demand"], owns) == ["models.demand.target"]


def test_owned_patterns_cut_matching_children_only():
    plain = to_plain(
        _manifest(
            functions={
                "q1": {"kind": "builtin"},
                "q1-kpi": {"kind": "builtin"},
                "q10": {"kind": "builtin"},
            }
        )
    )
    paths = pinned_paths(plain, ["functions"], ["functions.q1-*"])
    assert paths == ["functions.q1", "functions.q10"]


def test_a_key_that_cannot_be_addressed_is_reported():
    plain = {"sources": {"a": {"profile": {}, "odd.key": 1}}}
    with pytest.raises(TaskError) as caught:
        pinned_paths(plain, ["sources.a"], ["sources.a.profile"])
    assert caught.value.code == "tasks.path.unaddressable"


def test_owned_paths_name_plain_keys_always_and_pattern_matches_as_they_exist():
    plain = {
        "sources": {"a": {"location": "a.csv", "profile": {"rows": 1}}},
        "functions": {"q1": {}, "q1-kpi": {}, "q1-trend": {}, "q10": {}},
    }
    owns = ["sources.a.profile", "sources.a.refresh_period", "models.q1", "functions.q1-*"]
    assert owned_paths(plain, owns) == [
        "functions.q1-kpi",
        "functions.q1-trend",
        "models.q1",
        "sources.a.profile",
        "sources.a.refresh_period",
    ]
    assert owned_paths({}, owns) == ["models.q1", "sources.a.profile", "sources.a.refresh_period"]
