"""The rules enforced on every commit, each with a case that breaks it, and proof
that a refused change set leaves nothing behind on any connection."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest
from dta_core.schema import Violation
from dta_core.store import (
    ChangeSet,
    HeadMoved,
    InvalidInput,
    LockTimeout,
    Store,
    TimeRegression,
    ViolationError,
)
from dta_store_testkit import at, changes, meta, prov, raw_connection, sql

BASE = {
    "Zone": [{"uid": "zone:1", "name": "North"}],
    "Junction": [{"uid": "node:j1", "name": "J1", "elevation": 10.0, "zone": "zone:1"}],
    "Tank": [{"uid": "node:t1", "name": "T1", "capacity": 50.0, "level": 5.0}],
    "Pipe": [{"uid": "link:p1", "start_node": "node:j1", "end_node": "node:t1"}],
}


def _fingerprint(store: Store) -> dict:
    """Everything a refused change set could have touched, read behind the store's back."""
    connection = raw_connection(store.settings)
    try:
        state = {
            "head": sql(connection, "SELECT HASHOF('main')")[0][0],
            "status": sql(connection, "SELECT * FROM `dolt_status`"),
            "log": sql(connection, "SELECT COUNT(*) FROM `dolt_log`")[0][0],
        }
        for table in ("_object", "zone", "junction", "tank", "pipe", "dta_sync_run"):
            state[table] = sql(connection, f"SELECT * FROM `{table}` ORDER BY `uid`")
        return state
    finally:
        connection.close()


def _refused(
    store: Store, change_set: ChangeSet, commit_meta=None, branch: str = "main"
) -> list[Violation]:
    """Commit a change set that must be refused; assert nothing at all changed."""
    before = _fingerprint(store)
    commit_meta = commit_meta or meta(t=at(100))
    listed = store.validate(change_set, commit_meta, branch)
    with pytest.raises(ViolationError) as caught:
        store.commit(change_set, commit_meta, branch)
    assert _fingerprint(store) == before
    assert caught.value.code == "store.violation"
    assert caught.value.detail["violations"] == [
        dataclasses.asdict(v) for v in caught.value.violations
    ]
    # What validate reports is what commit refuses.
    assert listed == caught.value.violations
    return caught.value.violations


def _keys(violations: list[Violation]) -> set[tuple[str, str | None, str | None, str]]:
    return {(v.type, v.uid, v.property, v.rule) for v in violations}


@pytest.fixture
def loaded(store: Store) -> Store:
    store.commit(changes(BASE), meta(t=at(1)))
    return store


# -- which facts may be written where ------------------------------------------


def test_observed_facts_reach_the_live_branch_only_through_sync(loaded: Store):
    row = {
        "Tank": [
            {"uid": "node:t1", "level": 6.0},
            {"uid": "node:t2", "name": "T2", "capacity": 1.0},
        ]
    }
    for cause in ("manual", "action", "derived", "registration", "scenario"):
        found = _refused(loaded, changes(row, kind="observed"), meta(cause, at(2)))
        assert _keys(found) == {
            ("Tank", "node:t1", None, "fact_kind"),
            ("Tank", "node:t2", None, "fact_kind"),
        }
    loaded.commit(changes(row, kind="observed"), meta("sync", at(2)))
    assert loaded.get("node:t1").values["level"] == 6.0


def test_simulated_facts_are_refused_on_the_live_branch_whatever_the_cause(loaded: Store):
    row = {"Tank": [{"uid": "node:t1", "level": 6.0}]}
    for cause in ("sync", "manual", "scenario", "derived"):
        found = _refused(loaded, changes(row, kind="simulated"), meta(cause, at(2)))
        assert _keys(found) == {("Tank", "node:t1", None, "fact_kind")}
    # A per-row override is held to the same rule.
    mixed = changes(row, kind="inferred", row_provenance={"node:t1": prov("simulated")})
    assert _keys(_refused(loaded, mixed, meta("derived", at(2)))) == {
        ("Tank", "node:t1", None, "fact_kind")
    }


