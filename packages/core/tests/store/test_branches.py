"""Branches hold what-if versions apart from the live branch, and a merge
brings one into another as a single version or not at all."""

from __future__ import annotations

import pytest
from dta_core.store import (
    BranchExists,
    InvalidInput,
    MergeConflict,
    NothingToMerge,
    ProtectedBranch,
    Store,
    TimeRegression,
    UnknownBranch,
    ViolationError,
)
from dta_store_testkit import at, changes, database_state, meta

SEED = {
    "Zone": [{"uid": "zone:1", "name": "North"}, {"uid": "zone:2", "name": "South"}],
    "Tank": [{"uid": "node:t1", "name": "T1", "capacity": 50.0, "level": 5.0}],
}


def _seeded(store: Store, branch: str = "what-if"):
    store.commit(changes(SEED), meta(t=at(1)))
    return store.branch_create(branch)


def _zones(store: Store, **where) -> list[str]:
    return sorted(store.table("Zone", **where).column("uid").to_pylist())


# -- isolation -----------------------------------------------------------------


def test_a_new_branch_starts_at_the_version_it_was_created_from(store: Store):
    start = _seeded(store)
    head = store.head()
    assert (start.id, start.seq, start.t, start.branch) == (head.id, 1, head.t, "what-if")
    assert store.head("what-if").id == head.id
    assert [(b.name, b.head) for b in store.branches()] == [("main", head.id), ("what-if", head.id)]
    assert [v.id for v in store.versions(branch="what-if")] == [v.id for v in store.versions()]


def test_writes_on_a_branch_and_on_the_live_branch_do_not_see_each_other(store: Store):
    _seeded(store)
    on_branch = store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:b", "name": "branch only"}],
                "Tank": [{"uid": "node:t1", "level": 9.0}],
            },
            kind="simulated",
        ),
        meta("scenario", at(2)),
        branch="what-if",
    )
    on_main = store.commit(
        changes({"Zone": [{"uid": "zone:m", "name": "main only"}]}), meta(t=at(3))
    )
    # Each continues its own numbering from the shared version.
    assert (on_branch.seq, on_branch.branch) == (2, "what-if")
    assert (on_main.seq, on_main.branch) == (2, "main")
    assert on_branch.id != on_main.id

    assert _zones(store) == ["zone:1", "zone:2", "zone:m"]
    assert _zones(store, branch="what-if") == ["zone:1", "zone:2", "zone:b"]
    assert store.get("zone:b") is None and store.get("zone:m", branch="what-if") is None
    assert store.get("node:t1").values["level"] == 5.0
    simulated = store.get("node:t1", branch="what-if")
    assert simulated.values["level"] == 9.0
    assert simulated.provenance_of("level").kind == "simulated"
    assert simulated.provenance_of("capacity").kind == "observed"

    # History and differences follow the branch they are asked about.
    assert store.history("node:t1", ["level"]).column("level").to_pylist() == [5.0]
    assert store.history("node:t1", ["level"], branch="what-if").column("level").to_pylist() == [
        5.0,
        9.0,
    ]
    assert store.diff("#1", "#2")["Zone"].column("uid").to_pylist() == ["zone:m"]
    assert sorted(store.diff("#1", "#2", branch="what-if")) == ["Tank", "Zone"]
    assert [v.cause for v in store.versions(branch="what-if")] == ["install", "sync", "scenario"]
    # A version of one branch is not a version of the other.
    with pytest.raises(Exception) as caught:
        store.resolve(on_branch.id, branch="main")
    assert caught.value.code == "store.unknown_version"
    assert store.resolve(on_branch)[0].branch == "what-if"


