from pathlib import Path

import pytest
import yaml
from dta_core.manifest import Owner, Status, parse_manifest, required_fields
from dta_core.manifest.checks import GateInputs, current_stage, evaluate_gates, subject_of
from dta_toolkit.tasks.catalogue import CATALOGUE, Kind, tasks_for
from dta_toolkit.tasks.errors import TaskError


def _plan(harness, stage):
    return harness.run("plan", "--stage", stage).result


def _by_id(plan):
    return {task["id"]: task for task in plan["tasks"]}


def test_plan_lists_the_catalogue_tasks_in_one_batch_per_stage(make_ws, designed):
    ws = make_ws("designed", designed)
    expected = {
        "3": ["profile-erp", "profile-sensors", "profile-sheets"],
        "4": [
            "mapping-erp",
            "mapping-sensors",
            "mapping-sheets",
            "question-q1",
            "question-q2",
            "question-q3",
        ],
        "6": [
            "function-q2",
            "function-q3-policy",
            "model-q2-arima",
            "model-q2-ets",
            "model-q3-gbm",
            "stage-erp",
            "stage-sensors",
            "stage-sheets",
        ],
        "operate": ["remodel-q2", "remodel-q3"],
    }
    for stage, ids in expected.items():
        plan = _plan(ws, stage)
        assert [task["id"] for task in plan["tasks"]] == ids
        assert plan["batches"] == [ids]
        assert plan["next_batch"] == 0 and plan["open_batch"] is None
        assert all(task["status"] == "pending" for task in plan["tasks"])


def test_plan_states_what_each_kind_reads_and_owns(make_ws, designed):
    ws = make_ws("designed", designed)
    everything = {}
    for stage in ("3", "4", "6", "operate"):
        everything.update(_by_id(_plan(ws, stage)))

    def reads(task_id):
        return everything[task_id]["reads"]["manifest"]

    def owns(task_id):
        return everything[task_id]["owned_keys"]

    assert reads("profile-erp") == ["sources.erp"]
    assert owns("profile-erp") == [
        "sources.erp.profile",
        "sources.erp.refresh_period",
        "sources.erp.rate_limit_period",
    ]
    assert reads("mapping-erp") == ["ontology", "sources.erp"]
    assert owns("mapping-erp") == ["sources.erp.mappings"]
    assert reads("question-q2") == [
        "ontology",
        "contract.questions.q2",
        "contract.controls",
        "contract.objective",
    ]
    assert owns("question-q2") == [
        "contract.questions.q2.targets",
        "contract.questions.q2.horizon",
        "contract.questions.q2.function",
        "contract.questions.q2.model",
        "models.q2",
        "functions.q2",
        "functions.q2-*",
    ]
    assert reads("stage-erp") == ["ontology", "sources.erp"] and owns("stage-erp") == []
    assert reads("model-q2-ets") == ["ontology", "models.q2", "contract.questions.q2"]
    assert owns("model-q2-ets") == []
    assert reads("function-q3-policy") == ["ontology", "functions.q3-policy"]
    assert owns("function-q3-policy") == []
    assert reads("remodel-q3") == ["ontology", "contract.questions.q3", "models.q3"]
    assert owns("remodel-q3") == [
        "models.q3.candidates",
        "models.q3.features",
        "models.q3.backtest",
    ]
    for task_id, task in everything.items():
        assert task["write_zone"] == f"tasks/{task_id}/output"
        assert task["reads"]["input"] == f"tasks/{task_id}/input"
        assert task["reads"]["ontology_file"] == "ontology/domain.yaml"
        assert task["needs"] == []


def test_a_custom_connector_is_built_before_its_source_is_staged(make_ws, designed):
    designed["sources"]["sheets"]["connector"] = "custom"
    plan = _plan(make_ws("custom", designed), "6")
    assert _by_id(plan)["stage-sheets"]["needs"] == ["connector-sheets"]
    assert _by_id(plan)["stage-erp"]["needs"] == []
    assert plan["batches"] == [
        [
            "connector-sheets",
            "function-q2",
            "function-q3-policy",
            "model-q2-arima",
            "model-q2-ets",
            "model-q3-gbm",
            "stage-erp",
            "stage-sensors",
        ],
        ["stage-sheets"],
    ]