def test_inferred_and_assumed_facts_may_be_written_by_any_cause(loaded: Store):
    loaded.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="inferred"),
        meta("derived", at(2)),
    )
    loaded.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 7.0}]}, kind="assumed"),
        meta("manual", at(3)),
    )
    assert loaded.get("node:t1").values["level"] == 7.0


def test_sync_is_refused_on_any_other_branch(loaded: Store):
    loaded.branch_create("what-if")
    found = _refused(
        loaded,
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="observed"),
        meta("sync", at(2)),
        branch="what-if",
    )
    assert _keys(found) == {("", None, None, "cause")}
    # Off the live branch a scenario may write simulated and observed-looking facts.
    loaded.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="simulated"),
        meta("scenario", at(2)),
        branch="what-if",
    )
    assert loaded.get("node:t1", branch="what-if").values["level"] == 6.0
    assert loaded.get("node:t1").values["level"] == 5.0


def test_meta_model_types_are_exempt_from_the_fact_kind_rule(loaded: Store):
    run = {"TwinSyncRun": [{"uid": "twin_sync_run:r1", "status": "running"}]}
    loaded.commit(changes(run, kind="observed"), meta("action", at(2)))
    loaded.commit(
        changes(
            {"TwinSyncRun": [{"uid": "twin_sync_run:r1", "status": "failed"}]}, kind="simulated"
        ),
        meta("manual", at(3)),
    )
    assert loaded.get("twin_sync_run:r1").values["status"] == "failed"


def test_causes_owned_by_an_operation_cannot_be_used_for_a_plain_commit(loaded: Store):
    for cause in ("install", "migration", "merge"):
        with pytest.raises(InvalidInput) as caught:
            loaded.commit(changes({}), meta(cause, at(2)))
        assert caught.value.code == "store.invalid_meta"
    assert loaded.head().seq == 1


# -- values, references, database constraints ----------------------------------


def test_every_violation_in_a_change_set_is_reported_together(loaded: Store):
    bad = changes(
        {
            "Tank": [
                {"uid": "node:t2", "name": "T2", "capacity": -5.0},  # below minimum
                {"uid": "node:t3", "capacity": 5.0},  # name missing
                {"uid": "node:t4", "name": "T4", "capacity": 5.0, "status": "exploded"},
                {"uid": "node:t5", "name": "T5", "capacity": "big"},  # wrong type
                {"uid": "node:t6", "name": "T6", "capacity": 5.0, "colour": "red"},
                {"uid": "node:t7", "name": "T7", "capacity": 5.0, "code": "nope"},
                {"uid": "node:t1", "name": None},  # partial row nulling a required property
            ],
            "Pipe": [{"uid": "link:p2", "start_node": "node:j1", "end_node": "zone:1"}],
            "Valve": [{"uid": "valve:1"}],
            "Junction": [{"uid": "node:j2", "name": "J2", "elevation": 1.0, "zone": "zone:404"}],
        }
    )
    assert _keys(_refused(loaded, bad)) == {
        ("Tank", "node:t2", "capacity", "range"),
        ("Tank", "node:t3", "name", "required"),
        ("Tank", "node:t4", "status", "enum"),
        ("Tank", "node:t5", "capacity", "type"),
        ("Tank", "node:t6", "colour", "unknown_property"),
        ("Tank", "node:t7", "code", "pattern"),
        ("Tank", "node:t1", "name", "required"),
        ("Pipe", "link:p2", "end_node", "reference"),
        ("Valve", None, None, "unknown_type"),
        ("Junction", "node:j2", "zone", "reference"),
    }


def test_a_naive_datetime_in_a_row_is_refused(loaded: Store):
    from datetime import datetime

    found = _refused(
        loaded, changes({"Tank": [{"uid": "node:t1", "sampled_at": datetime(2024, 1, 1, 12)}]})
    )
    assert _keys(found) == {("Tank", "node:t1", "sampled_at", "type")}
    assert "timezone" in found[0].message