def test_a_branch_can_start_at_an_earlier_version(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    store.commit(changes({"Zone": [{"uid": "zone:3", "name": "later"}]}), meta(t=at(2)))
    start = store.branch_create("past", at="#1")
    assert start.seq == 1 and store.head("past").seq == 1
    assert _zones(store, branch="past") == ["zone:1", "zone:2"]
    by_time = store.branch_create("past-by-time", at=at(1.5).isoformat())
    assert by_time.id == start.id
    from_branch = store.branch_create("copy", source="past")
    assert from_branch.id == start.id and from_branch.branch == "copy"


def test_version_time_never_goes_back_on_a_branch_either(store: Store):
    _seeded(store)
    store.commit(changes({}, kind="assumed"), meta("scenario", at(10)), branch="what-if")
    with pytest.raises(TimeRegression) as caught:
        store.commit(changes({}, kind="assumed"), meta("scenario", at(9)), branch="what-if")
    assert caught.value.code == "store.time_regression"
    # The live branch has its own clock.
    assert store.commit(changes({}), meta(t=at(2))).seq == 2


# -- merging -------------------------------------------------------------------


def test_a_clean_merge_is_one_version_holding_both_sides(store: Store):
    _seeded(store)
    theirs = store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:b", "name": "proposed"}],
                "Tank": [{"uid": "node:t1", "level": 7.0}],
            },
            kind="assumed",
        ),
        meta("scenario", at(2)),
        branch="what-if",
    )
    ours = store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:m", "name": "measured"}],
                "Tank": [{"uid": "node:t1", "capacity": 60.0}],
            }
        ),
        meta(t=at(3)),
    )
    merged = store.merge("what-if", "main", meta("merge", at(4), note="accept the proposal"))
    assert (merged.seq, merged.branch) == (3, "main")
    entry = store.versions("head")[0]
    assert entry.cause == "merge" and entry.note == "accept the proposal"
    assert entry.parents == (ours.id, theirs.id)
    assert [v.seq for v in store.versions()] == [0, 1, 2, 3]

    assert _zones(store) == ["zone:1", "zone:2", "zone:b", "zone:m"]
    tank = store.get("node:t1")
    assert (tank.values["level"], tank.values["capacity"]) == (7.0, 60.0)
    # Each cell keeps the provenance it had on the side it came from.
    assert tank.provenance_of("level").kind == "assumed"
    assert tank.provenance_of("capacity").kind == "observed"
    assert store.get("zone:b").provenance_of("name").kind == "assumed"

    # The merge shows as what it brought to the live branch, nothing more.
    difference = store.diff("#2", "#3")
    assert sorted(difference) == ["Tank", "Zone"]
    assert difference["Zone"].column("uid").to_pylist() == ["zone:b"]
    assert difference["Tank"].column("level").to_pylist() == [7.0]
    history = store.history("node:t1", ["level", "capacity"])
    assert history.column("_seq").to_pylist() == [1, 2, 3]
    assert history.column("level").to_pylist() == [5.0, 5.0, 7.0]
    assert history.column("capacity").to_pylist() == [50.0, 60.0, 60.0]
    assert store.changes("Zone", "#3..#3").column("uid").to_pylist() == ["zone:b"]
    assert store.changes("Zone", "#2..#3").column("uid").to_pylist() == ["zone:m", "zone:b"]
    # What the live branch was before the merge is still there to read.
    assert store.get("node:t1", at="#2").values["level"] == 5.0
    assert database_state(store.settings)["status"] == ()

    with pytest.raises(NothingToMerge) as caught:
        store.merge("what-if", "main", meta("merge", at(5)))
    assert caught.value.code == "store.nothing_to_merge"
    # The branch goes on after the merge, and can be merged again.
    store.commit(
        changes({"Zone": [{"uid": "zone:b2", "name": "more"}]}, kind="assumed"),
        meta("scenario", at(6)),
        branch="what-if",
    )
    assert store.merge("what-if", "main", meta("merge", at(7))).seq == 4
    assert "zone:b2" in _zones(store)


