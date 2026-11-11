"""`dta manifest validate`: the complete sample passes, and each check fires on its own.

Every failing case is the complete sample with one small change that breaks
exactly one rule, so the expected output is exactly one finding.
"""

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
from dta_core.manifest.checks import CHECKS

Mutation = Callable[[dict[str, Any]], None]


def _parent(data: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    *parents, last = path.split(".")
    node = data
    for part in parents:
        node = node[part]
    return node, last


def put(path: str, value: Any) -> Mutation:
    def mutate(data: dict[str, Any]) -> None:
        node, key = _parent(data, path)
        node[key] = value

    return mutate


def drop(path: str) -> Mutation:
    def mutate(data: dict[str, Any]) -> None:
        node, key = _parent(data, path)
        del node[key]

    return mutate


def chain(*mutations: Mutation) -> Mutation:
    def mutate(data: dict[str, Any]) -> None:
        for mutation in mutations:
            mutation(data)

    return mutate


def orphan_model(data: dict[str, Any]) -> None:
    model = copy.deepcopy(data["models"]["m_forecast"])
    data["models"]["m_orphan"] = {**model, "question": "q_missing"}


def stale_status(data: dict[str, Any]) -> None:
    data["field_status"]["contract.questions.q_gone.text"] = "confirmed"


Q = "contract.questions"
STATIC = put("contract.tier", "static")

# (case id, code, severity, path of the finding, mutation of the complete sample)
CASES = [
    ("question_function", "ref.question_function", "error", f"{Q}.q_level.function",
     put(f"{Q}.q_level.function", "f_missing")),
    ("question_model_missing", "ref.question_model", "error", f"{Q}.q_forecast.model",
     put(f"{Q}.q_forecast.model", "m_missing")),
    ("question_model_other", "ref.question_model", "error", f"{Q}.q_forecast.model",
     put(f"{Q}.q_forecast.model", "m_schedule")),
    ("model_question", "ref.model_question", "error", "models.m_orphan.question",
     orphan_model),
    ("control_action_missing", "ref.control_action", "error",
     "contract.controls.c_pump_speed.action",
     put("contract.controls.c_pump_speed.action", "a_missing")),
    ("control_action_other", "ref.control_action", "error",
     "contract.controls.c_pump_speed.action",
     put("actions.set_pump_speed.control", "c_other")),
    ("loop_policy", "ref.loop", "error", "control_loops.loop_pumps.policy",
     put("control_loops.loop_pumps.policy", "f_missing")),
    ("loop_controls", "ref.loop", "error", "control_loops.loop_pumps.controls",
     put("control_loops.loop_pumps.controls", ["c_pump_speed", "c_missing"])),
    ("objective_kpi", "ref.objective_kpi", "error", "contract.objective.kpi",
     put("contract.objective.kpi", "f_missing")),
    ("env_declared_webhook", "ref.env_declared", "error", "contract.notification.webhook_env",
     drop("env.ALERT_WEBHOOK_URL")),
    ("env_declared_body", "ref.env_declared", "error", "actions.set_pump_speed.executor.body.site",
     put("actions.set_pump_speed.executor.body", {"speed": "{{ speed }}", "site": "${SITE_ID}"})),
    ("env_unused", "ref.env_unused", "warning", "env.UNUSED_VAR",
     put("env.UNUSED_VAR", {"description": "Read by nothing"})),
    ("property_format_target", "ref.property_format", "error", f"{Q}.q_level.targets",
     put(f"{Q}.q_level.targets", ["tank.level"])),
    ("property_format_boundary", "ref.property_format", "error", "contract.boundary.drivers",
     put("contract.boundary.drivers", ["Tank_inflow"])),
    ("mapping_identity", "ref.mapping_columns", "error", "sources.scada.mappings.tanks.identity",
     put("sources.scada.mappings.tanks.identity", ["tank_no"])),
    ("mapping_dataset", "ref.mapping_columns", "error", "sources.pumps.mappings.pumps.dataset",
     put("sources.pumps.mappings.pumps.dataset", "pump_log")),
    ("bounds_inverted", "control.bounds", "error", "contract.controls.c_pump_speed.bounds",
     put("contract.controls.c_pump_speed.bounds", {"minimum": 1500, "maximum": 0})),
    ("bounds_empty", "control.bounds", "error", "contract.controls.c_pump_speed.bounds",
     put("contract.controls.c_pump_speed.bounds", {"max_step": 100})),
    ("binding", "control.binding", "error", "actions.set_pump_speed.binding",
     put("actions.set_pump_speed.binding", "advisory")),
    ("executor_addon", "action.executor_addon", "error", "actions.set_pump_speed.binding",
     put("contract.addons.executors", False)),
    ("static_control", "tier.static_control", "error", "contract.controls.c_pump_speed.autonomy",
     chain(STATIC, drop("sync"), drop("fidelity"), drop("control_loops"))),
    ("static_extras", "tier.static_extras", "warning", "sync",
     chain(STATIC, put("contract.controls.c_pump_speed.autonomy", "advisory"),
           drop("fidelity"), drop("control_loops"))),
    ("secret_query", "secret.inline", "error", "sources.pumps.location",
     put("sources.pumps.location", "https://pumps.example.org/api/v1/status?api_key=k3yL1teral")),
    ("secret_userinfo", "secret.inline", "error", "sources.scada.location",
     put("sources.scada.location", "postgresql://${SCADA_USER}:k3yL1teral@${SCADA_HOST}/plant")),
    ("secret_header", "secret.inline", "error",
     "actions.set_pump_speed.executor.headers.Authorization",
     put("actions.set_pump_speed.executor.headers.Authorization", "Bearer k3yL1teral")),
    ("function_interval", "function.interval", "error", "functions.f_schedule.interval",
     drop("functions.f_schedule.interval")),
    ("status_stale", "status.stale", "warning", "field_status.contract.questions.q_gone.text",
     stale_status),
    ("unmapped_target", "coverage.unmapped_target", "warning", f"{Q}.q_level.targets",
     put(f"{Q}.q_level.targets", ["Tank.level", "Tank.temperature"])),
    # With a sync interval set, refresh <= interval <= staleness already implies this
    # rule, so it is observable on its own only before the interval is derived.
    ("staleness", "feasible.staleness", "error", f"{Q}.q_level.staleness",
     chain(put(f"{Q}.q_level.staleness", "PT30S"), drop("sync.interval"))),
    ("interval_rate_limit", "feasible.interval", "error", "sync.interval",
     put("sources.scada.rate_limit_period", "PT6M")),
    ("interval_staleness", "feasible.interval", "error", "sync.interval",
     chain(put("sync.interval", "PT12M"), drop("sync.maximum_interval"))),
    ("interval_minimum", "feasible.interval", "error", "sync.interval",
     put("sync.minimum_interval", "PT6M")),
    ("interval_maximum", "feasible.interval", "error", "sync.interval",
     put("sync.maximum_interval", "PT4M")),
    ("noise", "feasible.noise", "error", f"{Q}.q_level.tolerance.value",
     put(f"{Q}.q_level.tolerance.value", 0.005)),
    ("history", "feasible.history", "error", "models.m_forecast.backtest",
     put("models.m_forecast.backtest.initial_window", "P400D")),
    ("history_provisional", "feasible.history", "warning", "models.m_forecast.backtest",
     chain(put("models.m_forecast.backtest.initial_window", "P400D"),
           put(f"{Q}.q_forecast.provisional", True))),
    ("acceptance", "feasible.acceptance", "warning", "models.m_forecast.acceptance.threshold",
     put("models.m_forecast.acceptance.threshold", 0.3)),
]  # fmt: skip


def _findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document["ok"]:
        return document["result"]["findings"]
    return document["error"]["detail"]["findings"]


def test_complete_sample_passes_validate_complete(workspace, complete):
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate", "--complete")
    assert code == 0, document
    assert document["result"]["findings"] == []
    assert document["result"]["gaps"] == []
    assert workspace.gate("design")["result"] == "passed"


def test_a_mapping_may_read_a_view_the_profile_does_not_hold(workspace, complete):
    """A profile measures what the source holds; a view exists only once its query has run."""
    put("sources.pumps.views", {"pump_log": 'SELECT * FROM "pumps"."pump_events"'})(complete)
    put("sources.pumps.mappings.pumps.dataset", "pump_log")(complete)
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 0, document
    assert document["result"]["findings"] == []


def test_every_registered_check_has_a_case():
    assert {case[1] for case in CASES} == {spec.code for spec in CHECKS}


@pytest.mark.parametrize(
    ("code", "severity", "path", "mutation"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_one_violation_gives_exactly_its_finding(
    workspace, complete, code, severity, path, mutation
):
    mutation(complete)
    workspace.write_manifest(complete)
    result = workspace.run("--json", "manifest", "validate")
    document = json.loads(result.stdout)

    findings = _findings(document)
    assert [(f["code"], f["severity"], f["path"]) for f in findings] == [(code, severity, path)]
    assert result.exit_code == (1 if severity == "error" else 0)
    assert workspace.gate("design")["result"] == ("failed" if severity == "error" else "passed")
    # Neither the report nor the logs may repeat a credential that was written inline.
    assert "k3yL1teral" not in result.stdout + result.stderr


def test_validate_text_mode_prints_findings_and_exits_1(workspace, complete):
    put("contract.objective.kpi", "f_missing")(complete)
    workspace.write_manifest(complete)
    result = workspace.run("manifest", "validate")
    assert result.exit_code == 1
    assert "ref.objective_kpi" in result.stdout
    assert "contract.objective.kpi" in result.stdout
    assert "validate.failed" in result.stderr


def test_validate_complete_fails_on_gaps_alone(workspace, complete):
    del complete["field_status"]["contract.purpose"]
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate", "--complete")
    assert code == 1
    detail = document["error"]["detail"]
    assert detail["findings"] == []
    assert [gap["path"] for gap in detail["gaps"]] == ["contract.purpose"]
    # The design gate looks at error findings only, so it still passes.
    assert workspace.gate("design")["result"] == "passed"


def test_design_gate_goes_stale_when_the_manifest_changes(workspace, complete):
    workspace.write_manifest(complete)
    assert workspace.run("manifest", "validate").exit_code == 0
    complete["twin"]["name"] = "Renamed"
    workspace.write_manifest(complete)
    assert workspace.gate("design")["result"] == "stale"


def test_schema_error_cannot_run(workspace, complete):
    complete["contract"]["tier"] = "sometimes"
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 2
    assert document["error"]["code"] == "manifest.schema"


_ONTOLOGY = """\
id: https://example.org/dta/sample
name: sample
prefixes:
  linkml: https://w3id.org/linkml/
  ex: https://example.org/dta/sample/
default_prefix: ex
default_range: string
imports:
  - linkml:types
classes:
  Shed:
    attributes:
      uid:
        identifier: true
        required: true
"""


def _write_ontology(workspace, complete, text):
    workspace.write_manifest(complete)
    target = workspace.root / complete["ontology"]["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_validate_checks_the_manifest_against_its_ontology_file(workspace, complete):
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 0, document  # no ontology file yet: only the manifest's own checks run

    _write_ontology(workspace, complete, _ONTOLOGY)
    code, document = workspace.json("manifest", "validate")
    assert code == 1
    found = document["error"]["detail"]["findings"]
    codes = {finding["code"] for finding in found if finding["severity"] == "error"}
    assert "ontology.unknown_type" in codes
    assert workspace.gate("design")["result"] == "failed"
    # The gaps gate looks at the same findings, so it cannot pass either.
    assert any("error finding" in reason for reason in workspace.gate("gaps")["reasons"])


def test_an_ontology_file_that_cannot_be_loaded_is_one_error_finding(workspace, complete):
    _write_ontology(workspace, complete, "classes: [this is not an ontology\n")
    code, document = workspace.json("manifest", "validate")
    assert code == 1
    found = [f for f in document["error"]["detail"]["findings"] if f["severity"] == "error"]
    assert [(f["code"], f["path"]) for f in found] == [("ontology.unusable", "ontology.path")]


def test_signing_is_refused_while_the_manifest_does_not_fit_its_ontology(workspace, complete):
    _write_ontology(workspace, complete, _ONTOLOGY)
    code, document = workspace.json(
        "manifest", "sign", "--non-interactive", "--approved-by", "tester"
    )
    assert code == 1
    assert document["error"]["code"] == "sign.errors"
