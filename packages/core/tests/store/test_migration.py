"""A change of ontology that only adds is one new version; everything written
before it stays readable as it was, and anything else is refused with reasons."""

from __future__ import annotations

import dataclasses

import pytest
from dta_core.schema import Field, Kind, Ontology
from dta_core.store import (
    InvalidInput,
    MigrationRequired,
    OntologyMismatch,
    Store,
    TimeRegression,
    ViolationError,
)
from dta_store_testkit import (
    at,
    changes,
    database_state,
    meta,
    water_ontology,
    water_ontology_v2,
)

SEED = {
    "Zone": [{"uid": "zone:1", "name": "North"}],
    "Tank": [
        {"uid": "node:t1", "name": "T1", "capacity": 50.0, "level": 5.0, "status": "ok"},
        {"uid": "node:t2", "name": "T2", "capacity": 80.0, "tags": ["a", "b"]},
    ],
    "Junction": [{"uid": "node:j1", "name": "J1", "elevation": 12.0, "zone": "zone:1"}],
}


def _migrated(store: Store) -> Store:
    """Two versions under the first ontology, then the migration as version 3."""
    store.commit(changes(SEED), meta(t=at(1)))
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="inferred"),
        meta("derived", at(2)),
    )
    version = store.migrate(water_ontology_v2(), meta("migration", at(3), note="add valves"))
    assert version.seq == 3
    return store


def _points(table, *names: str) -> list[tuple]:
    return list(zip(*(table.column(name).to_pylist() for name in ("_seq", *names)), strict=True))


def _with_tank(ontology: Ontology, **replacement) -> Ontology:
    tank = dataclasses.replace(ontology.type("Tank"), **replacement)
    return dataclasses.replace(
        ontology, types=tuple(tank if t.name == "Tank" else t for t in ontology.types)
    )


# -- an additive change --------------------------------------------------------


def test_a_migration_is_one_version_that_changes_no_stored_value(store: Store):
    before = None
    store.commit(changes(SEED), meta(t=at(1)))
    before = {name: store.table(name).to_pylist() for name in ("Zone", "Tank", "Junction")}
    version = store.migrate(water_ontology_v2(), meta("migration", at(3), note="add valves"))
    entry = store.versions("head")[0]
    assert (version.seq, entry.cause, entry.note, entry.actor) == (
        2,
        "migration",
        "add valves",
        "test",
    )
    assert [v.seq for v in store.versions(cause="migration")] == [2]
    # The store now works with the new ontology; the old one stays in force where it was.
    assert store.ontology.hash == water_ontology_v2().hash
    assert store.ontology_at().hash == water_ontology_v2().hash
    assert store.ontology_at("#1").hash == water_ontology().hash
    assert store.ontology_at("#1").to_json() == water_ontology().to_json()
    # Nothing differs between the two versions, and the working set is clean.
    assert store.diff("#1", "#2") == {}
    for name in ("Zone", "Tank", "Junction"):
        assert store.changes(name, "#2..#2").num_rows == 0
        old_ontology = water_ontology()
        assert store.table(name, ontology=old_ontology).to_pylist() == before[name]
    assert database_state(store.settings)["status"] == ()


def test_versions_before_a_migration_read_as_they_were(store: Store):
    _migrated(store)
    # Under the ontology the store now has: what is new did not exist then.
    then = store.table("Tank", at="#2")
    assert then.schema.names == store.table("Tank").schema.names
    assert then.column("material").to_pylist() == [None, None]
    assert then.column("vent").to_pylist() == [None, None]
    assert sorted(then.column("level").to_pylist(), key=str) == [6.0, None]
    assert store.table("Valve", at="#2").num_rows == 0
    assert store.table("Valve", at="#2").schema.names == ["uid", "name", "setting", "on_pipe"]
    assert store.table("Tank", filter={"material": "steel"}, at="#2").num_rows == 0
    assert store.table("Tank", filter={"material": None}, at="#2").num_rows == 2
    old = store.get("node:t1", at="#1")
    assert old.values["level"] == 5.0 and old.values["material"] is None
    assert store.edges(at="#2").num_rows == store.edges(at="#3").num_rows == 1
    # Under the ontology of the time: exactly what was there.
    first = water_ontology()
    as_then = store.table("Tank", at="#2", ontology=first)
    assert "material" not in as_then.schema.names
    assert as_then.schema.names == ["uid", *[f.name for f in first.type("Tank").fields]]
    assert store.get("node:t1", at="#2", ontology=first).values == {
        **{f.name: None for f in first.type("Tank").fields},
        "name": "T1",
        "capacity": 50.0,
        "level": 6.0,
        "status": "ok",
    }


