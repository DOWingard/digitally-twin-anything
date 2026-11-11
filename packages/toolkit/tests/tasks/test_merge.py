import pytest
import yaml
from dta_core.manifest import parse_manifest
from dta_toolkit.tasks.catalogue import TaskSpec
from dta_toolkit.tasks.result import apply_patch, side_effect_reasons

SOURCES = ("erp", "sensors", "sheets")
ROUNDS = 10


def _submit_all(ws):
    """Open the profiling batch and submit a valid result for every source."""
    ws.open("3")
    for source in SOURCES:
        ws.finish(
            f"profile-{source}",
            ws.profile_patch(source, rows=len(source)),
            files={"report.md": f"# {source}\n"},
            proposals=[{"target": "ontology", "description": f"add a type for {source}"}],
        )


def _profiled(ws):
    return {
        key: source.profile.datasets["main"].rows
        for key, source in ws.manifest().sources.items()
        if source.profile is not None
    }


def _refusals(outcome):
    """Per task, the codes a refused merge reported."""
    detail = outcome.refused("tasks.merge.refused")
    return {
        entry["task"]: [reason["code"] for reason in entry["reasons"]]
        for entry in detail["refused"]
    }


# -- merging -------------------------------------------------------------------


def test_merge_applies_every_submitted_patch_and_closes_the_batch(ws):
    _submit_all(ws)
    merged = ws.merge()
    assert merged["merged"] == ["profile-erp", "profile-sensors", "profile-sheets"]
    assert merged["batch_closed"] is True
    assert _profiled(ws) == {"erp": 3, "sensors": 7, "sheets": 6}
    assert str(ws.manifest().sources["erp"].refresh_period) == "1:00:00"
    assert not ws.workspace.ledger_path.exists()
    status = ws.status()
    assert status["open_batch"] is None
    assert status["merged"] == ["profile-erp", "profile-sensors", "profile-sheets"]


def test_merge_of_one_task_leaves_the_others_and_the_batch_open(ws):
    _submit_all(ws)
    merged = ws.merge("profile-sensors")
    assert merged["merged"] == ["profile-sensors"] and merged["batch_closed"] is False
    assert _profiled(ws) == {"sensors": 7}
    assert ws.task("profile-erp")["status"] == "submitted"
    assert ws.workspace.ledger_path.is_file() and ws.clean()


def test_proposals_are_kept_for_the_orchestrator_and_never_applied(ws):
    ontology = (ws.root / "ontology" / "domain.yaml").read_bytes()
    _submit_all(ws)
    ws.merge()
    assert ws.status()["proposals"] == [
        {
            "task": f"profile-{source}",
            "batch": "b1",
            "target": "ontology",
            "description": f"add a type for {source}",
        }
        for source in SOURCES
    ]
    assert (ws.root / "ontology" / "domain.yaml").read_bytes() == ontology


def test_only_a_submitted_task_can_be_merged(ws):
    ws.open("3")
    ws.run("merge", "profile-erp").refused("tasks.merge.not_submitted")
    ws.claim("profile-erp")
    ws.run("merge", "profile-erp").refused("tasks.merge.not_submitted")
    ws.run("merge", "profile-unknown").could_not_run("tasks.task.unknown")
    ws.run("merge").could_not_run("tasks.usage")
    ws.run("merge", "profile-erp", "--all").could_not_run("tasks.usage")
    assert ws.merge() == {
        "batch": "b1",
        "merged": [],
        "already_merged": [],
        "refused": [],
        "batch_closed": False,
    }


# -- two merges at once --------------------------------------------------------


