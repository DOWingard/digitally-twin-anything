from datetime import timedelta

import pytest
from dta_core.errors import DtaError
from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import CHECKS, Issue, Severity, check, run_checks
from dta_core.manifest.checks.view import fmt_duration, is_property_ref, placeholders

EXPECTED_CODES = {
    "ref.question_function",
    "ref.function_entry",
    "ref.model_family",
    "ref.code_declared",
    "ref.question_model",
    "ref.model_question",
    "ref.control_action",
    "ref.loop",
    "ref.objective_kpi",
    "ref.env_declared",
    "ref.env_unused",
    "ref.property_format",
    "ref.mapping_columns",
    "ref.clock_source",
    "ref.incremental",
    "control.bounds",
    "control.binding",
    "action.executor_addon",
    "tier.static_control",
    "tier.static_extras",
    "secret.inline",
    "function.interval",
    "status.stale",
    "coverage.unmapped_target",
    "feasible.staleness",
    "feasible.interval",
    "feasible.noise",
    "feasible.history",
    "feasible.acceptance",
    "grid.no_interval",
    "grid.horizon",
    "grid.backtest_step",
    "sync.mapping_invalid",
}
WARNINGS = {
    "ref.env_unused",
    "tier.static_extras",
    "status.stale",
    "coverage.unmapped_target",
    "feasible.acceptance",
}
LIVE_ONLY = {"feasible.staleness", "feasible.interval"}


def test_every_check_is_registered_once_with_its_severity():
    codes = [spec.code for spec in CHECKS]
    assert sorted(codes) == sorted(EXPECTED_CODES)
    assert {spec.code for spec in CHECKS if spec.severity == Severity.warning} == WARNINGS
    assert {spec.code for spec in CHECKS if spec.live_only} == LIVE_ONLY


def test_a_code_cannot_be_registered_twice():
    with pytest.raises(ValueError):
        check("ref.loop")(lambda view: [])


def test_a_new_check_reports_under_its_code_and_a_crash_aborts_validation():
    def always(view):
        yield Issue("twin", "always reported")

    def crash(view):
        raise RuntimeError("boom")

    manifest = parse_manifest({"twin": {"id": "demo", "name": "Demo"}})
    try:
        check("test.always")(always)
        findings = run_checks(manifest)
        assert [(f.code, f.path) for f in findings if f.code.startswith("test.")] == [
            ("test.always", "twin")
        ]
        check("test.crash")(crash)
        with pytest.raises(DtaError) as caught:
            run_checks(manifest)
        assert caught.value.code == "check.internal"
        assert caught.value.detail == {"check": "test.crash"}
    finally:
        CHECKS[:] = [spec for spec in CHECKS if not spec.code.startswith("test.")]


def test_findings_are_sorted_by_path_then_code():
    manifest = parse_manifest(
        {
            "contract": {"tier": "static", "questions": {"q": {"targets": ["bad"]}}},
            "env": {"ZZZ": {"description": "unused"}},
            "functions": {"f": {"trigger": "interval"}},
        }
    )
    paths = [finding.path for finding in run_checks(manifest)]
    assert paths == sorted(paths)


def test_property_refs_placeholders_and_durations():
    assert is_property_ref("Tank.level") and is_property_ref("Pump2.max_speed_rpm")
    assert not any(map(is_property_ref, ["tank.level", "Tank.Level", "Tank", "Tank.level.x"]))
    assert placeholders("pg://${USER}:${PASS}@h/${DB}") == ["USER", "PASS", "DB"]
    assert placeholders("${1BAD} $NOT ${}") == []
    assert fmt_duration(timedelta(days=1, hours=6)) == "1 d 6 h"
    assert fmt_duration(timedelta(seconds=90.5)) == "1 min 30.5 s"
    assert fmt_duration(timedelta(0)) == "0 s"