def test_tasks_that_would_write_the_same_keys_are_split(make_ws, sample):
    # question-q1 owns every function named q1-*, which is where question-q1-kpi keeps its own.
    sample["contract"]["questions"]["q1-kpi"] = {"text": "How is the KPI?"}
    plan = _plan(make_ws("split", sample), "4")
    assert plan["batches"] == [
        [
            "mapping-erp",
            "mapping-sensors",
            "mapping-sheets",
            "question-q1",
            "question-q2",
            "question-q3",
        ],
        ["question-q1-kpi"],
    ]


def test_plan_changes_nothing(ws):
    before = ws.workspace.fingerprint()
    for stage in ("3", "4", "6", "operate"):
        _plan(ws, stage)
    assert ws.workspace.fingerprint() == before
    assert not ws.workspace.tasks_state_path.exists()
    assert not ws.workspace.ledger_path.exists()
    assert not ws.tasks.exists()


def test_plan_needs_a_stage_that_has_delegable_work(ws):
    # The build stands before profiling, where there is nothing to hand out.
    detail = ws.run("plan").could_not_run("tasks.stage.required")
    assert detail["stage"] not in detail["stages"]
    detail = ws.run("plan", "--stage", "5").could_not_run("tasks.stage.unknown")
    assert detail["stages"] == ["3", "4", "6", "operate"]
    ws.run("plan", "--stage", "3", "--store-version", "v1").could_not_run("tasks.usage")


def test_plan_without_a_stage_takes_the_one_the_build_stands_at(make_ws):
    complete = Path(__file__).parent.parent / "commands" / "complete_manifest.yaml"
    data = yaml.safe_load(complete.read_text(encoding="utf-8"))
    # As the user leaves it after the discussion: every value they own confirmed.
    data["field_status"] = {
        state.path: Status.confirmed.value
        for state in required_fields(parse_manifest(data))
        if state.owner == Owner.user and state.status == Status.proposed
    }
    ws = make_ws("complete", data)
    workspace = ws.workspace

    def stage() -> int | None:
        inputs = GateInputs(workspace.load_manifest(), recorded=workspace.load_state().gates)
        return current_stage(evaluate_gates(inputs))

    # Access is the one gate before design that only a command can pass.
    assert stage() == 2
    workspace.record_gate(2, True, subject_hash=subject_of(2, workspace.load_manifest()))
    assert stage() == 4
    before = workspace.fingerprint()
    assert ws.run("plan").result == _plan(ws, "4")
    assert workspace.fingerprint() == before


@pytest.mark.parametrize("bad", ["../../escape", "a/b", "a.b", "Upper", "x y", "tail\n", "-dash"])
def test_a_manifest_id_that_could_leave_the_task_directory_is_refused(make_ws, sample, bad):
    sample["sources"][bad] = {"connector": "file", "location": "x.csv"}
    ws = make_ws("unsafe", sample)
    detail = ws.run("plan", "--stage", "3").could_not_run("tasks.subject.invalid")
    assert detail["value"] == bad
    ws.run("plan", "--stage", "3", "--open").could_not_run("tasks.subject.invalid")
    assert not ws.tasks.exists() and not ws.workspace.ledger_path.exists()


def test_two_subjects_that_spell_the_same_task_id_are_refused(make_ws, designed):
    designed["models"] = {
        "a": {"candidates": {"b-c": {"family": "x"}}},
        "a-b": {"candidates": {"c": {"family": "y"}}},
    }
    detail = (
        make_ws("clash", designed).run("plan", "--stage", "6").could_not_run("tasks.plan.duplicate")
    )
    assert detail["id"] == "model-a-b-c"


