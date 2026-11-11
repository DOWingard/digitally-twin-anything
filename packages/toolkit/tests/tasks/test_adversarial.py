"""What a careless or wrong agent could try, and how each attempt ends."""

import json
import os
import subprocess
import sys
import time

import pytest

_HOLD_LOCK = """
import sys, time
from pathlib import Path
from dta_toolkit.workspace import Workspace
with Workspace(Path(sys.argv[1])).exclusive():
    print("ready", flush=True)
    time.sleep(float(sys.argv[2]))
"""


# -- task ids ------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_id",
    [
        "../outside",
        "../../etc/passwd",
        "..",
        ".",
        "/abs/path",
        "profile-erp/../profile-sensors",
        "profile-erp/output",
        "a\\b",
        "profile-erp\n",
        "Profile-Erp",
        "profile erp",
        "x" * 201,
        "profile-érp",
    ],
)
def test_a_task_id_cannot_name_anything_outside_the_task_directories(ws, tmp_path, task_id):
    ws.open("3")
    before = sorted(str(path) for path in tmp_path.rglob("*") if ".dta/logs" not in str(path))
    for command in (
        ("claim", task_id, "--holder", "mallory"),
        ("claim", task_id, "--holder", "mallory", "--renew", "--token", "t"),
        ("submit", task_id, "--token", "t"),
        ("merge", task_id),
        ("abort", task_id),
    ):
        ws.run(*command).could_not_run("tasks.id.invalid")
    after = sorted(str(path) for path in tmp_path.rglob("*") if ".dta/logs" not in str(path))
    assert after == before


def test_an_empty_task_id_is_rejected(ws):
    ws.open("3")
    ws.run("claim", "", "--holder", "mallory").could_not_run("tasks.id.invalid")
    ws.run("submit", "", "--token", "t").could_not_run("tasks.id.invalid")


# -- the task directory itself -------------------------------------------------


def test_a_task_directory_swapped_for_a_link_is_refused(ws, tmp_path):
    ws.open("3")
    token = ws.claim("profile-erp")
    elsewhere = tmp_path / "elsewhere"
    os.rename(ws.tasks / "profile-erp", elsewhere)
    (ws.tasks / "profile-erp").symlink_to(elsewhere)
    outcome = ws.submit("profile-erp", token)
    assert sorted(outcome.reasons) == ["tasks.output.missing", "tasks.zone.violation"]

    # Another task's submit sees it too.
    other = ws.claim("profile-sheets")
    ws.write_result("profile-sheets", ws.profile_patch("sheets"))
    assert ws.submit("profile-sheets", other).reasons == ["tasks.zone.violation"]


def test_an_output_directory_pointed_at_another_tasks_output_is_refused(ws):
    ws.open("3")
    token = ws.claim("profile-erp")
    victim = ws.claim("profile-sensors", holder="bob")
    ws.write_result("profile-sensors", ws.profile_patch("sensors"))
    os.rmdir(ws.output("profile-erp"))
    ws.output("profile-erp").symlink_to(ws.output("profile-sensors"))
    assert ws.submit("profile-erp", token).reasons == ["tasks.output.missing"]
    assert ws.submit("profile-sensors", victim).reasons == ["tasks.zone.violation"]


def test_output_written_before_the_claim_blocks_the_claim(ws):
    ws.open("3")
    (ws.output("profile-erp") / "early.txt").write_text("written without a lease")
    detail = ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.zone.violation")
    assert detail["problems"] == [
        {
            "path": "tasks/profile-erp/output",
            "problem": "not empty before the task was claimed",
        }
    ]
    (ws.output("profile-erp") / "early.txt").unlink()
    assert ws.run("claim", "profile-erp", "--holder", "alice").code == 0


def test_a_forged_task_description_blocks_the_claim(ws):
    ws.open("3")
    (ws.tasks / "profile-erp" / "task.json").write_text('{"writes": {"owned_keys": ["twin"]}}')
    ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.spec.changed")
    assert ws.task("profile-erp")["status"] == "open"


def test_whatever_was_under_tasks_when_the_batch_opened_must_stay_as_it_was(ws):
    ws.tasks.mkdir()
    (ws.tasks / "README.txt").write_text("kept by the orchestrator")
    (ws.tasks / "archive").mkdir()
    (ws.tasks / "archive" / "old.json").write_text("{}")
    ws.open("3")
    assert ws.clean()
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))

    (ws.tasks / "archive" / "old.json").write_text('{"changed": true}')
    outcome = ws.submit("profile-erp", token)
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": "tasks/archive", "problem": "changed since the batch opened"}
    ]
    (ws.tasks / "archive" / "old.json").write_text("{}")
    (ws.tasks / "README.txt").unlink()
    outcome = ws.submit("profile-erp", token)
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": "tasks/README.txt", "problem": "removed since the batch opened"}
    ]
    (ws.tasks / "README.txt").write_text("kept by the orchestrator")
    assert ws.submit("profile-erp", token).code == 0