def test_what_a_migration_added_can_be_written_and_read_afterwards(store: Store):
    _migrated(store)
    written = store.commit(
        changes(
            {
                "Valve": [{"uid": "valve:1", "name": "V1", "setting": "open"}],
                "Tank": [
                    {"uid": "node:t1", "material": "steel", "status": "flooded", "vent": "valve:1"}
                ],
                "Junction": [{"uid": "node:j1", "pressure": 3.2}],
            }
        ),
        meta(t=at(4)),
    )
    assert written.seq == 4
    tank = store.get("node:t1")
    assert (tank.values["material"], tank.values["status"], tank.values["vent"]) == (
        "steel",
        "flooded",
        "valve:1",
    )
    assert store.table("Valve").to_pylist() == [
        {"uid": "valve:1", "name": "V1", "setting": "open", "on_pipe": None}
    ]
    assert store.table("Tank", filter={"material": "steel"}).column("uid").to_pylist() == [
        "node:t1"
    ]
    assert store.get("node:j1").values["pressure"] == 3.2
    assert "Tank.vent" in set(store.edges().column("edge_type").to_pylist())
    # The rules that came with the migration hold from here on.
    for bad, rule in [
        ({"Tank": [{"uid": "node:t1", "capacity": 2_000_000.0}]}, "check"),
        ({"Junction": [{"uid": "node:j1", "pressure": -1.0}]}, "range"),
        ({"Valve": [{"uid": "valve:2", "name": "V2", "setting": "ajar"}]}, "enum"),
        ({"Tank": [{"uid": "node:t2", "vent": "node:j1"}]}, "reference"),
    ]:
        with pytest.raises(ViolationError) as caught:
            store.commit(changes(bad), meta(t=at(5)))
        assert [v.rule for v in caught.value.violations] == [rule], bad
    assert store.head().seq == 4


def test_reads_over_a_range_that_crosses_a_migration(store: Store):
    _migrated(store)
    store.commit(
        changes(
            {
                "Valve": [{"uid": "valve:1", "name": "V1"}],
                "Tank": [{"uid": "node:t1", "material": "steel", "level": 7.0}],
            }
        ),
        meta(t=at(4)),
    )
    # The migration rewrote every row and changed none: it adds no point.
    history = store.history("node:t1", ["level", "material"])
    assert _points(history, "level", "material") == [
        (1, 5.0, None),
        (2, 6.0, None),
        (4, 7.0, "steel"),
    ]
    assert _points(store.history("node:t1"), "_change") == [
        (1, "added"),
        (2, "modified"),
        (4, "modified"),
    ]
    assert _points(store.history("node:t2"), "_change") == [(1, "added")]
    assert _points(store.changes("Tank"), "uid", "_change") == [
        (1, "node:t1", "added"),
        (1, "node:t2", "added"),
        (2, "node:t1", "modified"),
        (4, "node:t1", "modified"),
    ]
    assert _points(store.changes("Valve"), "uid", "_change") == [(4, "valve:1", "added")]
    assert _points(store.changes("Junction", "#2..head"), "uid") == []
    # A difference taken across it names what changed, with the earlier values.
    difference = store.diff("#2", "#4")
    assert sorted(difference) == ["Tank", "Valve"]
    row = difference["Tank"].to_pylist()[0]
    assert (row["_change"], row["uid"], row["level"], row["material"]) == (
        "modified",
        "node:t1",
        7.0,
        "steel",
    )
    assert (row["_before"]["level"], row["_before"]["material"]) == (6.0, None)
    assert difference["Valve"].column("_change").to_pylist() == ["added"]
    assert store.diff("#2", "#3") == {} and store.diff("#1", "#3", types=["Valve"]) == {}
    # The same answers under the first ontology, which knows nothing of the additions.
    first = water_ontology()
    assert "material" not in store.history("node:t1", ontology=first).schema.names
    assert _points(store.history("node:t1", ["level"], ontology=first), "level") == [
        (1, 5.0),
        (2, 6.0),
        (4, 7.0),
    ]


