"""A process killed inside a shared-file write: after the rename, before the ledger entry.

The file on disk is then exactly what the toolkit meant to write, but the
ledger still holds the hash of the old content. Left alone, that would look
like a change made by someone else and block the whole batch.
"""

import hashlib
import json
import time

SOURCES = ("erp", "sensors", "sheets")


def _submit_all(ws):
    ws.open("3")
    for source in SOURCES:
        ws.finish(f"profile-{source}", ws.profile_patch(source))


def _profiled(ws):
    return sorted(key for key, source in ws.manifest().sources.items() if source.profile)


def _note(ws):
    return ws.root / ".dta" / "tasks.pending.tmp"


def test_a_merge_killed_before_its_manifest_write_was_recorded_recovers_on_rerun(ws):
    _submit_all(ws)
    ws.crash_recording("manifest.yaml", "merge", "--all")
    assert _profiled(ws) == ["erp"]
    assert ws.task("profile-erp")["status"] == "submitted"
    # The ledger does not know the new manifest: as it stands it is a foreign change.
    assert ws.workspace.foreign_changes() == [{"path": "manifest.yaml", "change": "modified"}]

    rerun = ws.spawn("merge", "--all").result
    assert rerun["merged"] == ["profile-erp", "profile-sensors", "profile-sheets"]
    assert rerun["batch_closed"] is True
    assert _profiled(ws) == ["erp", "sensors", "sheets"]
    assert not _note(ws).exists()


def test_a_merge_killed_before_its_state_write_was_recorded_recovers_on_rerun(ws):
    _submit_all(ws)
    ws.crash_recording("tasks.json", "merge", "profile-erp")
    # Both writes reached the disk; only the second one's ledger entry is missing.
    assert ws.task("profile-erp")["status"] == "merged"
    assert ws.workspace.foreign_changes() == [{"path": ".dta/tasks.json", "change": "modified"}]

    assert ws.clean()  # any next command completes the entry first
    assert ws.workspace.foreign_changes() == []
    assert ws.merge()["merged"] == ["profile-sensors", "profile-sheets"]
    assert _profiled(ws) == ["erp", "sensors", "sheets"]


def test_a_submit_killed_before_its_state_write_was_recorded_does_not_block_the_batch(ws):
    ws.open("3")
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.crash_recording("tasks.json", "submit", "profile-erp", "--token", token)
    assert ws.task("profile-erp")["status"] == "submitted"

    other = ws.claim("profile-sensors", holder="bob")
    ws.write_result("profile-sensors", ws.profile_patch("sensors"))
    assert ws.submit("profile-sensors", other).code == 0
    assert ws.merge()["merged"] == ["profile-erp", "profile-sensors"]


def test_a_claim_killed_after_the_lease_was_written_holds_the_task_until_it_expires(ws):
    ws.open("3")
    ws.crash_recording("tasks.json", "claim", "profile-erp", "--holder", "alice", "--ttl", "1")
    # The lease is on disk but its token died with the process.
    assert ws.task("profile-erp")["lease"]["holder"] == "alice"
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.lease.held")
    time.sleep(1.2)
    taken = ws.run("claim", "profile-erp", "--holder", "bob").result
    assert taken["taken_over_from"] == "alice"
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    assert ws.submit("profile-erp", taken["token"]).code == 0


def test_a_closing_write_killed_before_it_was_recorded_is_not_taken_for_tampering(ws):
    _submit_all(ws)
    ws.merge("profile-erp")
    ws.merge("profile-sensors")
    ws.crash_recording("tasks.json", "merge", "profile-sheets")
    assert ws.status()["open_batch"] is None and ws.workspace.ledger_path.is_file()
    assert ws.open("4")["opened"]["batch"] == "b2"
    assert ws.clean()


def test_a_write_that_was_announced_but_never_made_is_forgotten(ws):
    ws.open("3")
    state = ws.state_bytes()
    ws.crash_recording("tasks.pending.tmp", "claim", "profile-erp", "--holder", "alice")
    assert _note(ws).is_file() and ws.state_bytes() == state
    assert ws.run("claim", "profile-erp", "--holder", "bob").code == 0
    assert not _note(ws).exists()
    assert ws.task("profile-erp")["lease"]["holder"] == "bob"
    assert ws.clean()


def test_only_the_announced_content_is_accepted_after_an_interruption(ws):
    _submit_all(ws)
    ws.crash_recording("manifest.yaml", "merge", "--all")
    # Somebody edits the half-recorded manifest before the toolkit runs again.
    path = ws.workspace.manifest_path
    path.write_text(path.read_text() + "# slipped in\n")
    edited = ws.manifest_bytes()

    outcome = ws.run("merge", "--all")
    refused = outcome.refused("tasks.merge.refused")["refused"]
    assert {entry["task"] for entry in refused} == {f"profile-{s}" for s in SOURCES}
    assert all(
        [reason["code"] for reason in entry["reasons"]] == ["tasks.foreign.modified"]
        for entry in refused
    )
    assert ws.manifest_bytes() == edited
    assert not _note(ws).exists()
    assert all(ws.task(f"profile-{s}")["status"] == "submitted" for s in SOURCES)


def test_a_planted_note_cannot_bless_a_different_file(ws):
    ws.open("3")
    stray = ws.root / "stray.txt"
    stray.write_text("x")
    _note(ws).write_text(
        json.dumps({"path": "stray.txt", "sha256": hashlib.sha256(b"x").hexdigest()})
    )
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    assert ws.submit("profile-erp", token).reasons == ["tasks.foreign.added"]
    assert not _note(ws).exists()


def test_the_leftover_of_an_interrupted_task_description_write_is_cleared_by_the_claim(ws):
    ws.open("3")
    leftover = ws.tasks / "profile-erp" / ".task.json.k3j2h1.tmp"
    leftover.write_text("{")
    assert ws.clean()
    token = ws.claim("profile-erp")
    assert not leftover.exists()
    ws.write_result("profile-erp", ws.profile_patch("erp"))

    # Once the task is claimed no such file can come from the toolkit any more.
    leftover.write_text("{")
    outcome = ws.submit("profile-erp", token)
    assert outcome.reasons == ["tasks.zone.violation"]
    leftover.unlink()
    assert ws.submit("profile-erp", token).code == 0
