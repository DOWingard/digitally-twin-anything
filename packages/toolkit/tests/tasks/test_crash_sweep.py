"""Every command that writes, killed before each change it makes to the file system.

For each command the test first counts the changes it makes when left alone
(files created, replaced or removed, directories made or removed). It then
starts from the same situation once per change, kills the command just before
that change, runs the same command again and carries the build to its end.

Wherever the kill fell, the end has to be the same: every task merged, every
patch in the manifest, no batch open, nothing left to repair. A kill is the
real thing: the process ends on the spot, with no clean-up of any kind.
"""

import os
import shutil

import pytest

SOURCES = ("erp", "sensors", "sheets")
TASKS = [f"profile-{source}" for source in SOURCES]
# Commands that take a lease run an hour in the past, so that a lease left
# behind by a killed process is over when the build is carried on.
PAST = -3600
# A second run can find that the first one had already done the deciding write.
ALREADY_DONE = {"tasks.batch.open", "tasks.lease.held"}


def _result(ws, task):
    ws.write_result(task, ws.profile_patch(task.removeprefix("profile-")), files={"a.txt": "a"})


def _finish(ws, task):
    token = ws.claim(task, holder=f"agent-{task}")
    _result(ws, task)
    assert ws.submit(task, token).code == 0


def _open(ws):
    return ("plan", "--stage", "3", "--open"), 0


def _claim(ws):
    ws.open("3")
    return ("claim", "profile-erp", "--holder", "first"), PAST


def _take_over(ws):
    ws.open("3")
    assert ws.run("claim", "profile-erp", "--holder", "old", clock_offset=2 * PAST).code == 0
    (ws.output("profile-erp") / "half.txt").write_text("left by the holder that went away")
    return ("claim", "profile-erp", "--holder", "new"), PAST


def _renew(ws):
    ws.open("3")
    held = ws.run("claim", "profile-erp", "--holder", "first", clock_offset=PAST).result
    renewal = ("--renew", "--token", held["token"], "--ttl", "60")
    return ("claim", "profile-erp", "--holder", "first", *renewal), PAST


def _submit(ws):
    ws.open("3")
    token = ws.claim("profile-erp")
    _result(ws, "profile-erp")
    return ("submit", "profile-erp", "--token", token), 0


def _merge_one(ws):
    ws.open("3")
    _finish(ws, "profile-erp")
    return ("merge", "profile-erp"), 0


def _merge_all(ws):
    ws.open("3")
    for task in TASKS:
        _finish(ws, task)
    return ("merge", "--all"), 0


def _abort_one(ws):
    ws.open("3")
    _finish(ws, "profile-erp")
    return ("abort", "profile-erp"), 0


def _abort_batch(ws):
    ws.open("3")
    _finish(ws, "profile-erp")
    ws.claim("profile-sensors", holder="busy")
    return ("abort", "--batch"), 0


def _abort_the_last(ws):
    ws.open("3")
    for task in TASKS[:2]:
        _finish(ws, task)
        ws.merge(task)
    return ("abort", "profile-sheets"), 0


def _abort_after_a_killed_merge(ws):
    ws.open("3")
    _finish(ws, "profile-erp")
    ws.crash("merge.manifest_written", "merge", "profile-erp")
    return ("abort", "--batch"), 0


SITUATIONS = {
    "open": _open,
    "claim": _claim,
    "take over": _take_over,
    "renew": _renew,
    "submit": _submit,
    "merge one": _merge_one,
    "merge all": _merge_all,
    "abort one": _abort_one,
    "abort batch": _abort_batch,
    "abort the last": _abort_the_last,
    "abort after a killed merge": _abort_after_a_killed_merge,
}


def _carry_through(ws, where):
    """From wherever the batch stands, do everything that is left and check the end."""
    for _ in range(3):  # a task that was given up comes back in the next batch
        report = ws.run("status", "--verify").result
        if report["open_batch"] is None:
            if report["merged"] == TASKS:
                break
            ws.open("3")
            report = ws.run("status", "--verify").result
        assert report["verified"]["clean"], (where, report["verified"]["problems"])
        for task in report["open_batch"]["tasks"]:
            if task["status"] in ("merged", "abandoned"):
                continue
            if task["status"] in ("open", "claimed"):
                _finish(ws, task["id"])
            ws.merge(task["id"])

    end = ws.run("status", "--verify").result
    assert end["open_batch"] is None and end["verified"]["clean"], (where, end)
    assert end["merged"] == TASKS, (where, end["merged"])
    manifest = ws.manifest()
    for source in SOURCES:
        assert manifest.sources[source].profile.datasets["main"].rows == 3, (where, source)
    assert not ws.workspace.ledger_path.exists(), where
    assert not (ws.root / ".dta" / "tasks.pending.tmp").exists(), where
    assert ws.open("4")["opened"]["batch"], where


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs a process that can be forked")
@pytest.mark.parametrize("situation", list(SITUATIONS))
def test_a_command_killed_before_any_of_its_changes_is_repaired_by_running_it_again(
    make_ws, tmp_path, situation
):
    template = make_ws("template")
    command, offset = SITUATIONS[situation](template)

    reference = template.clone(tmp_path / "reference")
    changes = reference.count_changes(*command, clock_offset=offset)
    assert changes > 0
    _carry_through(reference, f"{situation}, not killed")

    for number in range(1, changes + 1):
        where = f"{situation}, killed before change {number} of {changes}"
        ws = template.clone(tmp_path / f"killed-{number}")
        ws.crash_before_change(number, *command, clock_offset=offset)
        again = ws.run(*command, clock_offset=offset)
        assert again.code == 0 or again.error["code"] in ALREADY_DONE, (where, again.payload)
        _carry_through(ws, where)
        shutil.rmtree(ws.root)
