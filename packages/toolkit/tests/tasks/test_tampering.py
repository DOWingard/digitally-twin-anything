"""Shared state changed behind the toolkit's back, by accident or on purpose.

The task state file, the ledger of sanctioned writes, results that were already
merged, the manifest file itself: each can be edited, emptied, swapped or
half-written by something other than the toolkit. None of that may be taken
over silently, and none of it may leave a batch that cannot be got out of.
"""

import hashlib
import json
import os
import shutil

import pytest

SOURCES = ("erp", "sensors", "sheets")


def _note(ws):
    return ws.root / ".dta" / "tasks.pending.tmp"


def _codes(entries):
    return [entry["code"] for entry in entries]


def _verify(ws):
    return ws.run("status", "--verify").result["verified"]


def _submit_all(ws):
    ws.open("3")
    for source in SOURCES:
        ws.finish(
            f"profile-{source}",
            ws.profile_patch(source),
            files={"report.txt": "the real findings\n"},
        )


def _raw_file(directory, name: bytes, content: bytes = b"x") -> None:
    """Create a file whose name is given as bytes; skip where the file system refuses it."""
    try:
        with open(os.path.join(os.fsencode(directory), name), "wb") as handle:
            handle.write(content)
    except OSError:
        pytest.skip("this file system only accepts names that are valid text")


# -- the task state file -------------------------------------------------------


def test_a_hand_edited_state_file_is_never_written_back_as_the_toolkits_own(ws):
    ws.open("3")
    alice = ws.claim("profile-erp", holder="alice")
    bob = ws.claim("profile-sensors", holder="bob")

    # One agent gives itself the other's lease, with a token of its own choosing.
    path = ws.workspace.tasks_state_path
    state = json.loads(path.read_text())
    tasks = state["batches"][0]["tasks"]
    tasks["profile-sensors"]["lease"]["token_sha256"] = hashlib.sha256(b"mine").hexdigest()
    tasks["profile-erp"]["lease"]["expires_at"] = "2036-01-01T00:00:00Z"
    path.write_text(json.dumps(state, indent=2))
    edited = ws.state_bytes()

    renewal = ws.run("claim", "profile-erp", "--holder", "alice", "--renew", "--token", alice)
    assert _codes(renewal.refused("tasks.state.tampered")["reasons"]) == [
        "tasks.state.tampered",
        "tasks.foreign.modified",
    ]
    ws.run("claim", "profile-sheets", "--holder", "carol").refused("tasks.state.tampered")
    # Nothing rewrote the file, so the edit is still what it was: a change nobody sanctioned.
    assert ws.state_bytes() == edited
    assert ws.workspace.foreign_changes() == [{"path": ".dta/tasks.json", "change": "modified"}]

    ws.write_result("profile-sensors", ws.profile_patch("sensors"))
    assert ws.submit("profile-sensors", "mine").reasons == ["tasks.foreign.modified"]
    assert ws.submit("profile-sensors", bob).reasons == [
        "tasks.lease.token",
        "tasks.foreign.modified",
    ]
    assert ws.manifest().sources["sensors"].profile is None
    assert ws.abort()["state_file_changed"] is True


def test_no_lease_is_given_or_extended_while_shared_files_are_disturbed(ws):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice")
    state = ws.state_bytes()
    (ws.root / "stray.txt").write_text("x")
    renew = ("claim", "profile-erp", "--holder", "alice", "--renew", "--token", token)

    ws.run("claim", "profile-sensors", "--holder", "bob").refused("tasks.foreign.added")
    ws.run(*renew).refused("tasks.foreign.added")
    assert ws.state_bytes() == state

    (ws.root / "stray.txt").unlink()
    assert ws.run("claim", "profile-sensors", "--holder", "bob").code == 0
    assert ws.run(*renew).result["renewals"] == 1


# -- results that were already merged ------------------------------------------


