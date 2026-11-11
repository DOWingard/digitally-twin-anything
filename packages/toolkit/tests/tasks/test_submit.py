import json
import os

import pytest


@pytest.fixture
def held(ws):
    """An open profiling batch with profile-erp claimed and a valid result written."""
    ws.open("3")
    (ws.tasks / "profile-erp" / "input" / "sample.csv").write_text("id\n1\n")
    token = ws.claim("profile-erp", holder="alice")
    ws.write_result("profile-erp", ws.profile_patch("erp"), files={"report.md": "# profile\n"})
    ws.token = token
    return ws


def _refused(ws, *codes):
    """Submit profile-erp and assert it is refused for exactly these reasons, changing nothing."""
    state, manifest = ws.state_bytes(), ws.manifest_bytes()
    outcome = ws.submit("profile-erp", ws.token)
    assert outcome.reasons == list(codes), outcome.payload
    assert ws.state_bytes() == state and ws.manifest_bytes() == manifest
    assert ws.task("profile-erp")["status"] == "claimed"
    return outcome


def test_a_clean_task_is_accepted_and_its_output_sealed(held):
    accepted = held.submit("profile-erp", held.token).result
    assert accepted["status"] == "submitted"
    assert accepted["patch_keys"] == ["sources.erp.profile", "sources.erp.refresh_period"]
    assert accepted["artifacts"] == ["report.md"]
    record = held.task("profile-erp")
    assert record["status"] == "submitted"
    assert record["output_sha256"] == accepted["output_sha256"]
    # Nothing reaches the manifest before the merge.
    assert held.manifest().sources["erp"].profile is None
    assert held.clean()


# -- the seven refusals, each on its own ---------------------------------------


def test_refused_for_a_patch_key_outside_the_owned_keys(held):
    patch = {**held.profile_patch("erp"), "sources.erp.location": "postgresql://elsewhere/erp"}
    held.write_result("profile-erp", patch, files={"report.md": "# profile\n"})
    outcome = _refused(held, "tasks.patch.not_owned")
    assert outcome.reason("tasks.patch.not_owned")["keys"] == ["sources.erp.location"]


@pytest.mark.parametrize(
    "key",
    [
        "sources.sensors.profile",  # another task's key
        "sources.erp",  # the parent of the owned keys
        "sources.erp.profiles",  # shares characters with an owned key, not a segment
        "sources.erp.mappings",
        "contract.purpose",
        "ontology.path",
        "twin.id",
        "field_status",
    ],
)
def test_no_key_outside_the_owned_keys_gets_through(held, key):
    held.write_result("profile-erp", {key: "x"}, files={"report.md": "# profile\n"})
    outcome = _refused(held, "tasks.patch.not_owned")
    assert outcome.reason("tasks.patch.not_owned")["keys"] == [key]


def test_refused_for_a_direct_edit_of_the_manifest(held):
    path = held.workspace.manifest_path
    path.write_text(path.read_text() + "# a note left by an agent\n")
    outcome = _refused(held, "tasks.foreign.modified")
    assert outcome.reason("tasks.foreign.modified")["paths"] == ["manifest.yaml"]
    # Putting the file back as it was clears the refusal.
    path.write_text(path.read_text().replace("# a note left by an agent\n", ""))
    assert held.submit("profile-erp", held.token).code == 0


def test_refused_for_a_new_file_elsewhere_in_the_workspace(held):
    stray = held.root / "profiles" / "erp.json"
    stray.parent.mkdir()
    stray.write_text("{}")
    outcome = _refused(held, "tasks.foreign.added")
    assert outcome.reason("tasks.foreign.added")["paths"] == ["profiles/erp.json"]
    stray.unlink()
    assert held.submit("profile-erp", held.token).code == 0


@pytest.mark.parametrize(
    ("victim_state", "where", "problem"),
    [
        ("open", "output/planted.txt", "not empty before the task was claimed"),
        ("open", "planted.txt", "not part of a task directory"),
        ("claimed", "planted.txt", "not part of a task directory"),
        ("claimed", "input/planted.txt", "changed since the task was claimed"),
        ("submitted", "output/planted.txt", "changed since it was submitted"),
        ("submitted", "input/planted.txt", "changed since the task was claimed"),
    ],
)
def test_refused_for_a_file_written_into_another_tasks_directory(
    held, victim_state, where, problem
):
    if victim_state == "claimed":
        held.claim("profile-sensors", holder="bob")
    elif victim_state == "submitted":
        held.finish("profile-sensors", held.profile_patch("sensors"))
    (held.tasks / "profile-sensors" / where).write_text("planted by the erp agent")
    outcome = _refused(held, "tasks.zone.violation")
    problems = outcome.reason("tasks.zone.violation")["problems"]
    assert len(problems) == 1
    assert problems[0]["path"].startswith("tasks/profile-sensors")
    assert problems[0]["problem"] == problem


