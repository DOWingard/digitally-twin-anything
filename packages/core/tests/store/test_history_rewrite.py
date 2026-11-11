"""What the store does when the database is changed behind its back: a
version taken away or replaced, a commit it did not write, and a commit that
lands on a branch while the store is writing to it."""

from __future__ import annotations

import logging

import pytest
from dta_core.store import ForeignCommit, HeadMoved, HistoryRewritten, Store
from dta_store_testkit import at, changes, meta, raw_connection, sql, water_ontology


def _three_versions(store: Store) -> list:
    return [
        store.commit(changes({"Zone": [{"uid": f"zone:{n}", "name": f"Z{n}"}]}), meta(t=at(n)))
        for n in (1, 2, 3)
    ]


def _raw(store: Store, *statements: str | tuple, branch: str = "main") -> list:
    connection = raw_connection(store.settings, branch)
    try:
        results = []
        for statement in statements:
            text, parameters = statement if isinstance(statement, tuple) else (statement, None)
            results.append(sql(connection, text, parameters))
        return results
    finally:
        connection.close()


STRAY_ZONE = (
    "INSERT INTO `_object` (`uid`, `type`) VALUES ('zone:x', 'Zone')",
    "INSERT INTO `zone` (`uid`, `name`, `_prov`) VALUES ('zone:x', 'by hand', '{}')",
)


def test_a_version_taken_away_is_detected(store: Store):
    versions = _three_versions(store)
    assert store.head().seq == 3
    _raw(store, "CALL DOLT_RESET('--hard', 'HEAD~1')")
    for call in (
        store.head,
        lambda: store.table("Zone"),
        lambda: store.get("zone:1", at="head"),
        lambda: store.versions(),
        lambda: store.commit(changes({}), meta(t=at(9))),
        lambda: store.branch_create("after"),
    ):
        with pytest.raises(HistoryRewritten) as caught:
            call()
        assert caught.value.code == "store.history_rewritten"
    detail = caught.value.detail
    assert (detail["branch"], detail["known_head"], detail["known_seq"]) == (
        "main",
        versions[2].id,
        3,
    )
    assert detail["found_head"] == versions[1].id
    # Nothing was written on top of the altered history.
    assert _raw(store, "SELECT HASHOF('HEAD')")[0][0][0] == versions[1].id


def test_a_version_replaced_by_another_at_the_same_position_is_detected(store: Store):
    versions = _three_versions(store)
    message, date = _raw(
        store,
        ("SELECT `message`, `date` FROM `dolt_log` WHERE `commit_hash` = %s", (versions[2].id,)),
    )[0][0]
    # The replacement carries the record of the version it replaces, so a look
    # at the head alone would take it for the same version.
    replaced = _raw(
        store,
        "CALL DOLT_RESET('--hard', 'HEAD~1')",
        *STRAY_ZONE,
        ("CALL DOLT_COMMIT('-A', '-m', %s, '--date', %s)", (message, date.isoformat())),
    )[-1][0][0]
    assert replaced != versions[2].id
    with pytest.raises(HistoryRewritten) as caught:
        store.head()
    assert caught.value.detail["known_head"] == versions[2].id
    assert caught.value.detail["found_head"] == replaced
    with pytest.raises(HistoryRewritten):
        store.commit(changes({"Zone": [{"uid": "zone:4", "name": "Z4"}]}), meta(t=at(4)))
    assert _raw(store, "SELECT HASHOF('HEAD')")[0][0][0] == replaced


def test_rewriting_below_the_head_is_detected_even_when_new_versions_follow(store: Store):
    versions = _three_versions(store)
    messages = dict(_raw(store, "SELECT `commit_hash`, `message` FROM `dolt_log`")[0])
    dates = dict(_raw(store, "SELECT `commit_hash`, `date` FROM `dolt_log`")[0])
    statements: list = ["CALL DOLT_RESET('--hard', 'HEAD~2')", *STRAY_ZONE]
    for version in versions[1:]:
        statements.append(
            (
                "CALL DOLT_COMMIT('-A', '--allow-empty', '-m', %s, '--date', %s)",
                (messages[version.id], dates[version.id].isoformat()),
            )
        )
    _raw(store, *statements)
    with pytest.raises(HistoryRewritten):
        store.resolve("#3")
    with pytest.raises(HistoryRewritten):
        store.history("zone:1")


