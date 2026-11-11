"""The commit log: what is recorded with a version, how versions are selected
and filtered, and what a table read can be asked for."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pyarrow as pa
import pytest
from dta_core.store import (
    EPOCH,
    InvalidInput,
    NotInstalled,
    OntologyMismatch,
    Store,
    StoreClosed,
    StoreSettings,
    StoreUnavailable,
    TimeRegression,
    UnknownProperty,
    UnknownType,
    UnknownVersion,
    VersionRef,
    stored_ontology,
)
from dta_store_testkit import (
    at,
    changes,
    meta,
    raw_connection,
    sql,
    water_ontology,
    water_ontology_v2,
)


def _zone(n: int) -> dict:
    return {"Zone": [{"uid": f"zone:{n}", "name": f"Z{n}"}]}


def _seqs(found) -> list[int]:
    return [version.seq for version in found]


# -- what a version records ----------------------------------------------------


def test_everything_recorded_with_a_version_comes_back(store: Store):
    started = datetime.now(UTC)
    extra = {"backfill": True, "window": {"from": "2023-12-31", "parts": [1, 2]}, "ratio": 0.5}
    written = store.commit(
        changes(_zone(1)),
        meta(
            "sync",
            at(1),
            actor="loader",
            run="twinsyncrun:42",
            trace="trace-abc",
            valid_from=at(0),
            valid_to=at(1),
            note="hourly load",
            extra=extra,
        ),
    )
    install = store.versions("#0")[0]
    entry = store.versions("head")[0]
    assert entry.ref == written and isinstance(written, VersionRef)
    assert (entry.id, entry.seq, entry.t, entry.branch) == (written.id, 1, at(1), "main")
    assert (entry.cause, entry.actor, entry.run, entry.trace) == (
        "sync",
        "loader",
        "twinsyncrun:42",
        "trace-abc",
    )
    assert (entry.valid_from, entry.valid_to, entry.note) == (at(0), at(1), "hourly load")
    assert entry.extra == extra and written.extra == extra
    assert entry.parents == (install.id,)
    assert entry.written_at.tzinfo is not None
    assert (
        started - timedelta(seconds=5)
        <= entry.written_at
        <= datetime.now(UTC) + timedelta(seconds=5)
    )
    # The same through every way of naming the version, also for a store started later.
    for selector in ("head", "#1", written.id, written, at(1).isoformat()):
        assert store.resolve(selector) == [written]
        assert store.resolve(selector)[0].extra == extra
    assert store.head().extra == extra
    later = Store(store.settings, water_ontology())
    try:
        assert later.versions("head")[0] == entry
        assert later.versions("head")[0].extra == extra
    finally:
        later.close()
    # What is left out is recorded as absent, not invented.
    plain = store.versions("#0..head")[0]
    assert (plain.run, plain.valid_from, plain.valid_to, plain.note, plain.extra) == (
        None,
        None,
        None,
        "",
        {},
    )


def test_install_is_version_zero_at_the_time_it_is_given(make_store: Callable[..., Store]):
    store = make_store(install=False)
    with pytest.raises(NotInstalled) as caught:
        store.head()
    assert caught.value.code == "store.not_installed"
    first = store.install("twin-7", t=at(0), actor="ops", note="first install")
    entry = store.versions()[0]
    assert (first.seq, entry.cause, entry.actor, entry.note, entry.t) == (
        0,
        "install",
        "ops",
        "first install",
        at(0),
    )
    connection = raw_connection(store.settings)
    try:
        assert sql(connection, "SELECT `twin_id`, `format` FROM `dta_meta`") == (("twin-7", 1),)
    finally:
        connection.close()
    # Installing again is not a new version; nothing may be written before the install.
    assert store.install("twin-7", t=at(5)) == first
    assert _seqs(store.versions()) == [0]
    with pytest.raises(TimeRegression):
        store.commit(changes(_zone(1)), meta(t=at(-1)))
    # A store given another ontology does not take the installed twin for its own.
    other = Store(store.settings, water_ontology_v2())
    try:
        with pytest.raises(OntologyMismatch) as caught:
            other.install("twin-7")
        assert caught.value.code == "store.ontology_mismatch"
    finally:
        other.close()


def test_a_process_that_did_not_install_the_twin_reads_its_ontology_first(
    make_store: Callable[..., Store],
):
    store = make_store(install=False)
    with pytest.raises(NotInstalled) as caught:
        stored_ontology(store.settings)
    assert caught.value.code == "store.not_installed"
    store.install("twin-8", t=at(0))
    found = stored_ontology(store.settings)
    assert found == store.ontology
    assert found.hash == store.ontology.hash
    second = Store(store.settings, found)
    try:
        assert second.head().seq == 0
    finally:
        second.close()


def test_install_defaults_to_the_earliest_time(store: Store):
    assert store.versions()[0].t == EPOCH == datetime(1970, 1, 1, tzinfo=UTC)
    assert store.commit(changes(_zone(1)), meta(t=datetime(1971, 1, 1, tzinfo=UTC))).seq == 1


def test_version_time_is_utc_to_the_millisecond(store: Store):
    plus_two = timezone(timedelta(hours=2))
    local = datetime(2024, 3, 1, 14, 30, 15, 123987, tzinfo=plus_two)
    written = store.commit(changes(_zone(1)), meta(t=local))
    expected = datetime(2024, 3, 1, 12, 30, 15, 123000, tzinfo=UTC)
    assert written.t == expected and written.t.utcoffset() == timedelta(0)
    assert store.versions("head")[0].t == expected
    # The same instant, or any later one, is accepted next; an earlier one is not.
    assert store.commit(changes({}), meta(t=expected)).seq == 2
    with pytest.raises(TimeRegression) as caught:
        store.commit(changes({}), meta(t=expected - timedelta(milliseconds=1)))
    assert caught.value.code == "store.time_regression"
    assert store.resolve("2024-03-01T14:30:15.123+02:00")[0].seq == 2
    assert store.resolve("2024-03-01T12:30:15.122+00:00")[0].seq == 0


# -- filtering -----------------------------------------------------------------


def test_versions_are_filtered_by_what_was_recorded(store: Store):
    store.commit(
        changes(_zone(1)), meta("sync", at(1), actor="loader", run="r1", extra={"backfill": True})
    )
    store.commit(
        changes(_zone(2)), meta("sync", at(2), actor="loader", run="r2", extra={"backfill": False})
    )
    store.commit(
        changes(_zone(3)),
        meta(
            "sync", at(3), actor="replayer", run="r2", extra={"backfill": True, "source": "scada"}
        ),
    )
    store.commit(
        changes(_zone(4), kind="assumed"), meta("manual", at(4), actor="ana", extra={"backfill": 1})
    )
    store.commit(changes(_zone(5)), meta("sync", at(5), actor="loader"))

    assert _seqs(store.versions()) == [0, 1, 2, 3, 4, 5]
    assert _seqs(store.versions(extra={"backfill": True})) == [1, 3]
    assert _seqs(store.versions(extra={"backfill": False})) == [2]
    assert _seqs(store.versions(extra={"backfill": 1})) == [4]
    assert _seqs(store.versions(extra={"source": "scada"})) == [3]
    assert _seqs(store.versions(extra={"backfill": True, "source": "scada"})) == [3]
    assert _seqs(store.versions(extra={"source": None})) == []
    assert _seqs(store.versions(extra={})) == [0, 1, 2, 3, 4, 5]
    assert all(v.extra["backfill"] is True for v in store.versions(extra={"backfill": True}))

    assert _seqs(store.versions(cause="sync")) == [1, 2, 3, 5]
    assert _seqs(store.versions(cause="install")) == [0]
    assert _seqs(store.versions(actor="loader")) == [1, 2, 5]
    assert _seqs(store.versions(run="r2")) == [2, 3]
    assert _seqs(store.versions(cause="sync", actor="loader", extra={"backfill": True})) == [1]
    assert _seqs(store.versions("#2..head", cause="sync")) == [2, 3, 5]
    assert _seqs(store.versions("#2..#4", extra={"backfill": True})) == [3]
    assert _seqs(store.versions(newest_first=True)) == [5, 4, 3, 2, 1, 0]
    assert _seqs(store.versions(cause="sync", limit=2)) == [1, 2]
    assert _seqs(store.versions(cause="sync", limit=2, newest_first=True)) == [5, 3]
    assert _seqs(store.versions(limit=0)) == []

    for bad in (
        {"cause": "holiday"},
        {"extra": ["backfill"]},
        {"extra": {"when": datetime.now(UTC)}},
    ):
        with pytest.raises(InvalidInput) as caught:
            store.versions(**bad)
        assert caught.value.code == "store.bad_filter"


# -- selecting -----------------------------------------------------------------


def test_selectors_name_versions_by_position_id_time_and_range(store: Store):
    written = [
        store.commit(changes(_zone(n)), meta(t=at(minute)))
        for n, minute in enumerate([1, 2, 2, 5], start=1)
    ]
    iso = lambda minute: at(minute).isoformat()  # noqa: E731
    # A time names the last version written at or before it.
    assert store.resolve(iso(1))[0].seq == 1
    assert store.resolve(iso(1.5))[0].seq == 1
    assert store.resolve(iso(2))[0].seq == 3
    assert store.resolve(iso(4.999))[0].seq == 3
    assert store.resolve(iso(5))[0].seq == 4
    assert store.resolve(iso(500))[0].seq == 4
    assert store.resolve(iso(0))[0].seq == 0
    assert store.resolve("2024-01-01T01:01:00+01:00")[0].seq == 1
    with pytest.raises(UnknownVersion) as caught:
        store.resolve("1960-01-01T00:00:00+00:00")
    assert caught.value.code == "store.unknown_version"

    assert _seqs(store.resolve("#1..head")) == [1, 2, 3, 4]
    assert _seqs(store.resolve("#0..head/2")) == [0, 2, 4]
    assert _seqs(store.resolve("#1..#4/2")) == [1, 3]
    assert _seqs(store.resolve("#1..#4/10")) == [1]
    assert _seqs(store.resolve("#2..#2")) == [2]
    assert _seqs(store.resolve(f"{iso(1.5)}..{iso(5)}")) == [1, 2, 3, 4]
    assert _seqs(store.resolve(f"{written[1].id}..head")) == [2, 3, 4]
    # A range that starts before the first version starts at the first.
    assert _seqs(store.resolve("1960-01-01T00:00:00+00:00..#1")) == [0, 1]

    unknown_id = "0" * 32
    for selector, code in [
        ("#99", "store.unknown_version"),
        (unknown_id, "store.unknown_version"),
        ("#3..#1", "store.bad_selector"),
        ("#1..#99", "store.unknown_version"),
        ("yesterday", "store.bad_selector"),
        ("2024-01-01T00:00:00", "store.naive_datetime"),
        (5, "store.bad_selector"),
    ]:
        with pytest.raises((InvalidInput, UnknownVersion)) as caught:
            store.resolve(selector)
        assert caught.value.code == code, selector
    # A read of one version does not take a range.
    for call in (
        lambda: store.get("zone:1", at="#1..#2"),
        lambda: store.table("Zone", at="#1..#2"),
        lambda: store.edges(at="#1..head"),
        lambda: store.branch_create("b", at="#1..#2"),
    ):
        with pytest.raises(InvalidInput) as caught:
            call()
        assert caught.value.code == "store.bad_selector"
    # A reference to a version reads that version.
    assert store.table("Zone", at=written[1]).num_rows == 2
    assert store.table("Zone", at=iso(1)).num_rows == 1
    assert store.get("zone:4", at=written[2]) is None and store.get("zone:4", at=written[3])


# -- reading a type ------------------------------------------------------------

TANKS = [
    {
        "uid": "node:t1",
        "name": "T1",
        "capacity": 50.0,
        "level": 5.0,
        "status": "ok",
        "fill_count": 3,
    },
    {
        "uid": "node:t2",
        "name": "T2",
        "capacity": 80.0,
        "level": 40.0,
        "status": "low",
        "sealed": True,
    },
    {"uid": "node:t3", "name": "T3", "capacity": 20.0, "status": "ok", "fill_count": 9},
    {"uid": "node:t4", "name": "T4", "capacity": 65.0, "level": 12.5, "sealed": False},
]


def _uids(table: pa.Table) -> list[str]:
    return table.column("uid").to_pylist()


def test_a_table_read_takes_columns_filters_and_paging(store: Store):
    store.commit(changes({"Tank": TANKS}), meta(t=at(1)))
    whole = store.table("Tank")
    assert whole.schema.names == ["uid", *[f.name for f in water_ontology().type("Tank").fields]]
    assert sorted(_uids(whole)) == ["node:t1", "node:t2", "node:t3", "node:t4"]
    picked = store.table("Tank", columns=["level", "name"])
    assert picked.schema.names == ["uid", "level", "name"]
    assert picked.schema.field("level").type == pa.float64()
    assert sorted(picked.to_pylist(), key=lambda row: row["uid"])[2] == {
        "uid": "node:t3",
        "level": None,
        "name": "T3",
    }

    def where(condition) -> list[str]:
        return sorted(_uids(store.table("Tank", columns=["name"], filter=condition)))

    assert where({"status": "ok"}) == ["node:t1", "node:t3"]
    assert where({"status": "ok", "fill_count": 9}) == ["node:t3"]
    assert where({"level": None}) == ["node:t3"]
    assert where({"status": ["ok", "low"]}) == ["node:t1", "node:t2", "node:t3"]
    assert where({"uid": "node:t2"}) == ["node:t2"]
    assert where({"sealed": True}) == ["node:t2"]
    assert where([("capacity", ">=", 50.0), ("capacity", "<", 80.0)]) == ["node:t1", "node:t4"]
    assert where([("level", ">", 5.0)]) == ["node:t2", "node:t4"]
    assert where([("level", "<=", 5.0)]) == ["node:t1"]
    assert where([("status", "!=", "ok")]) == ["node:t2"]
    assert where([("status", "is_null", None)]) == ["node:t4"]
    assert where([("status", "not_null", None)]) == ["node:t1", "node:t2", "node:t3"]
    assert where([("name", "like", "T%")]) == ["node:t1", "node:t2", "node:t3", "node:t4"]
    assert where([("name", "like", "%3")]) == ["node:t3"]
    assert where([("uid", "like", "node:t_"), ("status", "like", "o%")]) == ["node:t1", "node:t3"]
    assert where([("name", "like", "t1")]) == []
    assert where([("name", "in", ["T1", "T4", "nobody"])]) == ["node:t1", "node:t4"]
    assert where([("name", "=", "T1' OR '1'='1")]) == []
    assert where({"status": []}) == []

    # Paging walks the type in uid order.
    assert _uids(store.table("Tank", limit=2)) == ["node:t1", "node:t2"]
    assert _uids(store.table("Tank", limit=2, offset=2)) == ["node:t3", "node:t4"]
    assert _uids(store.table("Tank", offset=3)) == ["node:t4"]
    assert _uids(store.table("Tank", limit=0)) == []
    assert _uids(store.table("Tank", filter={"status": "ok"}, limit=1, offset=1)) == ["node:t3"]

    # An order asked for is the store's: rows without a value last, ties in uid order.
    def ordered(*order: tuple[str, str], **more) -> list[str]:
        return _uids(store.table("Tank", columns=[], order_by=list(order), **more))

    assert ordered(("level", "asc")) == ["node:t1", "node:t4", "node:t2", "node:t3"]
    assert ordered(("level", "desc")) == ["node:t2", "node:t4", "node:t1", "node:t3"]
    assert ordered(("status", "desc"), ("capacity", "desc")) == [
        "node:t1",
        "node:t3",
        "node:t2",
        "node:t4",
    ]
    assert ordered(("status", "asc")) == ["node:t2", "node:t1", "node:t3", "node:t4"]
    assert ordered(("uid", "desc")) == ["node:t4", "node:t3", "node:t2", "node:t1"]
    assert ordered(("uid", "desc"), ("level", "asc")) == ordered(("uid", "desc"))
    assert ordered(("capacity", "desc"), limit=2) == ["node:t2", "node:t4"]
    assert ordered(("capacity", "desc"), limit=2, offset=2) == ["node:t1", "node:t3"]
    assert ordered(("capacity", "asc"), offset=3) == ["node:t2"]
    assert ordered(("name", "desc"), filter={"status": "ok"}) == ["node:t3", "node:t1"]

    assert store.count("Tank") == 4
    assert store.count("Tank", filter={"status": "ok"}) == 2
    assert store.count("Tank", filter=[("level", ">", 100.0)]) == 0
    assert store.count("Tank", filter={"status": []}) == 0
    assert store.count("Tank", at="#0") == 0

    with_prov = store.table("Tank", columns=["level"], prov=True, limit=1)
    assert with_prov.schema.names == ["uid", "level", "_prov"]
    assert '"k":"observed"' in with_prov.column("_prov").to_pylist()[0]

    for call, code in [
        (lambda: store.table("Reservoir"), "store.unknown_type"),
        (lambda: store.table("Node"), "store.unknown_type"),
        (lambda: store.changes("Reservoir"), "store.unknown_type"),
        (lambda: store.diff("#0", "#1", types=["Reservoir"]), "store.unknown_type"),
        (lambda: store.table("Tank", columns=["depth"]), "store.unknown_property"),
        (lambda: store.table("Tank", filter={"depth": 1}), "store.unknown_property"),
        (lambda: store.history("node:t1", ["depth"]), "store.unknown_property"),
        (lambda: store.table("Tank", filter=[("level", "~", 1)]), "store.bad_filter"),
        (lambda: store.table("Tank", filter=[("level", ">")]), "store.bad_filter"),
        (lambda: store.table("Tank", filter={"level": "deep"}), "store.bad_filter"),
        (lambda: store.table("Tank", filter={"tags": "a"}), "store.bad_filter"),
        (lambda: store.table("Tank", filter=[("name", "in", "T1")]), "store.bad_filter"),
        (lambda: store.table("Tank", filter=[("level", "like", "5%")]), "store.bad_filter"),
        (lambda: store.table("Tank", filter=[("name", "like", 5)]), "store.bad_filter"),
        (lambda: store.table("Tank", limit=-1), "store.bad_filter"),
        (lambda: store.table("Tank", offset=1.5), "store.bad_filter"),
        (lambda: store.table("Tank", order_by=[("depth", "asc")]), "store.unknown_property"),
        (lambda: store.table("Tank", order_by=[("level", "up")]), "store.bad_order"),
        (lambda: store.table("Tank", order_by=["level"]), "store.bad_order"),
        (lambda: store.table("Tank", order_by=[("tags", "asc")]), "store.bad_order"),
        (lambda: store.count("Reservoir"), "store.unknown_type"),
        (lambda: store.count("Tank", filter={"depth": 1}), "store.unknown_property"),
    ]:
        with pytest.raises((InvalidInput, UnknownType, UnknownProperty)) as caught:
            call()
        assert caught.value.code == code


# -- when the store cannot answer ----------------------------------------------


def test_a_closed_store_says_so(store: Store):
    store.commit(changes(_zone(1)), meta(t=at(1)))
    store.close()
    store.close()
    for call in (
        store.head,
        lambda: store.table("Zone"),
        lambda: store.commit(changes({}), meta(t=at(2))),
        lambda: store.install("again"),
        lambda: store.branch_create("b"),
    ):
        with pytest.raises(StoreClosed) as caught:
            call()
        assert caught.value.code == "store.closed"


def test_a_database_that_cannot_be_reached_is_reported_as_unavailable(
    server_settings: StoreSettings,
):
    nowhere = dataclasses.replace(
        server_settings, port=1, database="dta_nowhere", connect_timeout_s=2.0, pool_timeout_s=2.0
    )
    with pytest.raises(StoreUnavailable) as caught:
        store = Store(nowhere, water_ontology())
        try:
            store.head()
        finally:
            store.close()
    assert caught.value.code == "store.unavailable"
    assert server_settings.password not in str(caught.value)
    assert server_settings.password not in repr(caught.value.detail)


def test_wrong_credentials_are_reported_without_the_password(server_settings: StoreSettings):
    wrong = dataclasses.replace(
        server_settings, password="not-the-password", database="dta_nowhere", connect_timeout_s=5.0
    )
    with pytest.raises(StoreUnavailable) as caught:
        store = Store(wrong, water_ontology())
        try:
            store.head()
        finally:
            store.close()
    assert caught.value.code == "store.unavailable"
    assert "not-the-password" not in str(caught.value)
    assert "not-the-password" not in repr(caught.value.detail)