def test_a_file_planted_in_a_claimed_output_is_refused_when_its_owner_submits(held):
    bob = held.claim("profile-sensors", holder="bob")
    held.write_result("profile-sensors", held.profile_patch("sensors"))
    (held.output("profile-sensors") / "planted.txt").write_text("planted by the erp agent")
    outcome = held.submit("profile-sensors", bob)
    assert outcome.reasons == ["tasks.artifact.undeclared"]
    assert outcome.reason("tasks.artifact.undeclared")["paths"] == ["planted.txt"]


def test_refused_for_a_changed_input_file(held):
    (held.tasks / "profile-erp" / "input" / "sample.csv").write_text("id\n999\n")
    outcome = _refused(held, "tasks.input.changed")
    problems = outcome.reason("tasks.input.changed")["problems"]
    assert problems == [
        {"path": "tasks/profile-erp/input", "problem": "changed since the task was claimed"}
    ]


@pytest.mark.parametrize("change", ["added", "removed", "emptied", "directory"])
def test_any_change_to_the_inputs_is_noticed(held, change):
    inputs = held.tasks / "profile-erp" / "input"
    if change == "added":
        (inputs / "extra.csv").write_text("")
    elif change == "removed":
        (inputs / "sample.csv").unlink()
    elif change == "emptied":
        (inputs / "sample.csv").write_text("")
    else:
        (inputs / "sub").mkdir()
    _refused(held, "tasks.input.changed")


def test_refused_for_a_symlink_in_the_output_pointing_outside(held):
    link = held.output("profile-erp") / "manifest.yaml"
    link.symlink_to(held.workspace.manifest_path)
    outcome = _refused(held, "tasks.output.symlink")
    assert outcome.reason("tasks.output.symlink")["links"] == [
        {"path": "manifest.yaml", "target": str(held.workspace.manifest_path)}
    ]
    assert held.workspace.manifest_path.is_file() and not held.workspace.manifest_path.is_symlink()


def test_refused_for_a_stale_pin_after_a_sanctioned_write(held):
    def move(manifest):
        manifest.sources["erp"].location = "postgresql://${ERP_HOST}/erp_v2"

    held.edit_manifest(move)
    outcome = _refused(held, "tasks.pin.manifest")
    assert outcome.reason("tasks.pin.manifest")["reads"] == ["sources.erp"]


# -- further refusals ----------------------------------------------------------


def test_a_sanctioned_write_elsewhere_in_the_manifest_does_not_disturb_the_task(held):
    def unrelated(manifest):
        manifest.sources["sensors"].location = "https://${SENSOR_HOST}/v2"
        manifest.contract.purpose = "Keep the line running and cheap"

    held.edit_manifest(unrelated)
    assert held.submit("profile-erp", held.token).code == 0


def test_refused_when_the_ontology_file_changes(held):
    with held.workspace.exclusive():
        held.workspace.atomic_write(held.root / "ontology" / "domain.yaml", "classes: {Pump: {}}\n")
    _refused(held, "tasks.pin.ontology")


def test_refused_for_a_shared_file_that_was_removed(held):
    (held.root / "ontology" / "domain.yaml").unlink()
    outcome = _refused(held, "tasks.foreign.removed", "tasks.pin.ontology")
    assert outcome.reason("tasks.foreign.removed")["paths"] == ["ontology/domain.yaml"]


def test_refused_when_the_task_description_was_edited(held):
    spec = held.tasks / "profile-erp" / "task.json"
    document = json.loads(spec.read_text())
    document["writes"]["owned_keys"].append("sources.erp.location")
    spec.write_text(json.dumps(document))
    held.write_result(
        "profile-erp", {"sources.erp.location": "x"}, files={"report.md": "# profile\n"}
    )
    # The edited file grants nothing: ownership comes from the toolkit's own record.
    _refused(held, "tasks.spec.changed", "tasks.patch.not_owned")


