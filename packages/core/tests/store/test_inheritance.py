"""Type inheritance: references to an abstract type, one type per uid, uid scopes."""

from __future__ import annotations

import pymysql
import pytest
from dta_core.store import Store, ViolationError
from dta_store_testkit import at, changes, meta, raw_connection, sql

NETWORK = {
    "Zone": [{"uid": "zone:1", "name": "North"}],
    "Junction": [{"uid": "node:j1", "name": "J1", "elevation": 10.0}],
    "Tank": [{"uid": "node:t1", "name": "T1", "capacity": 50.0}],
}


def _rules(error: ViolationError) -> set[tuple[str | None, str | None, str]]:
    return {(v.uid, v.property, v.rule) for v in error.violations}


def test_reference_to_an_abstract_type_accepts_each_descendant(store: Store):
    store.commit(changes(NETWORK), meta(t=at(1)))
    store.commit(
        changes(
            {
                "Pipe": [
                    {"uid": "link:a", "start_node": "node:j1", "end_node": "node:t1"},
                    {"uid": "link:b", "start_node": "node:t1", "end_node": "node:j1"},
                    {"uid": "link:c", "start_node": "node:j1", "end_node": "node:j1"},
                ]
            }
        ),
        meta(t=at(2)),
    )
    pipes = {row["uid"]: row for row in store.table("Pipe").to_pylist()}
    assert pipes["link:a"]["end_node"] == "node:t1" and pipes["link:b"]["start_node"] == "node:t1"


def test_reference_to_an_abstract_type_refuses_any_other_type(store: Store):
    store.commit(changes(NETWORK), meta(t=at(1)))
    with pytest.raises(ViolationError) as caught:
        store.commit(
            changes(
                {
                    "Pipe": [
                        {"uid": "link:a", "start_node": "zone:1", "end_node": "node:t1"},
                        # A reference to a concrete type refuses its siblings too.
                        {
                            "uid": "link:b",
                            "start_node": "node:j1",
                            "end_node": "node:t1",
                            "feeds_tank": "node:j1",
                        },
                        {"uid": "link:c", "start_node": "node:j1", "end_node": "node:missing"},
                    ]
                }
            ),
            meta(t=at(2)),
        )
    assert _rules(caught.value) == {
        ("link:a", "start_node", "reference"),
        ("link:b", "feeds_tank", "reference"),
        ("link:c", "end_node", "reference"),
    }
    assert store.table("Pipe").num_rows == 0


def test_a_reference_may_point_at_an_object_created_in_the_same_change_set(store: Store):
    store.commit(
        changes(
            {
                **NETWORK,
                "Pipe": [{"uid": "link:a", "start_node": "node:j1", "end_node": "node:t1"}],
            }
        ),
        meta(t=at(1)),
    )
    assert store.get("link:a").values["start_node"] == "node:j1"


def test_one_uid_cannot_hold_two_types(store: Store):
    store.commit(changes(NETWORK), meta(t=at(1)))
    with pytest.raises(ViolationError) as caught:
        store.commit(
            changes({"Tank": [{"uid": "node:j1", "name": "impostor", "capacity": 1.0}]}),
            meta(t=at(2)),
        )
    assert _rules(caught.value) == {("node:j1", None, "uid_type")}
    # Nor within one change set.
    with pytest.raises(ViolationError) as caught:
        store.commit(
            changes(
                {
                    "Junction": [{"uid": "node:x", "name": "X", "elevation": 1.0}],
                    "Tank": [{"uid": "node:x", "name": "X", "capacity": 1.0}],
                }
            ),
            meta(t=at(2)),
        )
    assert ("node:x", None, "duplicate") in _rules(caught.value)
    assert store.get("node:j1").type == "Junction" and store.get("node:x") is None


def test_the_database_itself_refuses_a_second_type_for_a_uid(store: Store):
    """The guarantee does not rest on the layer alone."""
    store.commit(changes(NETWORK), meta(t=at(1)))
    connection = raw_connection(store.settings)
    try:
        with pytest.raises(pymysql.MySQLError):
            sql(
                connection,
                "INSERT INTO `tank` (`uid`, `name`, `capacity`, `_prov`) "
                "VALUES ('node:j1', 'impostor', 1.0, '{}')",
            )
        with pytest.raises(pymysql.MySQLError):
            sql(connection, "INSERT INTO `_object` (`uid`, `type`) VALUES ('node:j1', 'Tank')")
        assert sql(connection, "SELECT * FROM `dolt_status`") == ()
    finally:
        connection.close()


def test_uids_of_different_scopes_do_not_collide(store: Store):
    store.commit(
        changes(
            {
                "Zone": [{"uid": "zone:1", "name": "zone one"}],
                "Junction": [{"uid": "node:1", "name": "node one", "elevation": 1.0}],
                "Pipe": [{"uid": "link:1", "start_node": "node:1", "end_node": "node:1"}],
            }
        ),
        meta(t=at(1)),
    )
    assert store.get("zone:1").type == "Zone"
    assert store.get("node:1").type == "Junction"
    assert store.get("link:1").type == "Pipe"


def test_a_uid_outside_the_scope_of_its_type_is_refused(store: Store):
    with pytest.raises(ViolationError) as caught:
        store.commit(
            changes(
                {
                    "Zone": [{"uid": "node:1", "name": "wrong scope"}, {"name": "no uid"}],
                    # Junction and Tank share the scope of their common ancestor.
                    "Tank": [{"uid": "tank:1", "name": "T", "capacity": 1.0}],
                    "Junction": [{"uid": "node:", "name": "empty key", "elevation": 1.0}],
                }
            ),
            meta(t=at(1)),
        )
    assert {(v.type, v.uid, v.rule) for v in caught.value.violations} == {
        ("Zone", "node:1", "uid"),
        ("Zone", None, "uid"),
        ("Tank", "tank:1", "uid"),
        ("Junction", "node:", "uid"),
    }


def test_a_uid_can_be_reused_by_another_type_after_its_object_is_deleted(store: Store):
    store.commit(changes(NETWORK), meta(t=at(1)))
    store.commit(changes(deletes={"Junction": ["node:j1"]}), meta(t=at(2)))
    store.commit(
        changes({"Tank": [{"uid": "node:j1", "name": "now a tank", "capacity": 5.0}]}),
        meta(t=at(3)),
    )
    assert store.get("node:j1").type == "Tank"
    assert store.get("node:j1", at="#1").type == "Junction"
    assert store.get("node:j1", at="#2") is None
    # History follows one type; the earlier life is asked for by name.
    assert store.history("node:j1").column("_change").to_pylist() == ["added"]
    earlier = store.history("node:j1", type="Junction")
    assert earlier.column("_change").to_pylist() == ["added", "removed"]
    assert earlier.column("_seq").to_pylist() == [1, 2]