@pytest.mark.parametrize(
    "part", ["output/report.txt", "output/result.json", "input/extra", "task.json", "left-here"]
)
def test_a_merged_result_altered_after_its_batch_closed_is_not_taken_into_the_next(ws, part):
    _submit_all(ws)
    assert ws.merge()["batch_closed"] is True
    target = ws.tasks / "profile-erp" / part
    original = target.read_bytes() if target.exists() else None
    target.write_text("something else entirely\n")

    detail = ws.run("plan", "--stage", "4", "--open").refused("tasks.results.changed")
    assert all(problem["path"].startswith("tasks/profile-erp") for problem in detail["problems"])
    assert ws.status()["open_batch"] is None and not ws.workspace.ledger_path.exists()
    assert _codes(_verify(ws)["problems"]) == ["tasks.results.changed"]

    if original is None:
        target.unlink()
    else:
        target.write_bytes(original)
    assert _verify(ws)["clean"] is True
    assert ws.open("4")["opened"]["batch"] == "b2"


def test_a_merged_result_is_retired_by_removing_its_directory_whole(ws):
    _submit_all(ws)
    ws.merge()
    (ws.output("profile-erp") / "report.txt").write_text("something else entirely\n")
    ws.run("plan", "--stage", "4", "--open").refused("tasks.results.changed")
    shutil.rmtree(ws.tasks / "profile-erp")
    assert ws.open("4")["opened"]["batch"] == "b2"
    assert ws.clean()


def test_abort_reports_what_happened_to_the_task_directories_too(ws):
    ws.open("3")
    ws.finish("profile-erp", ws.profile_patch("erp"), files={"report.txt": "the real findings\n"})
    ws.merge("profile-erp")
    ws.claim("profile-sensors", holder="careless")
    (ws.output("profile-erp") / "report.txt").write_text("something else entirely\n")

    aborted = ws.abort()
    assert aborted["batch_closed"] is True
    assert _codes(aborted["findings"]) == ["tasks.zone.violation"]
    assert aborted["findings"][0]["detail"]["problems"] == [
        {"path": "tasks/profile-erp/output", "problem": "changed since it was submitted"}
    ]
    # Closing the batch did not make the altered result acceptable.
    ws.run("plan", "--stage", "3", "--open").refused("tasks.results.changed")


# -- the ledger ----------------------------------------------------------------


def test_an_unreadable_ledger_stops_every_write_and_abort_is_still_the_way_out(ws):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.workspace.ledger_path.write_text("")  # emptied, not forged
    state = ws.state_bytes()

    assert _codes(_verify(ws)["problems"]) == ["tasks.ledger.unreadable"]
    ws.run("claim", "profile-sensors", "--holder", "bob").refused("tasks.ledger.unreadable")
    ws.run("claim", "profile-erp", "--holder", "alice", "--renew", "--token", token).refused(
        "tasks.ledger.unreadable"
    )
    assert ws.submit("profile-erp", token).reasons == ["tasks.ledger.unreadable"]
    ws.run("abort", "profile-erp").refused("tasks.ledger.unreadable")
    assert ws.state_bytes() == state and not _note(ws).exists()

    aborted = ws.abort()
    assert aborted["batch_closed"] is True
    assert _codes(aborted["findings"]) == ["tasks.ledger.unreadable"]
    assert not ws.workspace.ledger_path.exists() and not _note(ws).exists()
    assert ws.open("3")["opened"]["batch"] == "b2"
    assert ws.clean()


def test_a_merge_leaves_the_manifest_alone_while_the_ledger_is_unreadable(ws):
    _submit_all(ws)
    ws.workspace.ledger_path.write_text("{")
    before = ws.manifest_bytes(), ws.state_bytes()
    refused = ws.run("merge", "--all").refused("tasks.merge.refused")["refused"]
    assert [_codes(entry["reasons"]) for entry in refused] == [["tasks.ledger.unreadable"]] * 3
    assert (ws.manifest_bytes(), ws.state_bytes()) == before


def test_an_interrupted_write_waits_for_a_usable_ledger_instead_of_failing_every_command(ws):
    ws.open("3")
    ws.crash_recording("tasks.json", "claim", "profile-erp", "--holder", "alice")
    ws.workspace.ledger_path.write_text("")

    assert _codes(_verify(ws)["problems"]) == ["tasks.ledger.unreadable"]
    assert _note(ws).is_file()  # still owed to the ledger
    ws.run("claim", "profile-sensors", "--holder", "bob").refused("tasks.ledger.unreadable")
    assert ws.abort()["batch_closed"] is True
    assert not _note(ws).exists() and not ws.workspace.ledger_path.exists()