def test_refused_when_the_state_file_was_edited(held):
    path = held.workspace.tasks_state_path
    original = path.read_text()
    state = json.loads(original)
    state["batches"][0]["tasks"]["profile-erp"]["lease"]["expires_at"] = "2999-01-01T00:00:00Z"
    path.write_text(json.dumps(state))
    outcome = held.submit("profile-erp", held.token)
    assert outcome.reasons == ["tasks.foreign.modified"]
    assert outcome.reason("tasks.foreign.modified")["paths"] == [".dta/tasks.json"]

    # Granting oneself more keys in the state file does not get a patch through either.
    state = json.loads(original)
    state["batches"][0]["tasks"]["profile-erp"]["spec"]["owns"].append("sources.erp.location")
    path.write_text(json.dumps(state))
    held.write_result(
        "profile-erp", {"sources.erp.location": "x"}, files={"report.md": "# profile\n"}
    )
    outcome = held.submit("profile-erp", held.token)
    assert "tasks.foreign.modified" in outcome.reasons
    assert held.manifest().sources["erp"].location != "x"


def test_refused_when_the_ledger_is_gone(held):
    held.workspace.ledger_path.unlink()
    _refused(held, "tasks.ledger.missing")


def test_refused_without_a_result_or_with_a_malformed_one(held):
    result = held.output("profile-erp") / "result.json"
    result.unlink()
    (held.output("profile-erp") / "report.md").unlink()
    _refused(held, "tasks.result.missing")

    for text in (
        "{not json",
        "[]",
        '{"manifest_patch": {}}',  # the other keys are missing
        '{"manifest_patch": {}, "proposals": [], "artifacts": [], "notes": "", "extra": 1}',
        '{"manifest_patch": [], "proposals": [], "artifacts": [], "notes": ""}',
        '{"manifest_patch": {}, "proposals": [{"target": "x"}], "artifacts": [], "notes": ""}',
        '{"manifest_patch": {"a": 1, "a": 2}, "proposals": [], "artifacts": [], "notes": ""}',
        '{"manifest_patch": {"a": NaN}, "proposals": [], "artifacts": [], "notes": ""}',
    ):
        result.write_text(text)
        _refused(held, "tasks.result.invalid")


def test_refused_for_malformed_patch_keys(held):
    for key in ("", "sources..erp", "sources.erp.profile.", "sources.erp.*", "sources/erp"):
        held.write_result("profile-erp", {key: 1}, files={"report.md": "# profile\n"})
        outcome = _refused(held, "tasks.patch.key")
        assert outcome.reason("tasks.patch.key")["keys"] == [key]


def test_refused_when_the_patch_would_break_the_schema(held):
    held.write_result(
        "profile-erp",
        {"sources.erp.profile": {"datasets": "not a mapping"}},
        files={"report.md": "# profile\n"},
    )
    outcome = _refused(held, "tasks.patch.schema")
    paths = [problem["path"] for problem in outcome.reason("tasks.patch.schema")["problems"]]
    assert "sources.erp.profile.datasets" in paths


def test_files_in_the_output_must_match_what_the_result_declares(held):
    out = held.output("profile-erp")
    (out / "unlisted.bin").write_bytes(b"\x00")
    outcome = _refused(held, "tasks.artifact.undeclared")
    assert outcome.reason("tasks.artifact.undeclared")["paths"] == ["unlisted.bin"]
    (out / "unlisted.bin").unlink()

    held.write_result(
        "profile-erp",
        held.profile_patch("erp"),
        files={"report.md": "# profile\n"},
        artifacts=["report.md", "missing.md"],
    )
    outcome = _refused(held, "tasks.artifact.missing")
    assert outcome.reason("tasks.artifact.missing")["paths"] == ["missing.md"]

    for path in ("../../manifest.yaml", "/etc/passwd", "a//b", "./report.md"):
        held.write_result(
            "profile-erp",
            held.profile_patch("erp"),
            files={"report.md": "# profile\n"},
            artifacts=["report.md", path],
        )
        outcome = _refused(held, "tasks.artifact.path")
        assert outcome.reason("tasks.artifact.path")["paths"] == [path]