def test_open_prepares_and_pins_each_task_of_the_next_batch(ws):
    opened = ws.open("3", "--store-version", "commit-41")
    assert opened["opened"]["tasks"] == ["profile-erp", "profile-sensors", "profile-sheets"]
    assert opened["opened"]["store_version"] == "commit-41"
    assert opened["open_batch"] == opened["opened"]["batch"]
    assert {task["status"] for task in opened["tasks"]} == {"open"}
    for task_id in opened["opened"]["tasks"]:
        assert sorted(p.name for p in (ws.tasks / task_id).iterdir()) == ["input", "output"]
        record = ws.task(task_id)
        assert record["status"] == "open" and record["lease"] is None
        assert record["pins"]["store_version"] == "commit-41"
        assert len(record["pins"]["manifest_reads"]) == 64
        assert record["pins"]["ontology"]["path"] == "ontology/domain.yaml"
        assert len(record["pins"]["ontology"]["sha256"]) == 64
    assert ws.workspace.ledger_path.is_file()
    assert ws.clean()


def test_open_is_refused_while_a_batch_is_open_and_when_nothing_is_left(ws):
    ws.open("3")
    state = ws.state_bytes()
    detail = ws.run("plan", "--stage", "3", "--open").refused("tasks.batch.open")
    assert detail["unfinished"] == ["profile-erp", "profile-sensors", "profile-sheets"]
    ws.run("plan", "--stage", "4", "--open").refused("tasks.batch.open")
    assert ws.state_bytes() == state

    for source in ("erp", "sensors", "sheets"):
        ws.finish(f"profile-{source}", ws.profile_patch(source))
    assert ws.merge()["batch_closed"] is True
    plan = _plan(ws, "3")
    assert {task["status"] for task in plan["tasks"]} == {"merged"}
    assert plan["next_batch"] is None
    ws.run("plan", "--stage", "3", "--open").refused("tasks.batch.nothing")
    assert not ws.workspace.ledger_path.exists()


def test_open_takes_the_batches_in_order_and_skips_what_is_merged(make_ws, sample):
    sample["contract"]["questions"]["q1-kpi"] = {"text": "How is the KPI?"}
    ws = make_ws("ordered", sample)
    first = ws.open("4")["opened"]["tasks"]
    assert "question-q1" in first and "question-q1-kpi" not in first
    ws.finish("question-q1", {"functions.q1": {"kind": "builtin", "entry": "latest"}})
    ws.merge()
    ws.abort()

    # The first batch still has unmerged tasks, so it is opened again without the merged one.
    second = ws.open("4")["opened"]["tasks"]
    assert second == [
        "mapping-erp",
        "mapping-sensors",
        "mapping-sheets",
        "question-q2",
        "question-q3",
    ]
    for task_id in second:
        ws.finish(task_id)
    ws.merge()
    assert ws.open("4")["opened"]["tasks"] == ["question-q1-kpi"]


def test_a_task_waits_until_what_it_needs_is_merged(make_ws, designed):
    designed["sources"]["sheets"]["connector"] = "custom"
    ws = make_ws("needs", designed)
    first = ws.open("6")["opened"]["tasks"]
    assert "connector-sheets" in first and "stage-sheets" not in first
    for task_id in first:
        if task_id != "connector-sheets":
            ws.finish(task_id)
    ws.merge()
    ws.abort("connector-sheets")
    # The connector was given up, so the task that needs it stays closed.
    assert ws.open("6")["opened"]["tasks"] == ["connector-sheets"]
    ws.finish("connector-sheets", files={"connector.py": "def read(): ...\n"})
    ws.merge()
    assert ws.open("6")["opened"]["tasks"] == ["stage-sheets"]


def test_a_new_kind_is_a_new_row(sample):
    from dta_core.manifest import parse_manifest

    extended = (
        *CATALOGUE,
        Kind(
            name="audit",
            stage="7",
            per="source",
            id="audit-{s}",
            reads=("sources.{s}",),
            needs=("never-there-{s}",),
        ),
    )
    manifest = parse_manifest(sample)
    tasks = tasks_for(manifest, "7", extended)
    assert [task.id for task in tasks] == ["audit-erp", "audit-sensors", "audit-sheets"]
    assert all(task.needs == () for task in tasks)
    with pytest.raises(TaskError):
        tasks_for(manifest, "7")