def test_a_leftover_ledger_that_cannot_be_read_blocks_the_next_batch_until_removed(ws):
    ws.open("3")
    ws.crash("close.state_written", "abort", "--batch")
    assert ws.status()["open_batch"] is None
    ws.workspace.ledger_path.write_text("{ not json")
    for command in (("plan", "--stage", "3", "--open"), ("merge", "--all"), ("abort", "--batch")):
        ws.run(*command).refused("tasks.ledger.unreadable")
    ws.workspace.ledger_path.unlink()
    assert ws.open("3")["opened"]["batch"] == "b2"


# -- abort after an interrupted merge ------------------------------------------


def test_an_abort_does_not_disown_a_patch_that_is_already_in_the_manifest(ws):
    ws.open("3")
    ws.finish(
        "profile-erp",
        ws.profile_patch("erp"),
        proposals=[{"target": "ontology", "description": "add a type"}],
    )
    ws.claim("profile-sensors", holder="bob")
    ws.crash("merge.manifest_written", "merge", "profile-erp")
    assert ws.manifest().sources["erp"].profile is not None
    assert ws.task("profile-erp")["status"] == "submitted"

    ws.run("abort", "profile-erp").refused("tasks.abort.applied")
    assert ws.task("profile-erp")["status"] == "submitted"
    aborted = ws.abort()
    assert aborted["completed"] == ["profile-erp"]
    assert aborted["abandoned"] == ["profile-sensors", "profile-sheets"]
    assert aborted["batch_closed"] is True

    record = ws.task("profile-erp")
    assert record["status"] == "merged"
    assert record["patch_keys"] == ["sources.erp.profile", "sources.erp.refresh_period"]
    status = ws.status()
    assert status["merged"] == ["profile-erp"]
    assert [entry["proposal"]["description"] for entry in status["proposals"]] == ["add a type"]
    assert ws.open("3")["opened"]["tasks"] == ["profile-sensors", "profile-sheets"]


def test_an_interrupted_merge_can_be_completed_after_an_abort_was_refused(ws):
    ws.open("3")
    ws.finish("profile-erp", ws.profile_patch("erp"))
    ws.crash("merge.manifest_written", "merge", "profile-erp")
    ws.run("abort", "profile-erp").refused("tasks.abort.applied")
    assert ws.merge("profile-erp")["merged"] == ["profile-erp"]
    assert ws.clean()


def test_a_submitted_task_whose_patch_is_not_in_the_manifest_is_simply_abandoned(ws):
    ws.open("3")
    ws.finish("profile-erp", ws.profile_patch("erp"))
    aborted = ws.abort("profile-erp")
    assert (aborted["abandoned"], aborted["completed"]) == (["profile-erp"], [])
    assert ws.manifest().sources["erp"].profile is None


# -- names that are not text ---------------------------------------------------


def test_a_name_that_is_not_text_is_recorded_exactly_and_stays_watched(ws):
    name = b"notes-caf\xe9.txt"
    key = "hex/" + name.hex()
    ws.tasks.mkdir()
    _raw_file(ws.tasks, name, b"kept by the orchestrator")
    ws.open("3")
    assert list(ws.state()["batches"][0]["baseline"]) == [key]
    assert ws.clean()

    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    _raw_file(ws.tasks, name, b"changed")
    outcome = ws.submit("profile-erp", token)
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": f"tasks/{key}", "problem": "changed since the batch opened"}
    ]
    _raw_file(ws.tasks, name, b"kept by the orchestrator")
    assert ws.submit("profile-erp", token).code == 0