def test_a_conflicting_merge_is_refused_whole(store: Store):
    _seeded(store)
    store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:b", "name": "would merge cleanly"}],
                "Tank": [{"uid": "node:t1", "level": 7.0}],
            },
            kind="assumed",
        ),
        meta("scenario", at(2)),
        branch="what-if",
    )
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="inferred"),
        meta("derived", at(3)),
    )
    before = database_state(store.settings)
    branch_before = database_state(store.settings, "what-if")
    with pytest.raises(MergeConflict) as caught:
        store.merge("what-if", "main", meta("merge", at(4)))
    assert caught.value.code == "store.merge_conflict"
    assert "tank" in caught.value.detail["tables"]
    # Not even the part that did not conflict arrived.
    assert database_state(store.settings) == before
    assert database_state(store.settings, "what-if") == branch_before
    assert store.get("zone:b") is None and store.get("node:t1").values["level"] == 6.0
    assert [b.name for b in store.branches()] == ["main", "what-if"]
    assert store.commit(changes({}), meta(t=at(5))).seq == 3


def test_a_merge_that_would_break_a_reference_is_refused(store: Store):
    _seeded(store)
    store.commit(
        changes(
            {"Junction": [{"uid": "node:j1", "name": "J1", "elevation": 3.0, "zone": "zone:2"}]},
            kind="assumed",
        ),
        meta("scenario", at(2)),
        branch="what-if",
    )
    store.commit(changes(deletes={"Zone": ["zone:2"]}), meta(t=at(3)))
    before = database_state(store.settings)
    with pytest.raises((MergeConflict, ViolationError)) as caught:
        store.merge("what-if", "main", meta("merge", at(4)))
    assert caught.value.code in ("store.merge_conflict", "store.violation")
    assert database_state(store.settings) == before
    assert store.get("node:j1") is None


@pytest.mark.parametrize(("kind", "cause"), [("simulated", "scenario"), ("observed", "manual")])
def test_simulated_and_observed_facts_cannot_reach_the_live_branch_by_merge(
    store: Store, kind: str, cause: str
):
    _seeded(store)
    store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:b", "name": "new on the branch"}],
                "Tank": [{"uid": "node:t1", "level": 9.0}],
            },
            kind=kind,
        ),
        meta(cause, at(2)),
        branch="what-if",
    )
    before = database_state(store.settings)
    with pytest.raises(ViolationError) as caught:
        store.merge("what-if", "main", meta("merge", at(3)))
    assert caught.value.code == "store.violation"
    found = {(v.type, v.uid, v.property, v.rule) for v in caught.value.violations}
    assert found == {
        ("Zone", "zone:b", None, "fact_kind"),
        ("Tank", "node:t1", "level", "fact_kind"),
    }
    assert database_state(store.settings) == before
    assert store.get("zone:b") is None and store.get("node:t1").values["level"] == 5.0
    assert store.commit(changes({}), meta(t=at(4))).seq == 2


def test_the_live_branch_can_be_merged_into_a_branch(store: Store):
    _seeded(store)
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 9.0}]}, kind="simulated"),
        meta("scenario", at(2)),
        branch="what-if",
    )
    store.commit(changes({"Zone": [{"uid": "zone:m", "name": "observed later"}]}), meta(t=at(3)))
    merged = store.merge("main", "what-if", meta("merge", at(4)))
    assert (merged.seq, merged.branch) == (3, "what-if")
    assert _zones(store, branch="what-if") == ["zone:1", "zone:2", "zone:m"]
    assert store.get("node:t1", branch="what-if").values["level"] == 9.0
    # Nothing moved on the live branch.
    assert store.head().seq == 2 and store.get("node:t1").values["level"] == 5.0