def test_refused_for_things_in_the_output_that_are_not_plain_files(held):
    out = held.output("profile-erp")
    os.mkfifo(out / "pipe")
    _refused(held, "tasks.output.special")
    (out / "pipe").unlink()

    # A second name for a shared file would let the output change it later.
    os.link(held.workspace.manifest_path, out / "copy.yaml")
    held.write_result(
        "profile-erp",
        held.profile_patch("erp"),
        files={"report.md": "# profile\n"},
        artifacts=["report.md", "copy.yaml"],
    )
    outcome = _refused(held, "tasks.output.hardlink")
    assert outcome.reason("tasks.output.hardlink")["paths"] == ["copy.yaml"]
    (out / "copy.yaml").unlink()
    held.write_result("profile-erp", held.profile_patch("erp"), files={"report.md": "# profile\n"})

    # A link that stays inside the output directory is refused all the same.
    (out / "alias.md").symlink_to("report.md")
    _refused(held, "tasks.output.symlink")


def test_refused_when_the_output_directory_itself_is_a_link(held, tmp_path):
    import shutil

    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(held.output("profile-erp")), str(elsewhere))
    held.output("profile-erp").symlink_to(elsewhere)
    _refused(held, "tasks.output.missing")


def test_refused_for_writing_beside_the_output_directory(held):
    (held.tasks / "profile-erp" / "scratch.txt").write_text("notes")
    outcome = _refused(held, "tasks.zone.violation")
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": "tasks/profile-erp/scratch.txt", "problem": "not part of a task directory"}
    ]


def test_refused_for_a_stray_entry_under_tasks(held):
    (held.tasks / "notes.txt").write_text("shared scratch")
    (held.tasks / "scratch").mkdir()
    outcome = _refused(held, "tasks.zone.violation")
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": "tasks/notes.txt", "problem": "appeared after the batch opened"},
        {"path": "tasks/scratch", "problem": "appeared after the batch opened"},
    ]


def test_every_failed_check_is_reported_at_once(held):
    held.write_result(
        "profile-erp",
        {"contract.purpose": "rewritten"},
        files={"report.md": "# profile\n"},
        artifacts=[],
    )
    (held.tasks / "profile-erp" / "input" / "sample.csv").write_text("changed")
    (held.output("profile-erp") / "link").symlink_to("/etc/hostname")
    (held.root / "stray.txt").write_text("x")
    held.workspace.manifest_path.write_text(held.workspace.manifest_path.read_text() + "\n")
    (held.tasks / "profile-sheets" / "output" / "planted").write_text("x")
    outcome = held.submit("profile-erp", "not-the-token")
    assert sorted(outcome.reasons) == [
        "tasks.artifact.undeclared",
        "tasks.foreign.added",
        "tasks.foreign.modified",
        "tasks.input.changed",
        "tasks.lease.token",
        "tasks.output.symlink",
        "tasks.patch.not_owned",
        "tasks.zone.violation",
    ]
    assert held.task("profile-erp")["status"] == "claimed"


def test_a_refused_task_can_be_fixed_and_submitted_again(held):
    held.write_result("profile-erp", {"sources.erp.location": "x"})
    assert sorted(held.submit("profile-erp", held.token).reasons) == [
        "tasks.artifact.undeclared",
        "tasks.patch.not_owned",
    ]
    held.write_result(
        "profile-erp",
        held.profile_patch("erp"),
        files={"report.md": "# profile\n"},
        proposals=[{"target": "sources.erp.location", "description": "the host has moved"}],
    )
    assert held.submit("profile-erp", held.token).result["proposals"] == 1


def test_a_submitted_task_can_be_submitted_again_until_it_is_merged(held):
    first = held.submit("profile-erp", held.token).result["output_sha256"]
    (held.output("profile-erp") / "report.md").write_text("# profile, revised\n")
    second = held.submit("profile-erp", held.token).result["output_sha256"]
    assert first != second and held.task("profile-erp")["output_sha256"] == second
    held.merge("profile-erp")
    held.submit("profile-erp", held.token).refused("tasks.submit.closed")


def test_submit_needs_a_claim(ws):
    ws.open("3")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    outcome = ws.submit("profile-erp", "anything")
    assert outcome.reasons == ["tasks.lease.none"]
    ws.submit("profile-nothing", "anything").could_not_run("tasks.task.unknown")


def test_submit_works_through_the_installed_entry_point(held):
    refused = held.spawn("submit", "profile-erp", "--token", "wrong")
    assert refused.reasons == ["tasks.lease.token"]
    assert "wrong" not in refused.stderr  # a token is never logged
    accepted = held.spawn("submit", "profile-erp", "--token", held.token)
    assert accepted.result["status"] == "submitted"
    assert held.token not in accepted.stderr
    assert held.token not in (held.root / ".dta" / "logs" / "toolkit.log").read_text()