@pytest.mark.parametrize("mode", ["one task each", "both merge everything"])
def test_two_processes_merging_at_once_lose_nothing(make_ws, mode):
    for round_number in range(ROUNDS):
        ws = make_ws(f"merge-{round_number}")
        _submit_all(ws)
        if mode == "one task each":
            commands = [("merge", "profile-erp"), ("merge", "profile-sensors")]
            expected = {"erp": 3, "sensors": 7}
        else:
            commands = [("merge", "--all"), ("merge", "--all")]
            expected = {"erp": 3, "sensors": 7, "sheets": 6}
        outcomes = ws.race(*commands)
        assert [outcome.code for outcome in outcomes] == [0, 0], [o.payload for o in outcomes]

        # Both patches are in the manifest, and the manifest is whole.
        manifest = parse_manifest(yaml.safe_load(ws.manifest_bytes()))
        assert {k for k, s in manifest.sources.items() if s.profile} == set(expected)
        assert _profiled(ws) == expected

        # Each task was applied by exactly one of the two processes.
        applied = sorted(task for outcome in outcomes for task in outcome.result["merged"])
        assert applied == [f"profile-{source}" for source in expected]
        for source in expected:
            assert ws.task(f"profile-{source}")["status"] == "merged"
        if mode == "one task each":
            assert ws.task("profile-sheets")["status"] == "submitted"
            assert ws.clean()
            assert ws.merge()["batch_closed"] is True
        assert ws.status()["open_batch"] is None
        assert not ws.workspace.ledger_path.exists()


def test_agents_working_at_once_and_a_merger_end_with_every_result_in(make_ws, sample):
    count = 6
    sample["sources"] = {
        f"s{n}": {"connector": "file", "location": f"{n}.csv"} for n in range(count)
    }
    for round_number in range(3):
        ws = make_ws(f"busy-{round_number}", sample)
        ws.open("3")
        tokens = {}
        for outcome, n in zip(
            ws.race(*[("claim", f"profile-s{n}", "--holder", f"agent-{n}") for n in range(count)]),
            range(count),
            strict=True,
        ):
            tokens[n] = outcome.result["token"]
            ws.write_result(f"profile-s{n}", ws.profile_patch(f"s{n}", rows=n + 1))
        # Everybody submits while two mergers are already running.
        outcomes = ws.race(
            *[("submit", f"profile-s{n}", "--token", tokens[n]) for n in range(count)],
            ("merge", "--all"),
            ("merge", "--all"),
        )
        assert [outcome.code for outcome in outcomes] == [0] * (count + 2)
        ws.merge()  # whatever was submitted after the mergers had passed
        assert _profiled(ws) == {f"s{n}": n + 1 for n in range(count)}
        assert ws.status()["open_batch"] is None
        assert sorted(ws.status()["merged"]) == [f"profile-s{n}" for n in range(count)]


# -- output changed after submit -----------------------------------------------


@pytest.mark.parametrize("change", ["artifact", "result", "added", "removed"])
def test_output_changed_after_submit_is_refused_at_merge(ws, change):
    _submit_all(ws)
    out = ws.output("profile-erp")
    if change == "artifact":
        (out / "report.md").write_text("# erp, rewritten after submit\n")
    elif change == "result":
        ws.write_result(
            "profile-erp",
            {"sources.erp.refresh_period": "PT5S"},
            files={"report.md": "# erp\n"},
        )
    elif change == "added":
        (out / "late.bin").write_bytes(b"late")
    else:
        (out / "report.md").unlink()
    manifest = ws.manifest_bytes()

    outcome = ws.run("merge", "profile-erp")
    assert _refusals(outcome) == {"profile-erp": ["tasks.output.changed"]}
    assert ws.manifest_bytes() == manifest
    assert ws.task("profile-erp")["status"] == "submitted"

    # The others are unaffected, merged one by one or all together: a sealed
    # result that was disturbed is that task's problem alone.
    assert ws.merge("profile-sensors")["merged"] == ["profile-sensors"]
    outcome = ws.run("merge", "--all")
    assert _refusals(outcome) == {"profile-erp": ["tasks.output.changed"]}
    assert outcome.error["detail"]["merged"] == ["profile-sheets"]
    assert _profiled(ws) == {"sensors": 7, "sheets": 6}