def test_a_merge_is_recorded_with_cause_merge_and_in_time_order(store: Store):
    _seeded(store)
    store.commit(
        changes({"Zone": [{"uid": "zone:b", "name": "x"}]}, kind="assumed"),
        meta("scenario", at(2)),
        branch="what-if",
    )
    store.commit(changes({}), meta(t=at(5)))
    with pytest.raises(InvalidInput) as caught:
        store.merge("what-if", "main", meta("manual", at(6)))
    assert caught.value.code == "store.invalid_meta"
    with pytest.raises(InvalidInput) as caught:
        store.commit(changes({}), meta("merge", at(6)))
    assert caught.value.code == "store.invalid_meta"
    with pytest.raises(TimeRegression):
        store.merge("what-if", "main", meta("merge", at(4)))
    with pytest.raises(InvalidInput) as caught:
        store.merge("main", "main", meta("merge", at(6)))
    assert caught.value.code == "store.bad_branch_name"
    with pytest.raises(UnknownBranch):
        store.merge("nowhere", "main", meta("merge", at(6)))
    assert store.head().seq == 2 and store.get("zone:b") is None


def test_sync_writes_only_to_the_live_branch(store: Store):
    _seeded(store)
    with pytest.raises(ViolationError) as caught:
        store.commit(
            changes({"Zone": [{"uid": "zone:b", "name": "x"}]}),
            meta("sync", at(2)),
            branch="what-if",
        )
    assert [v.rule for v in caught.value.violations] == ["cause"]
    assert store.head("what-if").seq == 1


def _level(uid: str, value: float) -> dict:
    return {"Tank": [{"uid": uid, "level": value}]}


def _points(table, *names: str) -> list[tuple]:
    return list(zip(*(table.column(name).to_pylist() for name in ("_seq", *names)), strict=True))


def test_history_and_changes_stay_exact_across_forks_and_merges(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    """Lines of history that leave the live branch and come back do not hide or
    repeat any change, whichever way the change log is read."""
    tanks = [{"uid": f"node:{key}", "name": key, "capacity": 100.0, "level": 0.0} for key in "mab"]
    store.commit(changes({"Tank": tanks, "Zone": [{"uid": "zone:1", "name": "Z"}]}), meta(t=at(1)))
    store.branch_create("a")
    store.branch_create("b")

    def on_main(minute: float, value: float):
        return store.commit(
            changes(_level("node:m", value), kind="inferred"), meta("derived", at(minute))
        )

    def on_branch(branch: str, minute: float, uid: str, value: float):
        return store.commit(
            changes(_level(uid, value), kind="assumed"), meta("scenario", at(minute)), branch=branch
        )

    on_main(2, 1.0)  # 2, the first version after both branches left
    on_branch("a", 2, "node:a", 10.0)
    on_branch("a", 3, "node:a", 11.0)
    on_branch("b", 3, "node:b", 20.0)
    on_main(4, 2.0)  # 3
    assert store.merge("a", "main", meta("merge", at(5))).seq == 4
    on_main(6, 3.0)  # 5
    assert store.merge("main", "b", meta("merge", at(7))).seq == 3
    on_main(8, 4.0)  # 6, the first version after the live branch was merged into b
    on_branch("b", 9, "node:b", 21.0)
    assert store.merge("b", "main", meta("merge", at(10))).seq == 7
    store.branch_create("c")
    store.commit(
        changes(_level("node:m", 5.0), deletes={"Zone": ["zone:1"]}), meta(t=at(11))
    )  # 8, the first version after c left, removing an object
    store.commit(
        changes({"Zone": [{"uid": "zone:c", "name": "C"}]}, kind="assumed"),
        meta("scenario", at(12)),
        branch="c",
    )
    assert store.merge("c", "main", meta("merge", at(13))).seq == 9

    def check(reader: Store) -> None:
        level = lambda uid, **where: _points(reader.history(uid, ["level"], **where), "level")  # noqa: E731
        assert level("node:m") == [(1, 0.0), (2, 1.0), (3, 2.0), (5, 3.0), (6, 4.0), (8, 5.0)]
        assert level("node:a") == [(1, 0.0), (4, 11.0)]
        assert level("node:b") == [(1, 0.0), (7, 21.0)]
        assert level("node:m", range="#2..#6") == [(2, 1.0), (3, 2.0), (5, 3.0), (6, 4.0)]
        assert _points(reader.history("zone:1"), "_change") == [(1, "added"), (8, "removed")]
        assert _points(reader.history("zone:c"), "_change") == [(9, "added")]
        tank_changes = [
            *[(1, f"node:{key}", "added") for key in "abm"],
            (2, "node:m", "modified"),
            (3, "node:m", "modified"),
            (4, "node:a", "modified"),
            (5, "node:m", "modified"),
            (6, "node:m", "modified"),
            (7, "node:b", "modified"),
            (8, "node:m", "modified"),
        ]
        assert _points(reader.changes("Tank"), "uid", "_change") == tank_changes
        assert _points(reader.changes("Tank", "#2..#7"), "uid", "_change") == tank_changes[3:9]
        assert _points(reader.changes("Zone"), "uid", "_change") == [
            (1, "zone:1", "added"),
            (8, "zone:1", "removed"),
            (9, "zone:c", "added"),
        ]
        # The same holds along a branch: its own versions, then what it merged in.
        assert level("node:b", branch="b") == [(1, 0.0), (2, 20.0), (4, 21.0)]
        assert level("node:m", branch="b") == [(1, 0.0), (3, 3.0)]
        assert level("node:a", branch="b") == [(1, 0.0), (3, 11.0)]
        assert _points(reader.changes("Tank", branch="b"), "uid", "_change") == [
            *[(1, f"node:{key}", "added") for key in "abm"],
            (2, "node:b", "modified"),
            (3, "node:a", "modified"),
            (3, "node:m", "modified"),
            (4, "node:b", "modified"),
        ]
        # Every version still reads as the state it recorded.
        for seq, expected in [(1, 0.0), (2, 1.0), (4, 2.0), (7, 4.0), (9, 5.0)]:
            assert reader.get("node:m", at=f"#{seq}").values["level"] == expected

    check(store)
    # A store that reads the history from scratch finds the same.
    later = Store(store.settings, store.ontology)
    try:
        check(later)
    finally:
        later.close()
    # And so does reading the whole change log rather than naming the versions.
    monkeypatch.setattr("dta_core.store.reads._NAMED_COMMITS", 2)
    check(store)


# -- names ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", "Main", "has space", "a/b", "_dta_build", "9lives", "x" * 64, "a`b", "a;b", None]
)
def test_a_branch_name_outside_the_pattern_is_refused(store: Store, name):
    for call in (
        lambda: store.branch_create(name),
        lambda: store.head(name),
        lambda: store.table("Zone", branch=name),
        lambda: store.commit(changes({}), meta(t=at(1)), branch=name),
    ):
        with pytest.raises(InvalidInput) as caught:
            call()
        assert caught.value.code == "store.bad_branch_name"