def test_a_file_whose_name_is_not_text_is_accepted_neither_as_input_nor_as_output(ws):
    name = b"caf\xe9.csv"
    spelled = "hex/" + name.hex()
    ws.open("3")
    _raw_file(ws.tasks / "profile-sensors" / "input", name)
    detail = ws.run("claim", "profile-sensors", "--holder", "bob").refused("tasks.input.not_plain")
    assert detail["paths"] == [spelled]

    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    _raw_file(ws.output("profile-erp"), name)
    outcome = ws.submit("profile-erp", token)
    assert outcome.reasons == ["tasks.output.special", "tasks.artifact.undeclared"]
    assert outcome.reason("tasks.output.special")["paths"] == [spelled]
    # Naming it in the result does not make it acceptable.
    ws.write_result("profile-erp", ws.profile_patch("erp"), artifacts=[spelled])
    assert ws.submit("profile-erp", token).reasons == ["tasks.output.special"]


# -- the manifest file itself --------------------------------------------------


def test_a_pipe_in_place_of_the_manifest_is_refused_without_being_read(ws):
    ws.open("3")
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.workspace.manifest_path.unlink()
    os.mkfifo(ws.workspace.manifest_path)

    # Reading the pipe would never return. Each command runs in a process of its
    # own, with a time limit, so that it fails here instead of hanging the suite.
    outcome = ws.spawn("submit", "profile-erp", "--token", token)
    assert outcome.reasons == ["tasks.foreign.removed", "tasks.manifest.unreadable"]
    ws.spawn("claim", "profile-sensors", "--holder", "bob").refused("tasks.foreign.removed")
    ws.spawn("plan", "--stage", "3").could_not_run("tasks.manifest.not_plain")
    assert ws.spawn("status", "--verify").result["verified"]["clean"] is False
    aborted = ws.spawn("abort", "--batch").result
    assert aborted["batch_closed"] is True
    assert _codes(aborted["findings"]) == ["tasks.foreign.removed", "tasks.manifest.unreadable"]
    ws.spawn("plan", "--stage", "3", "--open").could_not_run("tasks.manifest.not_plain")
    assert not ws.workspace.ledger_path.exists()


def test_a_link_in_place_of_the_manifest_is_not_followed(ws, tmp_path):
    ws.open("3")
    copy = tmp_path / "manifest-copy.yaml"
    copy.write_bytes(ws.manifest_bytes())
    ws.workspace.manifest_path.unlink()
    ws.workspace.manifest_path.symlink_to(copy)

    ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.foreign.removed")
    ws.run("plan", "--stage", "3").could_not_run("tasks.manifest.not_plain")
    problems = _verify(ws)["problems"]
    assert "tasks.manifest.unreadable" in _codes(problems)


# -- files that change while they are being read -------------------------------


def test_a_watched_file_vanishing_mid_comparison_means_run_again_and_changes_nothing(
    ws, monkeypatch
):
    from dta_toolkit.workspace import Workspace

    ws.open("3")
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.finish("profile-sheets", ws.profile_patch("sheets"))
    before = ws.manifest_bytes(), ws.state_bytes()

    def vanish(self):
        raise FileNotFoundError("a file that was listed is gone")

    with monkeypatch.context() as patched:
        patched.setattr(Workspace, "fingerprint", vanish)
        for command in (
            ("claim", "profile-sensors", "--holder", "bob"),
            ("claim", "profile-erp", "--holder", "agent-1", "--renew", "--token", token),
            ("submit", "profile-erp", "--token", token),
            ("merge", "--all"),
            ("abort", "--batch"),
            ("status", "--verify"),
        ):
            ws.run(*command).could_not_run("tasks.files.unsettled")
        assert (ws.manifest_bytes(), ws.state_bytes()) == before

    assert ws.submit("profile-erp", token).code == 0
    assert ws.merge()["merged"] == ["profile-erp", "profile-sheets"]


def test_a_batch_is_not_opened_on_a_baseline_that_could_not_be_taken(ws, monkeypatch):
    from dta_toolkit.workspace import Workspace

    def vanish(self):
        raise FileNotFoundError("a file that was listed is gone")

    with monkeypatch.context() as patched:
        patched.setattr(Workspace, "fingerprint", vanish)
        ws.run("plan", "--stage", "3", "--open").could_not_run("tasks.files.unsettled")
    assert ws.status()["open_batch"] is None and not ws.workspace.ledger_path.exists()
    assert ws.open("3")["opened"]["batch"] == "b1"