def test_a_refused_merge_is_cured_by_submitting_again_or_by_giving_the_task_up(ws):
    ws.open("3")
    token = ws.finish("profile-erp", ws.profile_patch("erp"), files={"report.md": "v1\n"})
    (ws.output("profile-erp") / "report.md").write_text("v2\n")
    assert _refusals(ws.run("merge", "--all")) == {"profile-erp": ["tasks.output.changed"]}
    assert ws.submit("profile-erp", token).code == 0
    assert ws.merge()["merged"] == ["profile-erp"]

    ws.finish("profile-sensors", ws.profile_patch("sensors"))
    (ws.output("profile-sensors") / "late.txt").write_text("x")
    assert _refusals(ws.run("merge", "--all")) == {"profile-sensors": ["tasks.output.changed"]}
    assert ws.abort("profile-sensors")["abandoned"] == ["profile-sensors"]
    assert ws.merge()["merged"] == []
    assert ws.manifest().sources["sensors"].profile is None


# -- what a merge checks again -------------------------------------------------


def test_merge_is_refused_after_a_foreign_change(ws):
    _submit_all(ws)
    manifest = ws.workspace.manifest_path
    manifest.write_text(manifest.read_text() + "# edited behind the toolkit's back\n")
    before = ws.manifest_bytes()
    refusals = _refusals(ws.run("merge", "--all"))
    assert refusals == {f"profile-{source}": ["tasks.foreign.modified"] for source in SOURCES}
    assert ws.manifest_bytes() == before
    assert all(ws.task(f"profile-{s}")["status"] == "submitted" for s in SOURCES)


def test_merge_is_refused_for_a_pin_that_went_stale_after_submit(ws):
    _submit_all(ws)

    def move(manifest):
        manifest.sources["erp"].location = "postgresql://${ERP_HOST}/erp_v2"

    ws.edit_manifest(move)
    outcome = ws.run("merge", "--all")
    assert _refusals(outcome) == {"profile-erp": ["tasks.pin.manifest"]}
    assert outcome.error["detail"]["merged"] == ["profile-sensors", "profile-sheets"]
    assert ws.manifest().sources["erp"].profile is None


def test_merge_is_refused_while_something_under_tasks_is_out_of_place(ws):
    _submit_all(ws)
    (ws.tasks / "leftover.txt").write_text("x")
    refusals = _refusals(ws.run("merge", "--all"))
    assert refusals == {f"profile-{source}": ["tasks.zone.violation"] for source in SOURCES}
    (ws.tasks / "leftover.txt").unlink()
    assert len(ws.merge()["merged"]) == 3


def test_merge_is_refused_when_inputs_changed_after_submit(ws):
    _submit_all(ws)
    (ws.tasks / "profile-erp" / "input" / "late.csv").write_text("x")
    outcome = ws.run("merge", "profile-erp")
    assert _refusals(outcome) == {"profile-erp": ["tasks.input.changed"]}


def test_results_of_earlier_batches_may_not_change_while_a_batch_is_open(ws):
    _submit_all(ws)
    ws.merge()
    ws.open("4")
    token = ws.claim("mapping-erp")
    ws.write_result("mapping-erp")
    # An agent of this batch overwrites what an earlier batch produced.
    (ws.tasks / "profile-erp" / "output" / "report.md").write_text("clobbered\n")
    outcome = ws.submit("mapping-erp", token)
    assert outcome.reasons == ["tasks.zone.violation"]
    assert outcome.reason("tasks.zone.violation")["problems"] == [
        {"path": "tasks/profile-erp", "problem": "changed since the batch opened"}
    ]
    (ws.tasks / "profile-erp" / "output" / "report.md").write_text("# erp\n")
    assert ws.submit("mapping-erp", token).code == 0