def test_a_store_given_the_old_ontology_reads_but_does_not_write(store: Store):
    _migrated(store)
    old = Store(store.settings, water_ontology())
    try:
        assert old.head().seq == 3
        assert old.table("Tank").num_rows == 2 and "material" not in old.table("Tank").schema.names
        assert old.get("node:t1").values["level"] == 6.0
        assert old.ontology_at().hash == water_ontology_v2().hash
        with pytest.raises(OntologyMismatch) as caught:
            old.commit(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(4)))
        assert caught.value.code == "store.ontology_mismatch"
        with pytest.raises(OntologyMismatch):
            old.validate(changes({"Zone": [{"uid": "zone:2", "name": "x"}]}), meta(t=at(4)))
        assert old.head().seq == 3
    finally:
        old.close()
    # A store given the new one carries on.
    new = Store(store.settings, water_ontology_v2())
    try:
        assert (
            new.commit(changes({"Valve": [{"uid": "valve:1", "name": "V"}]}), meta(t=at(4))).seq
            == 4
        )
        assert new.migrate(water_ontology_v2(), meta("migration", at(5))).seq == 4
    finally:
        new.close()


def test_migrating_to_the_ontology_in_force_changes_nothing(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    before = database_state(store.settings)
    assert store.migrate(water_ontology(), meta("migration", at(2))).seq == 1
    assert database_state(store.settings) == before


def test_a_migration_is_recorded_as_one_and_in_time_order(store: Store):
    store.commit(changes(SEED), meta(t=at(5)))
    with pytest.raises(InvalidInput) as caught:
        store.migrate(water_ontology_v2(), meta("manual", at(6)))
    assert caught.value.code == "store.invalid_meta"
    with pytest.raises(InvalidInput) as caught:
        store.commit(changes({}), meta("migration", at(6)))
    assert caught.value.code == "store.invalid_meta"
    with pytest.raises(TimeRegression):
        store.migrate(water_ontology_v2(), meta("migration", at(4)))
    assert store.head().seq == 1 and store.ontology.hash == water_ontology().hash


# -- anything else -------------------------------------------------------------


def test_a_change_that_is_not_additive_is_refused_with_every_reason(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    first = water_ontology()
    tank = first.type("Tank")
    changed = {
        "capacity": dataclasses.replace(tank.field("capacity"), kind=Kind.integer),
        "level": dataclasses.replace(tank.field("level"), required=True),
    }
    fields = [changed.get(f.name, f) for f in tank.fields if f.name != "notes"]
    fields.append(Field("serial", Kind.string, required=True))
    worse = _with_tank(first, fields=tuple(fields), checks=())
    worse = dataclasses.replace(
        worse,
        types=tuple(t for t in worse.types if t.name != "TwinSyncRun"),
        enums={"TankStatus": ("ok", "out of service")},
    )
    before = database_state(store.settings)
    with pytest.raises(MigrationRequired) as caught:
        store.migrate(worse, meta("migration", at(2)))
    assert caught.value.code == "store.migration_required"
    reasons = caught.value.changes
    for expected in (
        "property Tank.notes is removed",
        "property Tank.capacity changes kind",
        "property Tank.level changes required",
        "new property Tank.serial is required",
        "check Tank.level_within_capacity is removed",
        "enum TankStatus loses the values",
        "enum TwinRunStatus is removed",
        "type TwinSyncRun is removed",
    ):
        assert [r for r in reasons if expected in r], (expected, reasons)
    assert len(reasons) == 8
    # Nothing moved: not the data, not the ontology, not the store.
    assert database_state(store.settings) == before
    assert store.ontology.hash == first.hash and store.ontology_at().hash == first.hash
    assert [b.name for b in store.branches()] == ["main"]
    assert (
        store.commit(
            changes({"Tank": [{"uid": "node:t1", "notes": "still here"}]}), meta(t=at(3))
        ).seq
        == 2
    )


def test_a_new_check_that_stored_rows_break_refuses_the_migration(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    first = water_ontology()
    tank = first.type("Tank")
    stricter = _with_tank(
        first,
        fields=(*tank.fields, Field("material", Kind.string)),
        checks=(*tank.checks, ("small", "capacity < 60"), ("named", "name <> 'T1'")),
    )
    before = database_state(store.settings)
    with pytest.raises(MigrationRequired) as caught:
        store.migrate(stricter, meta("migration", at(2)))
    reasons = caught.value.changes
    assert len(reasons) == 2
    assert any("check Tank.small" in r for r in reasons)
    assert any("check Tank.named" in r for r in reasons)
    # The column that would have been added is not there either.
    assert database_state(store.settings) == before
    assert store.ontology_at().hash == first.hash
    assert store.commit(changes({}), meta(t=at(3))).seq == 2
    # Once the rows satisfy it, the same kind of change goes through.
    relaxed = _with_tank(
        first,
        fields=(*tank.fields, Field("material", Kind.string)),
        checks=(*tank.checks, ("small", "capacity < 100")),
    )
    assert store.migrate(relaxed, meta("migration", at(4))).seq == 3
    with pytest.raises(ViolationError):
        store.commit(changes({"Tank": [{"uid": "node:t1", "capacity": 150.0}]}), meta(t=at(5)))


# -- branches ------------------------------------------------------------------


def test_a_branch_gets_a_new_ontology_by_merging_the_live_branch(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    store.branch_create("what-if")
    store.migrate(water_ontology_v2(), meta("migration", at(2)))
    store.commit(changes({"Valve": [{"uid": "valve:1", "name": "V1"}]}), meta(t=at(3)))
    # The branch still stands on the ontology it was created under.
    assert store.ontology_at(branch="what-if").hash == water_ontology().hash
    assert store.table("Tank", branch="what-if").column("material").to_pylist() == [None, None]
    assert store.table("Valve", branch="what-if").num_rows == 0
    proposal = changes({"Tank": [{"uid": "node:t1", "material": "steel"}]}, kind="assumed")
    with pytest.raises(OntologyMismatch):
        store.commit(proposal, meta("scenario", at(4)), branch="what-if")

    merged = store.merge("main", "what-if", meta("merge", at(4)))
    assert (merged.seq, merged.branch) == (2, "what-if")
    assert store.ontology_at(branch="what-if").hash == water_ontology_v2().hash
    assert store.ontology_at("#1", branch="what-if").hash == water_ontology().hash
    assert store.table("Valve", branch="what-if").num_rows == 1
    assert store.commit(proposal, meta("scenario", at(5)), branch="what-if").seq == 3
    assert store.get("node:t1", branch="what-if").values["material"] == "steel"
    # The merge brought new table definitions and one new object, and changed no other row.
    assert _points(store.changes("Tank", branch="what-if"), "uid", "_change") == [
        (1, "node:t1", "added"),
        (1, "node:t2", "added"),
        (3, "node:t1", "modified"),
    ]
    assert _points(store.changes("Valve", branch="what-if"), "uid", "_change") == [
        (2, "valve:1", "added")
    ]
    assert _points(store.history("node:t1", branch="what-if"), "_change") == [
        (1, "added"),
        (3, "modified"),
    ]
    assert sorted(store.diff("#1", "#2", branch="what-if")) == ["Valve"]

    # And the proposal can come back to the live branch.
    back = store.merge("what-if", "main", meta("merge", at(6)))
    assert back.seq == 4 and store.get("node:t1").values["material"] == "steel"
    assert _points(store.history("node:t1"), "_change") == [(1, "added"), (4, "modified")]
    assert _points(store.changes("Tank"), "uid", "_change") == [
        (1, "node:t1", "added"),
        (1, "node:t2", "added"),
        (4, "node:t1", "modified"),
    ]
    assert _points(store.changes("Valve"), "uid", "_change") == [(3, "valve:1", "added")]