def test_branch_bookkeeping(store: Store):
    _seeded(store)
    with pytest.raises(BranchExists) as caught:
        store.branch_create("what-if")
    assert caught.value.code == "store.branch_exists"
    with pytest.raises(BranchExists):
        store.branch_create("main")
    with pytest.raises(ProtectedBranch) as caught:
        store.branch_delete("main")
    assert caught.value.code == "store.protected_branch"
    for call in (
        lambda: store.head("missing"),
        lambda: store.table("Zone", branch="missing"),
        lambda: store.branch_delete("missing"),
        lambda: store.branch_create("other", source="missing"),
        lambda: store.commit(changes({}), meta(t=at(2)), branch="missing"),
    ):
        with pytest.raises(UnknownBranch) as caught:
            call()
        assert caught.value.code == "store.unknown_branch"

    store.commit(
        changes({"Zone": [{"uid": "zone:b", "name": "x"}]}, kind="assumed"),
        meta("scenario", at(2)),
        branch="what-if",
    )
    store.branch_delete("what-if")
    assert [b.name for b in store.branches()] == ["main"]
    with pytest.raises(UnknownBranch):
        store.head("what-if")
    # The name is free again and starts from the live branch, not from what was deleted.
    again = store.branch_create("what-if")
    assert again.seq == 1 and _zones(store, branch="what-if") == ["zone:1", "zone:2"]