def test_opening_discards_what_an_abandoned_attempt_left_and_never_follows_links(ws, tmp_path):
    ws.open("3")
    ws.claim("profile-erp")
    (ws.output("profile-erp") / "half.txt").write_text("from the first attempt")
    ws.abort()

    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("must survive")
    os.rename(ws.tasks / "profile-sensors", tmp_path / "moved")
    (ws.tasks / "profile-sensors").symlink_to(precious)

    ws.open("3")
    for source in ("erp", "sensors", "sheets"):
        base = ws.tasks / f"profile-{source}"
        assert not base.is_symlink()
        assert sorted(p.name for p in base.iterdir()) == ["input", "output"]
        assert list((base / "output").iterdir()) == []
    assert (precious / "keep.txt").read_text() == "must survive"
    assert ws.task("profile-erp")["status"] == "open" and ws.task("profile-erp")["lease"] is None


def test_tasks_replaced_by_a_link_cannot_be_opened(ws, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    ws.tasks.symlink_to(tmp_path / "elsewhere")
    ws.run("plan", "--stage", "3", "--open").could_not_run("tasks.zone.unusable")
    assert list((tmp_path / "elsewhere").iterdir()) == []
    assert not ws.workspace.ledger_path.exists()


# -- the state file and the ledger ---------------------------------------------


def test_a_removed_state_file_is_not_mistaken_for_a_fresh_start(ws):
    ws.open("3")
    ws.finish("profile-erp", ws.profile_patch("erp"))
    ws.workspace.tasks_state_path.unlink()
    # Without its state the toolkit sees no batch, but the ledger remembers one was tracked.
    ws.run("claim", "profile-sensors", "--holder", "bob").could_not_run("tasks.task.unknown")
    for command in (("plan", "--stage", "3", "--open"), ("merge", "--all"), ("abort", "--batch")):
        ws.run(*command).refused("tasks.state.tampered")
    assert ws.workspace.ledger_path.is_file()
    assert ws.manifest().sources["erp"].profile is None


def test_a_state_file_edited_to_close_the_batch_is_caught(ws):
    ws.open("3")
    path = ws.workspace.tasks_state_path
    state = json.loads(path.read_text())
    state["open_batch"] = None
    for task in state["batches"][0]["tasks"].values():
        task["status"] = "merged"
    path.write_text(json.dumps(state))
    ws.run("plan", "--stage", "4", "--open").refused("tasks.state.tampered")
    ws.run("merge", "--all").refused("tasks.state.tampered")


def test_a_damaged_state_file_stops_every_command(ws):
    ws.open("3")
    ws.workspace.tasks_state_path.write_text("{ not json")
    for command in (
        ("plan", "--stage", "3"),
        ("claim", "profile-erp", "--holder", "a"),
        ("submit", "profile-erp", "--token", "t"),
        ("merge", "--all"),
        ("abort", "--batch"),
        ("status",),
    ):
        ws.run(*command).could_not_run("tasks.state.damaged")


def test_abort_is_the_way_out_and_reports_what_was_disturbed(ws):
    ws.open("3")
    token = ws.finish("profile-erp", ws.profile_patch("erp"))
    ws.merge("profile-erp")
    ws.claim("profile-sensors", holder="bob")
    ws.workspace.manifest_path.write_text(ws.workspace.manifest_path.read_text() + "# x\n")
    (ws.root / "stray.txt").write_text("x")

    aborted = ws.abort()
    assert aborted["abandoned"] == ["profile-sensors", "profile-sheets"]
    assert aborted["batch_closed"] is True and aborted["state_file_changed"] is False
    assert [finding["code"] for finding in aborted["findings"]] == [
        "tasks.foreign.modified",
        "tasks.foreign.added",
    ]
    assert not ws.workspace.ledger_path.exists()
    assert ws.task("profile-erp")["status"] == "merged"  # what was merged stays merged
    ws.submit("profile-erp", token).refused("tasks.batch.none")
    # The orchestrator may now repair by hand; the next batch starts from what it finds.
    assert ws.open("3")["opened"]["tasks"] == ["profile-sensors", "profile-sheets"]
    assert ws.clean()


def test_abort_of_one_task_and_of_things_that_cannot_be_aborted(ws):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    assert ws.abort("profile-erp") == {
        "batch": "b1",
        "abandoned": ["profile-erp"],
        "completed": [],
        "batch_closed": False,
        "findings": [],
        "state_file_changed": False,
    }
    ws.submit("profile-erp", token).refused("tasks.submit.closed")
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.claim.closed")
    assert ws.abort("profile-erp")["abandoned"] == []  # already given up

    ws.finish("profile-sensors", ws.profile_patch("sensors"))
    ws.merge("profile-sensors")
    ws.run("abort", "profile-sensors").refused("tasks.abort.merged")
    ws.run("abort", "profile-unknown").could_not_run("tasks.task.unknown")
    ws.run("abort").could_not_run("tasks.usage")
    ws.run("abort", "profile-erp", "--batch").could_not_run("tasks.usage")
    # An abandoned task's directory is no longer anybody's concern.
    (ws.tasks / "profile-erp" / "junk.txt").write_text("x")
    assert ws.clean()
    assert ws.abort("profile-sheets")["batch_closed"] is True
    assert ws.abort() == {
        "batch": None,
        "abandoned": [],
        "completed": [],
        "batch_closed": False,
        "findings": [],
        "state_file_changed": False,
    }


# -- status --------------------------------------------------------------------


def test_status_shows_the_batch_and_verify_shows_what_a_submit_would_refuse(ws):
    assert ws.status() == {"open_batch": None, "batches": 0, "merged": [], "proposals": []}
    ws.open("3", "--store-version", "commit-9")
    ws.claim("profile-erp", holder="alice")
    report = ws.status()
    tasks = {task["id"]: task for task in report["open_batch"]["tasks"]}
    assert report["open_batch"]["stage"] == "3"
    assert tasks["profile-erp"]["status"] == "claimed"
    assert tasks["profile-erp"]["lease"]["holder"] == "alice"
    assert tasks["profile-erp"]["lease"]["expired"] is False
    assert tasks["profile-erp"]["store_version"] == "commit-9"
    assert tasks["profile-sensors"] == {
        "id": "profile-sensors",
        "status": "open",
        "lease": None,
        "submitted_at": None,
        "merged_at": None,
        "store_version": "commit-9",
    }
    assert "token" not in json.dumps(report)
    assert "verified" not in report

    state = ws.state_bytes()
    (ws.root / "stray.txt").write_text("x")
    (ws.tasks / "profile-sheets" / "output" / "planted").write_text("x")

    def move(manifest):
        manifest.sources["sensors"].location = "https://moved/v1"

    ws.edit_manifest(move)
    checked = ws.run("status", "--verify")
    assert checked.code == 0  # a query reports; it does not fail
    problems = {problem["code"]: problem for problem in checked.result["verified"]["problems"]}
    assert checked.result["verified"]["clean"] is False
    assert sorted(problems) == ["tasks.foreign.added", "tasks.pin.manifest", "tasks.zone.violation"]
    assert problems["tasks.pin.manifest"]["detail"]["task"] == "profile-sensors"
    assert ws.state_bytes() == state


# -- the lock ------------------------------------------------------------------


def test_commands_wait_for_the_workspace_lock(ws):
    ws.open("3")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(ws.root), "1.5"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        started = time.monotonic()
        claimed = ws.spawn("claim", "profile-erp", "--holder", "alice")
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=30)
    assert claimed.code == 0
    assert waited > 1.0, "the claim did not wait for the lock holder"