def test_a_commit_the_store_did_not_write_is_refused(store: Store):
    versions = _three_versions(store)
    foreign = _raw(store, *STRAY_ZONE, "CALL DOLT_COMMIT('-A', '-m', 'fixed by hand')")[-1][0][0]
    for call in (
        store.head,
        lambda: store.table("Zone"),
        lambda: store.commit(changes({}), meta(t=at(9))),
    ):
        with pytest.raises(ForeignCommit) as caught:
            call()
        assert caught.value.code == "store.foreign_commit"
    assert caught.value.detail == {"branch": "main", "commit": foreign, "position": 4}
    # A store started afterwards refuses it as well.
    later = Store(store.settings, water_ontology())
    try:
        with pytest.raises(ForeignCommit):
            later.head()
    finally:
        later.close()
    # Versions written before it are still what they were.
    assert _raw(store, ("SELECT COUNT(*) FROM `zone` AS OF %s", (versions[2].id,)))[0][0][0] == 3


def test_a_commit_claiming_the_wrong_position_is_refused(store: Store):
    versions = _three_versions(store)
    message = _raw(
        store, ("SELECT `message` FROM `dolt_log` WHERE `commit_hash` = %s", (versions[1].id,))
    )[0][0][0]
    _raw(store, *STRAY_ZONE, ("CALL DOLT_COMMIT('-A', '-m', %s)", (message,)))
    with pytest.raises(ForeignCommit):
        store.head()


def test_another_branch_made_again_under_the_same_name_is_read_afresh(
    store: Store, caplog: pytest.LogCaptureFixture
):
    _three_versions(store)
    store.branch_create("what-if", at="#1")
    store.commit(
        changes({"Zone": [{"uid": "zone:b", "name": "B"}]}, kind="assumed"),
        meta("scenario", at(4)),
        branch="what-if",
    )
    assert store.head("what-if").seq == 2
    _raw(store, "CALL DOLT_BRANCH('-D', 'what-if')", "CALL DOLT_BRANCH('what-if', 'main')")
    with caplog.at_level(logging.WARNING, logger="dta"):
        head = store.head("what-if")
    assert (head.seq, head.id) == (3, store.head().id)
    assert [r.levelname for r in caplog.records if "reading it again" in r.getMessage()] == [
        "WARNING"
    ]
    assert store.get("zone:b", branch="what-if") is None
    assert sorted(store.table("Zone", branch="what-if").column("uid").to_pylist()) == [
        "zone:1",
        "zone:2",
        "zone:3",
    ]


@pytest.mark.parametrize("step", ["begun", "written", "sealed"])
def test_a_commit_landing_on_the_branch_during_a_write_is_never_overwritten(
    store: Store, step: str
):
    """The store publishes only onto the head it read; it does not win a race it
    cannot see, and what it had built is not left anywhere."""
    versions = _three_versions(store)
    landed: list[str] = []

    def interfere(reached: str) -> None:
        if reached == step:
            landed.append(
                _raw(store, *STRAY_ZONE, "CALL DOLT_COMMIT('-A', '-m', 'fixed by hand')")[-1][0][0]
            )

    store._fault = interfere
    try:
        with pytest.raises(HeadMoved) as caught:
            store.commit(changes({"Zone": [{"uid": "zone:4", "name": "Z4"}]}), meta(t=at(4)))
    finally:
        store._fault = None
    assert caught.value.code == "store.head_moved"
    assert caught.value.detail["written"] is False
    assert caught.value.detail["read"] == versions[2].id
    head, uids = _raw(store, "SELECT HASHOF('HEAD')", "SELECT `uid` FROM `zone` ORDER BY `uid`")
    assert head[0][0] == landed[0]
    assert [uid for (uid,) in uids] == ["zone:1", "zone:2", "zone:3", "zone:x"]
    assert _raw(store, "SELECT COUNT(*) FROM `dolt_status`")[0][0][0] == 0
    with pytest.raises(ForeignCommit):
        store.head()
