"""Every stored row carries the provenance of its values."""

from __future__ import annotations

import json

import pytest
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, Provenance, Store, ViolationError
from dta_store_testkit import at, changes, meta, prov

TANK = {"uid": "node:t1", "name": "T1", "capacity": 50.0, "level": 1.0, "status": "ok"}
OBSERVED = prov("observed", "scada", at(1))


def _stored(store: Store, uid: str = "node:t1", version: str = "head") -> dict:
    table = store.table("Tank", filter={"uid": uid}, at=version, prov=True)
    return json.loads(table.column("_prov").to_pylist()[0])


def test_a_whole_row_carries_one_provenance_for_every_property(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    assert _stored(store) == {
        "*": {"s": "scada", "k": "observed", "c": 1.0, "t": "2024-01-01T00:01:00Z"}
    }
    found = store.get("node:t1")
    assert found.provenance == {"*": OBSERVED}
    assert found.provenance_of("level") == OBSERVED and found.provenance_of("name") == OBSERVED


def test_a_partial_write_gives_new_provenance_to_what_it_writes_and_keeps_the_rest(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    estimated = Provenance("level-model", FactKind.inferred, at(2), 0.7)
    store.commit(
        ChangeSet(upserts={"Tank": [{"uid": "node:t1", "level": 2.5}]}, provenance=estimated),
        meta("derived", at(2)),
    )
    found = store.get("node:t1")
    assert found.values["level"] == 2.5 and found.values["name"] == "T1"
    assert found.provenance == {"*": OBSERVED, "level": estimated}
    assert found.provenance_of("level") == estimated
    assert found.provenance_of("capacity") == OBSERVED

    # A second property written later gets its own entry; the first keeps its own.
    assumed = Provenance("operator", FactKind.assumed, at(3))
    store.commit(
        ChangeSet(upserts={"Tank": [{"uid": "node:t1", "status": "low"}]}, provenance=assumed),
        meta("manual", at(3)),
    )
    assert store.get("node:t1").provenance == {"*": OBSERVED, "level": estimated, "status": assumed}
    # The earlier version still reads as it was.
    assert store.get("node:t1", at="#2").provenance == {"*": OBSERVED, "level": estimated}
    assert store.get("node:t1", at="#1").provenance == {"*": OBSERVED}


def test_an_entry_equal_to_the_row_provenance_is_dropped(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    estimated = Provenance("level-model", FactKind.inferred, at(2))
    store.commit(
        ChangeSet(upserts={"Tank": [{"uid": "node:t1", "level": 2.5}]}, provenance=estimated),
        meta("derived", at(2)),
    )
    assert set(_stored(store)) == {"*", "level"}
    # The source reports the level again with the provenance the row already has.
    store.commit(
        ChangeSet(upserts={"Tank": [{"uid": "node:t1", "level": 3.0}]}, provenance=OBSERVED),
        meta(t=at(3)),
    )
    assert _stored(store) == {"*": OBSERVED.to_json()}
    assert store.get("node:t1").values["level"] == 3.0


def test_replacing_a_row_resets_its_provenance(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 2.5}]}, kind="inferred"),
        meta("derived", at(2)),
    )
    assert "level" in _stored(store)
    later = prov("observed", "scada", at(3))
    whole = {name: None for name in store.get("node:t1").values} | TANK | {"level": 9.0}
    store.commit(ChangeSet(upserts={"Tank": [whole]}, provenance=later), meta(t=at(3)))
    assert _stored(store) == {"*": later.to_json()}


def test_one_row_can_override_the_provenance_of_the_change_set(store: Store):
    manual = Provenance("operator", FactKind.assumed, at(1), 0.4)
    store.commit(
        ChangeSet(
            upserts={
                "Tank": [TANK, {"uid": "node:t2", "name": "T2", "capacity": 10.0}],
                "Zone": [{"uid": "zone:1", "name": "Z"}],
            },
            provenance=OBSERVED,
            row_provenance={"node:t2": manual},
        ),
        meta(t=at(1)),
    )
    assert store.get("node:t1").provenance == {"*": OBSERVED}
    assert store.get("node:t2").provenance == {"*": manual}
    assert store.get("zone:1").provenance == {"*": OBSERVED}
    assert store.get("node:t2").provenance_of("name").confidence == 0.4


def test_an_override_alone_is_enough_provenance(store: Store):
    store.commit(
        ChangeSet(upserts={"Tank": [TANK]}, row_provenance={"node:t1": OBSERVED}), meta(t=at(1))
    )
    assert store.get("node:t1").provenance == {"*": OBSERVED}


def test_a_write_without_provenance_is_refused_whole(store: Store):
    with pytest.raises(ViolationError) as caught:
        store.commit(
            ChangeSet(
                upserts={
                    "Tank": [TANK, {"uid": "node:t2", "name": "T2", "capacity": 10.0}],
                    "Zone": [{"uid": "zone:1", "name": "Z"}],
                },
                row_provenance={"node:t2": OBSERVED},
            ),
            meta(t=at(1)),
        )
    assert {(v.uid, v.rule) for v in caught.value.violations} == {
        ("node:t1", "provenance"),
        ("zone:1", "provenance"),
    }
    assert caught.value.code == "store.violation"
    assert store.table("Tank").num_rows == 0 and store.head().seq == 0


