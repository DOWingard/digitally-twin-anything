"""A process that dies at any step of a write leaves either nothing or the whole
version, and whatever a foreign writer left in the working set is discarded,
never committed.

A write passes four steps: `begun` (lock held, build branch ready), `written`
(change applied on the build branch), `sealed` (commit made there) and
`published` (target branch moved onto it). Only the last one changes the
branch being written to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from dta_core.store import NotInstalled, Store
from dta_store_testkit import (
    at,
    changes,
    database_state,
    meta,
    raw_connection,
    run_worker,
    sql,
    water_ontology,
    water_ontology_v2,
)
from store_worker import DIED

SEED = {"Zone": [{"uid": "zone:1", "name": "North"}]}
DISCARDING = "uncommitted changes found in the working set; discarding them"
BEFORE_PUBLISHING = ["begun", "written", "sealed"]


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _die(store: Store, operation: str, step: str) -> None:
    result = run_worker("die", store.settings.database, operation, step)
    assert result.returncode == DIED, (result.returncode, result.stdout, result.stderr[-3000:])


# -- a commit of rows ----------------------------------------------------------


@pytest.mark.parametrize("step", BEFORE_PUBLISHING)
def test_a_commit_killed_before_it_is_published_leaves_no_trace(store: Store, step: str):
    store.commit(changes(SEED), meta(t=at(1)))
    before = database_state(store.settings)
    _die(store, "commit", step)
    assert database_state(store.settings) == before
    assert [branch.name for branch in store.branches()] == ["main"]
    # The lock died with the process, and the next commit takes the next position.
    version = store.commit(changes({"Zone": [{"uid": "zone:2", "name": "after"}]}), meta(t=at(5)))
    assert version.seq == 2
    assert store.get("zone:doomed") is None and store.get("node:doomed") is None
    assert sorted(store.table("Zone").column("uid").to_pylist()) == ["zone:1", "zone:2"]
    assert store.diff("#1", "#2")["Zone"].column("uid").to_pylist() == ["zone:2"]
    assert [v.seq for v in store.versions()] == [0, 1, 2]


def test_a_commit_killed_after_it_is_published_is_whole(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    _die(store, "commit", "published")
    head = store.head()
    assert head.seq == 2
    assert store.get("zone:doomed") is not None and store.get("node:doomed") is not None
    assert database_state(store.settings)["status"] == ()
    assert store.commit(changes({}), meta(t=at(5))).seq == 3


def test_a_restarted_process_sees_what_was_published_and_nothing_else(store: Store):
    """The same holds for a store that starts after the crash, not only for one that was running."""
    store.commit(changes(SEED), meta(t=at(1)))
    _die(store, "commit", "sealed")
    later = Store(store.settings, water_ontology())
    try:
        assert later.head().seq == 1 and later.get("zone:doomed") is None
        assert (
            later.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(5))).seq
            == 2
        )
    finally:
        later.close()


# -- a change of table definitions ---------------------------------------------


@pytest.mark.parametrize("step", BEFORE_PUBLISHING)
def test_a_migration_killed_before_it_is_published_changes_nothing(store: Store, step: str):
    store.commit(changes(SEED), meta(t=at(1)))
    before = database_state(store.settings)
    _die(store, "migrate", step)
    assert database_state(store.settings) == before
    assert [branch.name for branch in store.branches()] == ["main"]
    assert store.ontology_at().hash == water_ontology().hash
    # Writing goes on under the old ontology, and the migration can be run again.
    assert store.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(5))).seq == 2
    version = store.migrate(water_ontology_v2(), meta("migration", at(6)))
    assert version.seq == 3 and store.ontology_at().hash == water_ontology_v2().hash
    store.commit(changes({"Valve": [{"uid": "valve:1", "name": "V1"}]}), meta(t=at(7)))
    assert store.get("valve:1").values["name"] == "V1"


def test_a_migration_killed_after_it_is_published_is_whole(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    _die(store, "migrate", "published")
    head = store.versions("head")[0]
    assert head.seq == 2 and head.cause == "migration"
    assert store.ontology_at().hash == water_ontology_v2().hash
    assert database_state(store.settings)["status"] == ()
    later = Store(store.settings, water_ontology_v2())
    try:
        later.commit(changes({"Valve": [{"uid": "valve:1", "name": "V1"}]}), meta(t=at(6)))
        assert later.table("Valve").num_rows == 1
    finally:
        later.close()


@pytest.mark.parametrize("step", BEFORE_PUBLISHING)
def test_an_install_killed_midway_can_be_run_again(make_store: Callable[..., Store], step: str):
    store = make_store(install=False)
    _die(store, "install", step)
    with pytest.raises(NotInstalled) as caught:
        store.head()
    assert caught.value.code == "store.not_installed"
    assert store.install("twin-under-test").seq == 0
    assert [branch.name for branch in store.branches()] == ["main"]
    assert store.commit(changes(SEED), meta(t=at(1))).seq == 1


def test_an_install_killed_after_it_is_published_is_whole(make_store: Callable[..., Store]):
    store = make_store(install=False)
    _die(store, "install", "published")
    assert store.head().seq == 0
    assert store.install("twin-under-test").seq == 0
    assert store.commit(changes(SEED), meta(t=at(1))).seq == 1


# -- changes left in the working set -------------------------------------------


def _leave_stray_changes(store: Store, branch: str = "main") -> None:
    """What a writer that bypasses the store leaves: rows, some staged, and a table."""
    connection = raw_connection(store.settings, branch)
    try:
        sql(connection, "INSERT INTO `_object` (`uid`, `type`) VALUES ('zone:stray', 'Zone')")
        sql(
            connection,
            "INSERT INTO `zone` (`uid`, `name`, `_prov`) VALUES ('zone:stray', 'stray', '{}')",
        )
        sql(connection, "UPDATE `zone` SET `name` = 'tampered' WHERE `uid` = 'zone:1'")
        sql(connection, "CREATE TABLE `stray_table` (`id` INT PRIMARY KEY)")
        sql(connection, "CALL DOLT_ADD('zone')")
        assert len(sql(connection, "SELECT * FROM `dolt_status`")) >= 3
    finally:
        connection.close()


def test_stray_changes_found_at_startup_are_discarded_and_logged(
    store: Store, caplog: pytest.LogCaptureFixture
):
    store.commit(changes(SEED), meta(t=at(1)))
    clean = database_state(store.settings)
    _leave_stray_changes(store)
    later = Store(store.settings, water_ontology())
    try:
        with caplog.at_level(logging.INFO, logger="dta"):
            assert later.head().seq == 1
    finally:
        later.close()
    assert database_state(store.settings) == clean
    discarded = [r for r in caplog.records if r.getMessage() == DISCARDING]
    assert len(discarded) == 1 and discarded[0].levelname == "WARNING"
    assert discarded[0].data == {"branch": "main", "tables": ["_object", "stray_table", "zone"]}
    assert "working set recovered" in _messages(caplog)


def test_stray_changes_are_never_swept_into_the_next_commit(
    store: Store, caplog: pytest.LogCaptureFixture
):
    store.commit(changes(SEED), meta(t=at(1)))
    _leave_stray_changes(store)
    with caplog.at_level(logging.WARNING, logger="dta"):
        version = store.commit(
            changes({"Zone": [{"uid": "zone:2", "name": "real"}]}), meta(t=at(2))
        )
    assert DISCARDING in _messages(caplog)
    assert version.seq == 2
    assert store.get("zone:stray") is None and store.get("zone:1").values["name"] == "North"
    assert store.diff("#1", "#2")["Zone"].column("uid").to_pylist() == ["zone:2"]
    state = database_state(store.settings)
    assert state["status"] == () and "stray_table" not in state


def test_validate_does_not_commit_or_discard_anything(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    before = database_state(store.settings)
    assert store.validate(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(2))) == []
    assert database_state(store.settings) == before


def test_changes_made_behind_the_store_during_a_commit_stay_out_of_the_version(
    store: Store, caplog: pytest.LogCaptureFixture
):
    """Even staged changes to the very table being written do not get into the version."""
    store.commit(changes(SEED), meta(t=at(1)))

    def interfere(step: str) -> None:
        if step == "written":
            _leave_stray_changes(store)

    store._fault = interfere
    try:
        with caplog.at_level(logging.WARNING, logger="dta"):
            version = store.commit(
                changes({"Zone": [{"uid": "zone:2", "name": "real"}]}), meta(t=at(2))
            )
    finally:
        store._fault = None
    # The version holds what the store wrote and nothing else.
    assert version.seq == 2
    assert store.diff("#1", "#2")["Zone"].column("uid").to_pylist() == ["zone:2"]
    assert store.get("zone:stray") is None and store.get("zone:1").values["name"] == "North"
    connection = raw_connection(store.settings)
    try:
        objects = sql(connection, "SELECT `uid` FROM `_object` AS OF %s", (version.id,))
        assert sorted(uid for (uid,) in objects) == ["zone:1", "zone:2"]
    finally:
        connection.close()
    # They stood in the way of publishing, so they were discarded then, and said so.
    assert DISCARDING in _messages(caplog)
    state = database_state(store.settings)
    assert state["status"] == () and "stray_table" not in state
    assert store.commit(changes({}), meta(t=at(3))).seq == 3
    assert store.diff("#2", "#3") == {}


def test_stray_changes_on_another_branch_are_discarded_by_a_commit_to_it(
    store: Store, caplog: pytest.LogCaptureFixture
):
    store.commit(changes(SEED), meta(t=at(1)))
    store.branch_create("what-if")
    _leave_stray_changes(store, branch="what-if")
    with caplog.at_level(logging.WARNING, logger="dta"):
        version = store.commit(
            changes({"Zone": [{"uid": "zone:2", "name": "real"}]}, kind="assumed"),
            meta("scenario", at(2)),
            branch="what-if",
        )
    assert version.seq == 2 and version.branch == "what-if"
    assert [r.data["branch"] for r in caplog.records if r.getMessage() == DISCARDING] == ["what-if"]
    assert store.get("zone:stray", branch="what-if") is None
    assert database_state(store.settings, "what-if")["status"] == ()