# -- logging -------------------------------------------------------------------


def test_every_step_and_every_refusal_is_logged_without_the_token(ws):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice")
    ws.write_result("profile-erp", {"twin.id": "hijacked"})
    ws.submit("profile-erp", token)
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.submit("profile-erp", token)
    ws.merge("profile-erp")
    ws.abort()
    lines = [
        json.loads(line)
        for line in (ws.root / ".dta" / "logs" / "toolkit.log").read_text().splitlines()
    ]
    messages = [line["message"] for line in lines]
    for expected in (
        "batch opened",
        "lease taken",
        "submit refused",
        "task submitted",
        "merge applied",
        "task abandoned",
        "batch closed",
    ):
        assert expected in messages, expected
    refused = next(line for line in lines if line["message"] == "submit refused")
    assert refused["level"] == "warn"
    assert refused["data"]["codes"] == ["tasks.patch.not_owned"]
    assert refused["data"]["task"] == "profile-erp" and refused["data"]["holder"] == "alice"
    assert all(line["trace_id"] for line in lines)
    assert token not in (ws.root / ".dta" / "logs" / "toolkit.log").read_text()


# -- no workspace --------------------------------------------------------------


def test_every_command_needs_a_workspace_and_creates_none(tmp_path):
    from dta_toolkit.cli import app
    from typer.testing import CliRunner

    nowhere = tmp_path / "nowhere"
    for command in (
        ("plan", "--stage", "3"),
        ("plan", "--stage", "3", "--open"),
        ("claim", "profile-erp", "--holder", "alice"),
        ("claim", "profile-erp", "--holder", "alice", "--renew", "--token", "t"),
        ("submit", "profile-erp", "--token", "t"),
        ("merge", "--all"),
        ("abort", "--batch"),
        ("status",),
        ("status", "--verify"),
    ):
        done = CliRunner().invoke(app, ["--workspace", str(nowhere), "--json", "tasks", *command])
        assert done.exit_code == 2, command
        assert json.loads(done.stdout)["error"]["code"] == "workspace.missing"
    assert not nowhere.exists()