def test_a_value_written_into_a_tasks_keys_by_someone_else_is_never_overwritten(ws):
    ws.open("3")
    token = ws.claim("profile-erp")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    ws.finish("profile-sensors", ws.profile_patch("sensors"))

    def measured_elsewhere(manifest):
        manifest.sources["erp"].refresh_period = "PT15M"
        manifest.sources["sensors"].refresh_period = "PT15M"

    # A toolkit command sets keys that open tasks own: a sanctioned write, so the
    # ledger is silent, and the tasks do not read their own keys.
    ws.edit_manifest(measured_elsewhere)

    assert ws.submit("profile-erp", token).reasons == ["tasks.pin.owned"]
    ws.claim("profile-sheets", holder="carol")  # its keys are untouched: no objection
    outcome = ws.run("merge", "--all")
    assert _refusals(outcome) == {"profile-sensors": ["tasks.pin.owned"]}
    assert str(ws.manifest().sources["sensors"].refresh_period) == "0:15:00"
    assert ws.manifest().sources["sensors"].profile is None

    # Once the keys already hold what the task would write, merging changes nothing and is fine.
    def as_the_task_found(manifest):
        patched = ws.profile_patch("sensors")
        manifest.sources["sensors"].refresh_period = patched["sources.sensors.refresh_period"]
        manifest.sources["sensors"].profile = patched["sources.sensors.profile"]

    ws.edit_manifest(as_the_task_found)
    before = ws.manifest_bytes()
    assert ws.merge("profile-sensors")["merged"] == ["profile-sensors"]
    assert ws.manifest_bytes() == before


def test_a_claim_is_refused_when_the_tasks_keys_were_already_written(ws):
    ws.open("3")

    def measured_elsewhere(manifest):
        manifest.sources["erp"].rate_limit_period = "PT1S"

    ws.edit_manifest(measured_elsewhere)
    ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.pin.owned")
    assert ws.run("claim", "profile-sensors", "--holder", "alice").code == 0


# -- idempotence and recovery --------------------------------------------------


def test_merging_twice_is_a_no_op(ws):
    _submit_all(ws)
    ws.merge()
    manifest, state = ws.manifest_bytes(), ws.state_bytes()
    again = ws.merge()
    assert again == {
        "batch": None,
        "merged": [],
        "already_merged": [],
        "refused": [],
        "batch_closed": False,
    }
    named = ws.merge("profile-erp")
    assert named["merged"] == [] and named["already_merged"] == ["profile-erp"]
    assert ws.manifest_bytes() == manifest and ws.state_bytes() == state


def test_applying_a_patch_twice_gives_the_same_manifest(sample):
    manifest = parse_manifest(sample)
    patch = {
        "sources.erp.profile": {
            "profiled_at": "2026-01-01T00:00:00Z",
            "datasets": {"main": {"rows": 1, "columns": {}}},
        },
        "sources.erp.refresh_period": 3600,  # normalised to a duration when written
        "sources.erp.rate_limit_period": None,
    }
    once = apply_patch(manifest, patch)
    twice = apply_patch(once, patch)
    assert once == twice and once != manifest
    assert apply_patch(manifest, {}) == manifest


def test_a_patch_may_not_change_anything_outside_the_owned_keys_even_indirectly(sample):
    manifest = parse_manifest(sample)
    # Creating the model entry to set one field also fills in the entry's other fields.
    spec = TaskSpec(id="t", kind="synthetic", stage="x", owns=("models.new.candidates",))
    patched = apply_patch(manifest, {"models.new.candidates": {"c": {"family": "arima"}}})
    reasons = side_effect_reasons(spec, manifest, patched)
    assert [reason["code"] for reason in reasons] == ["tasks.patch.side_effect"]
    assert reasons[0]["detail"]["paths"] == ["models.new.interventional"]
    whole = TaskSpec(id="t", kind="synthetic", stage="x", owns=("models.new",))
    assert side_effect_reasons(whole, manifest, patched) == []


