"""The parts of acceptance that need no running twin: inputs, verdicts and the report."""

from __future__ import annotations

import httpx
import m3tk_kit as kit
import pytest
from dta_core.errors import DtaError
from dta_core.manifest import parse_manifest
from dta_toolkit.acceptance import (
    AcceptanceRun,
    _guarded,
    render_markdown,
    service_url,
    skill_current,
    ui_reachable,
)
from dta_toolkit.acceptance.data import covered_properties
from dta_toolkit.acceptance.readiness import (
    SLICE_VERSIONS,
    domain_edge_types,
    mapped_types,
    slice_request,
    timed_properties,
)
from dta_toolkit.acceptance.result import CheckResult, described, problem_of


@pytest.fixture
def manifest():
    return parse_manifest(kit.manifest("m3tk-00000000"))


def test_what_the_checks_read_from_the_manifest(manifest) -> None:
    assert sorted(mapped_types(manifest)) == ["Sensor", "Site"]
    assert timed_properties(manifest) == [("Sensor", "level")]
    assert covered_properties(manifest) == ["Sensor.level"]


def test_domain_edge_types_keep_the_schema_order_and_holders() -> None:
    schema = {
        "edge_types": [
            {"name": "Sensor.site", "holder": "Sensor"},
            {"name": "TwinSyncRun.version", "holder": "TwinSyncRun"},
            {"name": "Site.parent", "holder": "Site"},
        ]
    }
    assert domain_edge_types(schema, ["Site", "Sensor"]) == ["Sensor.site", "Site.parent"]
    assert domain_edge_types({}, ["Site"]) == []


def test_the_history_slice_is_of_one_object_over_a_bounded_range() -> None:
    # A slice grows with objects times versions; the API refuses a large one.
    young = slice_request(2, 40, "Sensor", "level", "sensor:S1")
    assert young == {
        "range": "#2..head",
        "mode": "state",
        "layout": "long",
        "uids": "sensor:S1",
        "properties": "level",
        "limit": SLICE_VERSIONS,
    }
    old = slice_request(2, 1_000_000, "Sensor", "level", "sensor:S1")
    assert old["range"] == f"#{1_000_000 - SLICE_VERSIONS + 1}..head"
    assert old["uids"] == "sensor:S1"


def test_service_url_names_the_variable_never_its_value() -> None:
    assert service_url({"DTA_API_PORT": "18801"}, "DTA_API_PORT") == "http://127.0.0.1:18801"
    with pytest.raises(DtaError) as missing:
        service_url({}, "DTA_API_PORT")
    assert missing.value.code == "env.missing"
    with pytest.raises(DtaError) as bad:
        service_url({"DTA_API_PORT": "not-a-port-secretish"}, "DTA_API_PORT")
    assert bad.value.code == "env.bad_value"
    assert "not-a-port-secretish" not in bad.value.message
    assert "not-a-port-secretish" not in str(bad.value.as_dict())


def test_ui_reachable_does_not_apply_without_a_ui(manifest) -> None:
    result = ui_reachable(manifest, {})
    assert result.passed and result.evidence["applicable"] is False


def test_ui_reachable_fails_with_a_remedy_when_nothing_answers() -> None:
    manifest = parse_manifest(kit.manifest("m3tk-00000000", ui=True))
    port = kit.free_port()
    result = ui_reachable(manifest, {"DTA_UI_PORT": str(port)})
    assert not result.passed and "dta logs ui" in (result.remedy or "")


def test_skill_current_compares_the_rendered_hash() -> None:
    assert skill_current("abc", "abc").passed
    stale = skill_current("abc", "def")
    assert not stale.passed and stale.as_dict()["remedy"]
    assert not skill_current(None, "def").passed


def test_one_check_that_cannot_run_fails_alone() -> None:
    def unreachable() -> CheckResult:
        raise DtaError("GET /api/v1/meta/schema did not get an answer", code="api.unreachable")

    def crashing() -> CheckResult:
        raise KeyError("items")

    checks = [
        _guarded("readiness.R1", unreachable),
        _guarded("readiness.R2", crashing),
        _guarded("skill.current", lambda: skill_current("a", "a")),
    ]
    run = AcceptanceRun(checks=checks)
    assert run.failed == ["readiness.R1", "readiness.R2"] and not run.passed
    assert checks[0].evidence["error"]["code"] == "api.unreachable"
    assert checks[1].evidence["error"].startswith("KeyError")
    assert checks[2].passed and checks[2].duration_ms >= 0


def test_problem_and_request_as_reports_show_them() -> None:
    problem = httpx.Response(404, json={"code": "object.not_found", "title": "Not found"})
    assert problem_of(problem) == {"status": 404, "code": "object.not_found", "title": "Not found"}
    assert problem_of(httpx.Response(200, json={"items": []})) == {"status": 200}
    assert problem_of(httpx.Response(502, text="bad gateway")) == {"status": 502}
    assert described("GET", "/api/v1/objects/Site") == "GET /api/v1/objects/Site"
    assert described("GET", "/x", {"limit": 1, "depth": 2}) == "GET /x?limit=1&depth=2"


def _document(passed: bool) -> dict:
    checks = [
        CheckResult("readiness.R1", "the schema", True, {"types": 2}).as_dict(),
        CheckResult(
            "data.constraints",
            "no row set aside",
            passed,
            {"rows_rejected": 0 if passed else 1},
            "Correct the rows in the rejects file.",
        ).as_dict(),
    ]
    return {
        "twin_id": "m3tk-00000000",
        "manifest_hash": "abc",
        "at": "2026-11-11T00:00:00+00:00",
        "passed": passed,
        "failed": [] if passed else ["data.constraints"],
        "checks": checks,
    }


def test_the_report_names_each_check_and_what_to_change() -> None:
    deficit = render_markdown(_document(False))
    assert deficit.startswith("# Deficit report: m3tk-00000000")
    assert "failed: data.constraints" in deficit
    assert "| data.constraints | NO |" in deficit
    assert "What to change: Correct the rows in the rejects file." in deficit
    assert '"rows_rejected": 1' in deficit

    accepted = render_markdown(_document(True))
    assert accepted.startswith("# Acceptance report: m3tk-00000000")
    assert "every check passed" in accepted and "What to change" not in accepted