def test_provenance_that_is_not_a_provenance_value_is_refused(store: Store):
    loose = {"s": "scada", "k": "observed", "c": 1.0, "t": "2024-01-01T00:00:00Z"}
    with pytest.raises(ViolationError) as caught:
        store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=loose), meta(t=at(1)))  # type: ignore[arg-type]
    assert [(v.uid, v.rule) for v in caught.value.violations] == [("node:t1", "provenance")]
    with pytest.raises(ViolationError) as caught:
        store.commit(
            ChangeSet(
                upserts={"Tank": [TANK]},
                provenance=OBSERVED,
                row_provenance={"node:unwritten": OBSERVED},
            ),
            meta(t=at(1)),
        )
    assert [(v.uid, v.rule) for v in caught.value.violations] == [("node:unwritten", "provenance")]


def test_the_database_refuses_a_row_without_provenance(store: Store):
    import pymysql
    from dta_store_testkit import raw_connection, sql

    connection = raw_connection(store.settings)
    try:
        sql(connection, "INSERT INTO `_object` (`uid`, `type`) VALUES ('zone:1', 'Zone')")
        with pytest.raises(pymysql.MySQLError):
            sql(connection, "INSERT INTO `zone` (`uid`, `name`) VALUES ('zone:1', 'Z')")
        sql(connection, "CALL DOLT_RESET('--hard')")
    finally:
        connection.close()


def test_provenance_changes_show_in_history_only_when_asked_for(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    # The same level observed again later: the value is unchanged, its provenance is not.
    store.commit(
        ChangeSet(
            upserts={"Tank": [{"uid": "node:t1", "level": 1.0}]},
            provenance=prov("observed", "scada", at(2)),
        ),
        meta(t=at(2)),
    )
    assert store.history("node:t1", ["level"]).column("_seq").to_pylist() == [1]
    with_prov = store.history("node:t1", ["level"], prov=True)
    assert with_prov.column("_seq").to_pylist() == [1, 2]
    assert (
        json.loads(with_prov.column("_prov").to_pylist()[1])["level"]["t"] == "2024-01-01T00:02:00Z"
    )
    # Another property of the row was not touched, so its history has one entry either way.
    assert store.history("node:t1", ["name"], prov=True).column("_seq").to_pylist() == [1]
    # The whole row did change.
    assert store.history("node:t1").column("_seq").to_pylist() == [1, 2]


def test_a_property_can_carry_provenance_of_its_own_within_one_row(store: Store):
    metered = Provenance("meter-feed", FactKind.observed, at(0.5), 0.9)
    store.commit(
        ChangeSet(
            upserts={"Tank": [TANK]},
            provenance=OBSERVED,
            property_provenance={"node:t1": {"capacity": metered, "name": OBSERVED}},
        ),
        meta(t=at(1)),
    )
    # The entry equal to the row's is not stored twice.
    assert _stored(store) == {"*": OBSERVED.to_json(), "capacity": metered.to_json()}
    found = store.get("node:t1")
    assert found.provenance_of("capacity") == metered
    assert found.provenance_of("level") == OBSERVED


def test_an_update_needs_no_row_provenance_when_each_property_has_its_own(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    level = Provenance("scada", FactKind.observed, at(1.75))
    status = Provenance("scada", FactKind.observed, at(2))
    store.commit(
        ChangeSet(
            upserts={"Tank": [{"uid": "node:t1", "level": 3.0, "status": "low"}]},
            property_provenance={"node:t1": {"level": level, "status": status}},
        ),
        meta(t=at(2)),
    )
    found = store.get("node:t1")
    assert found.provenance == {"*": OBSERVED, "level": level, "status": status}
    assert (found.values["level"], found.values["status"]) == (3.0, "low")


@pytest.mark.parametrize(
    ("row", "own", "message"),
    [
        # one written property is left without any provenance
        ({"uid": "node:t1", "level": 3.0, "status": "low"}, {"level": OBSERVED}, "without"),
        # provenance for a property the row does not write
        ({"uid": "node:t1", "level": 3.0}, {"level": OBSERVED, "status": OBSERVED}, "not write"),
        # a new object needs provenance of its own, for the properties left null
        (
            {"uid": "node:t9", "name": "T9", "capacity": 1.0},
            {"name": OBSERVED, "capacity": OBSERVED},
            "its own",
        ),
        ({"uid": "node:t1", "level": 3.0}, {"level": "scada"}, "malformed"),
    ],
)
def test_property_provenance_that_does_not_cover_the_row_is_refused(
    store: Store, row: dict, own: dict, message: str
):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    head = store.head().id
    with pytest.raises(ViolationError) as caught:
        store.commit(
            ChangeSet(upserts={"Tank": [row]}, property_provenance={row["uid"]: own}),
            meta(t=at(2)),
        )
    found = caught.value.violations
    assert {violation.rule for violation in found} == {"provenance"}
    assert any(message in violation.message for violation in found)
    assert store.head().id == head


def test_the_rule_on_fact_kinds_covers_every_property_of_a_row(store: Store):
    store.commit(ChangeSet(upserts={"Tank": [TANK]}, provenance=OBSERVED), meta(t=at(1)))
    simulated = Provenance("model", FactKind.simulated, at(2))
    with pytest.raises(ViolationError) as caught:
        store.commit(
            ChangeSet(
                upserts={"Tank": [{"uid": "node:t1", "level": 3.0, "status": "low"}]},
                provenance=Provenance("operator", FactKind.assumed, at(2)),
                property_provenance={"node:t1": {"level": simulated}},
            ),
            meta("manual", at(2)),
        )
    assert [violation.rule for violation in caught.value.violations] == ["fact_kind"]