def test_a_merge_killed_between_the_manifest_and_the_state_recovers_on_rerun(ws):
    _submit_all(ws)
    ws.crash("merge.manifest_written", "merge", "--all")

    # The process died holding the lock, with the first patch written and nothing recorded.
    assert _profiled(ws) == {"erp": 3}
    assert all(ws.task(f"profile-{s}")["status"] == "submitted" for s in SOURCES)
    assert ws.status()["open_batch"]["id"] == "b1"
    after_crash = ws.manifest_bytes()
    assert ws.clean()  # the interrupted write was a sanctioned one

    rerun = ws.spawn("merge", "--all").result
    assert rerun["merged"] == ["profile-erp", "profile-sensors", "profile-sheets"]
    assert rerun["batch_closed"] is True
    assert _profiled(ws) == {"erp": 3, "sensors": 7, "sheets": 6}
    # Applying the first patch a second time changed nothing.
    recovered = parse_manifest(yaml.safe_load(after_crash))
    assert ws.manifest().sources["erp"] == recovered.sources["erp"]
    assert ws.task("profile-erp")["status"] == "merged"
    assert len(ws.status()["proposals"]) == 3
    assert not ws.workspace.ledger_path.exists()


def test_a_merge_killed_in_the_middle_of_a_batch_recovers_on_rerun(ws):
    _submit_all(ws)
    ws.merge("profile-erp")
    ws.crash("merge.manifest_written", "merge", "profile-sensors")
    assert _profiled(ws) == {"erp": 3, "sensors": 7}
    assert ws.task("profile-sensors")["status"] == "submitted"
    manifest = ws.manifest_bytes()

    rerun = ws.merge("profile-sensors")
    assert rerun["merged"] == ["profile-sensors"]
    assert ws.manifest_bytes() == manifest  # the same bytes were written again
    assert ws.merge()["merged"] == ["profile-sheets"]


def test_a_merge_killed_after_closing_the_batch_leaves_nothing_to_repair(ws):
    _submit_all(ws)
    ws.merge("profile-erp")
    ws.merge("profile-sensors")
    ws.crash("close.state_written", "merge", "profile-sheets")
    # The batch is closed on disk; only the ledger was not dropped yet.
    assert ws.status()["open_batch"] is None and ws.workspace.ledger_path.is_file()
    assert _profiled(ws) == {"erp": 3, "sensors": 7, "sheets": 6}

    assert ws.merge()["merged"] == []
    assert not ws.workspace.ledger_path.exists()


def test_a_leftover_ledger_does_not_block_the_next_batch(ws):
    _submit_all(ws)
    ws.crash("close.state_written", "merge", "--all")
    assert ws.workspace.ledger_path.is_file()
    # Between batches the orchestrator may edit by hand; the old ledger must not object.
    manifest = ws.workspace.manifest_path
    manifest.write_text(manifest.read_text() + "# edited between batches\n")
    assert ws.open("4")["opened"]["batch"] == "b2"
    assert ws.clean()


def test_an_abort_killed_after_closing_the_batch_is_completed_by_running_it_again(ws):
    ws.open("3")
    ws.crash("close.state_written", "abort", "--batch")
    assert ws.status()["open_batch"] is None and ws.workspace.ledger_path.is_file()
    again = ws.abort()
    assert again["batch"] is None and again["abandoned"] == []
    assert not ws.workspace.ledger_path.exists()


def test_a_claim_killed_before_the_lease_was_recorded_can_be_repeated(ws):
    ws.open("3")
    ws.crash("claim.spec_written", "claim", "profile-erp", "--holder", "alice")
    assert (ws.tasks / "profile-erp" / "task.json").is_file()
    assert ws.task("profile-erp")["status"] == "open"
    assert ws.clean()  # the file left behind is exactly what a claim writes
    token = ws.claim("profile-erp", holder="bob")
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    assert ws.submit("profile-erp", token).code == 0


@pytest.mark.parametrize("gap", ["open.ledger_rebased", "open.prepared"])
def test_an_open_killed_half_way_opens_nothing_and_can_be_repeated(ws, gap):
    ws.crash(gap, "plan", "--stage", "3", "--open")
    assert ws.status()["open_batch"] is None
    ws.run("claim", "profile-erp", "--holder", "alice").could_not_run("tasks.task.unknown")
    opened = ws.open("3")["opened"]
    assert opened["batch"] == "b1"
    assert ws.clean()
    for source in SOURCES:
        assert sorted(p.name for p in (ws.tasks / f"profile-{source}").iterdir()) == [
            "input",
            "output",
        ]