def test_constraints_only_the_database_holds_are_reported_for_every_row(loaded: Store):
    """A check over two columns cannot be judged from a partial row alone."""
    bad = changes(
        {
            "Tank": [
                {"uid": "node:t1", "level": 51.0},  # above the stored capacity of 50
                {"uid": "node:t2", "name": "T2", "capacity": 10.0, "level": 11.0},
                {"uid": "node:t3", "name": "fine", "capacity": 10.0, "level": 9.0},
                {"uid": "node:t4", "name": "T4", "capacity": 3.0, "level": 4.0},
            ]
        }
    )
    found = _refused(loaded, bad)
    assert _keys(found) == {
        ("Tank", "node:t1", None, "check"),
        ("Tank", "node:t2", None, "check"),
        ("Tank", "node:t4", None, "check"),
    }
    assert all("level_within_capacity" in v.message for v in found)


def test_a_violation_in_one_row_keeps_every_other_row_out(loaded: Store):
    mixed = changes(
        {
            "Zone": [{"uid": "zone:2", "name": "valid"}],
            "Tank": [{"uid": "node:t9", "name": "T9", "capacity": 1.0, "level": 2.0}],
        },
        deletes={"Pipe": ["link:p1"]},
    )
    _refused(loaded, mixed)
    assert loaded.get("zone:2") is None and loaded.get("link:p1") is not None
    # The valid part goes in once the violating row is set aside.
    mixed.upserts.pop("Tank")
    loaded.commit(mixed, meta(t=at(2)))
    assert loaded.get("zone:2") is not None and loaded.get("link:p1") is None


def test_validate_reports_without_writing_and_passes_a_sound_change_set(loaded: Store):
    sound = changes({"Zone": [{"uid": "zone:2", "name": "valid"}]})
    before = _fingerprint(loaded)
    assert loaded.validate(sound, meta(t=at(2))) == []
    assert _fingerprint(loaded) == before
    assert loaded.head().seq == 1


def test_malformed_rows_are_refused(loaded: Store):
    bad = ChangeSet(
        upserts={
            "Zone": [
                "not a row",  # type: ignore[list-item]
                {"uid": "zone:3", "name": "dup"},
                {"uid": "zone:3", "name": "dup"},
                {"uid": "zone:" + "x" * 200, "name": "long"},
            ]
        },
        deletes={"Zone": ["zone:3", ""], "Ghost": ["ghost:1"]},
        provenance=prov(),
    )
    assert _keys(_refused(loaded, bad)) == {
        ("Zone", None, None, "uid"),
        ("Zone", "zone:3", None, "duplicate"),
        ("Zone", "zone:" + "x" * 200, "uid", "uid"),
        ("Ghost", None, None, "unknown_type"),
    }


# -- deletes -------------------------------------------------------------------


def test_delete_removes_the_object_and_its_registration(loaded: Store):
    loaded.commit(changes(deletes={"Pipe": ["link:p1"]}), meta(t=at(2)))
    assert loaded.get("link:p1") is None and loaded.table("Pipe").num_rows == 0
    connection = raw_connection(loaded.settings)
    try:
        assert sql(connection, "SELECT * FROM `_object` WHERE `uid` = 'link:p1'") == ()
    finally:
        connection.close()
    assert loaded.get("link:p1", at="#1") is not None


def test_an_object_that_is_still_referenced_cannot_be_deleted(loaded: Store):
    found = _refused(loaded, changes(deletes={"Tank": ["node:t1"], "Zone": ["zone:1"]}))
    assert _keys(found) == {
        ("Tank", "node:t1", None, "referenced"),
        ("Zone", "zone:1", None, "referenced"),
    }
    assert "link:p1" in found[0].message or "link:p1" in found[1].message


def test_delete_goes_through_when_the_references_go_in_the_same_change_set(loaded: Store):
    loaded.commit(
        changes(
            # The pipe is deleted with its end; the junction is pointed elsewhere.
            {
                "Zone": [{"uid": "zone:2", "name": "South"}],
                "Junction": [{"uid": "node:j1", "zone": "zone:2"}],
            },
            deletes={"Tank": ["node:t1"], "Pipe": ["link:p1"], "Zone": ["zone:1"]},
        ),
        meta(t=at(2)),
    )
    assert loaded.get("node:t1") is None and loaded.get("zone:1") is None
    assert loaded.get("node:j1").values["zone"] == "zone:2"


def test_delete_of_a_missing_object_or_under_the_wrong_type_is_refused(loaded: Store):
    found = _refused(loaded, changes(deletes={"Tank": ["node:t404", "node:j1"]}))
    assert _keys(found) == {
        ("Tank", "node:t404", None, "not_found"),
        ("Tank", "node:j1", None, "uid_type"),
    }


def test_a_new_reference_to_an_object_deleted_in_the_same_change_set_is_refused(loaded: Store):
    found = _refused(
        loaded,
        changes(
            {"Pipe": [{"uid": "link:p2", "start_node": "node:j1", "end_node": "node:t1"}]},
            deletes={"Tank": ["node:t1"], "Pipe": ["link:p1"]},
        ),
    )
    assert _keys(found) == {("Pipe", "link:p2", "end_node", "reference")}


# -- version time --------------------------------------------------------------


def test_version_time_never_goes_backwards(loaded: Store):
    before = _fingerprint(loaded)
    with pytest.raises(TimeRegression) as caught:
        loaded.commit(changes({"Zone": [{"uid": "zone:2", "name": "late"}]}), meta(t=at(0.5)))
    assert caught.value.code == "store.time_regression"
    assert _fingerprint(loaded) == before
    # The same time as the head is allowed, and a late fact is recorded at the
    # current time with an earlier time of validity.
    late = ChangeSet(
        upserts={"Zone": [{"uid": "zone:2", "name": "late"}]},
        provenance=prov("observed", t_valid=at(0.5)),
    )
    version = loaded.commit(late, meta(t=at(1)))
    assert version.seq == 2 and version.t == at(1)
    assert loaded.get("zone:2").provenance["*"].t_valid == at(0.5)


def test_an_empty_change_set_still_makes_a_version(loaded: Store):
    """Knowledge time moves on even when a sync finds nothing new."""
    version = loaded.commit(changes({}), meta(t=at(9)))
    assert version.seq == 2 and loaded.head() == version
    assert loaded.diff("#1", "#2") == {}


# -- one committer, and the head it read ---------------------------------------


def test_a_commit_waits_for_the_database_lock_and_gives_up_cleanly(
    loaded: Store, make_store: Callable[..., Store]
):
    impatient = Store(dataclasses.replace(loaded.settings, lock_timeout_s=1.0), loaded.ontology)
    holder = raw_connection(loaded.settings)
    try:
        lock = f"dta_store:{loaded.settings.database}"
        assert sql(holder, "SELECT GET_LOCK(%s, 1)", (lock,)) == ((1,),)
        before = _fingerprint(loaded)
        with pytest.raises(LockTimeout) as caught:
            impatient.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(2)))
        assert caught.value.code == "store.lock_timeout"
        assert _fingerprint(loaded) == before
        sql(holder, "SELECT RELEASE_LOCK(%s)", (lock,))
        assert (
            impatient.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(2))).seq
            == 2
        )
    finally:
        holder.close()
        impatient.close()


def test_a_commit_is_refused_when_the_head_moves_under_it(loaded: Store):
    """Another writer that ignores the lock commits while this one is mid-flight."""
    intruder = raw_connection(loaded.settings)

    def intrude(step: str) -> None:
        if step == "written":
            sql(intruder, "CALL DOLT_COMMIT('--allow-empty', '-m', 'behind the back')")

    loaded._fault = intrude
    try:
        with pytest.raises(HeadMoved) as caught:
            loaded.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(2)))
    finally:
        loaded._fault = None
        intruder.close()
    assert caught.value.code == "store.head_moved" and caught.value.detail["written"] is False
    connection = raw_connection(loaded.settings)
    try:
        assert sql(connection, "SELECT * FROM `zone` WHERE `uid` = 'zone:2'") == ()
        assert sql(connection, "SELECT * FROM `dolt_status`") == ()
    finally:
        connection.close()
