"""What the stack commands do when something is wrong, each on real containers."""

from __future__ import annotations

import json
import os

import m3tk_kit as kit

pytestmark = kit.needs_stack


def test_unreadable_source_fails_the_access_gate_and_names_it(make_twin) -> None:
    twin = make_twin()
    twin.write()
    twin.ok("ontology", "check", "--write")
    twin.ok("env", "template")
    twin.fill_env()
    os.chmod(twin.data / "readings.csv", 0)  # the stack's user cannot read it

    error = twin.failed("access.failed", "source", "probe")
    failing = error["detail"]["failing"]
    assert [(entry["source"], entry["dataset"]) for entry in failing] == [("plant", "readings")]
    assert failing[0]["error_code"]
    assert error["detail"]["gate_recorded"] is True
    gate = next(g for g in twin.ok("status")["gates"] if g["name"] == "access")
    assert gate["result"] == "failed"
    assert twin.leaked() == []


def test_failing_build_step_stops_the_build_and_a_rerun_completes_it(make_twin) -> None:
    twin = make_twin()
    twin.prepare()
    readings = twin.data / "readings.csv"
    os.chmod(readings, 0)

    error = twin.failed("build.failed", "build")
    detail = error["detail"]
    assert detail["step"] == "load"
    assert [(e["step"], e["status"]) for e in detail["steps"]] == [
        ("scaffold", "ok"),
        ("image", "skipped"),
        ("store", "ok"),
        ("install", "ok"),
    ]
    assert detail["not_attempted"] == [
        "api",
        "contract",
        "sync",
        "code",
        "functions",
        "models",
        "questions",
        "ui",
        "mcp",
        "skill",
        "sdk",
    ]
    assert detail["evidence"]["code"].startswith("sync.")
    record = json.loads((twin.root / ".dta" / "build.json").read_text(encoding="utf-8"))
    assert record["steps"]["load"]["status"] == "failed"
    assert "api" not in record["steps"]
    assert twin.gates()["build"] == "failed"

    os.chmod(readings, 0o644)
    resumed = twin.ok("build")
    actions = {entry["step"]: (entry["action"], entry["status"]) for entry in resumed["steps"]}
    assert actions == {
        "scaffold": ("ran", "ok"),
        "image": ("ran", "skipped"),
        "store": ("ran", "ok"),
        "install": ("ran", "ok"),
        "load": ("ran", "ok"),
        "api": ("ran", "ok"),
        "contract": ("ran", "ok"),
        "sync": ("ran", "skipped"),
        "code": ("ran", "skipped"),
        "functions": ("ran", "ok"),
        "models": ("ran", "skipped"),
        "questions": ("ran", "ok"),
        "ui": ("ran", "skipped"),
        "mcp": ("ran", "skipped"),
        "skill": ("ran", "ok"),
        "sdk": ("ran", "skipped"),
    }
    assert twin.gates()["build"] == "passed"
    assert twin.leaked() == []


def test_accept_with_a_failing_check_writes_only_the_deficit_report(make_twin) -> None:
    twin = make_twin(bad_row=True)  # one reading below the ontology's minimum
    twin.prepare()
    built = twin.ok("build")
    load = next(entry for entry in built["steps"] if entry["step"] == "load")["detail"]
    assert load["rows_rejected"] == 1
    reports = twin.root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "acceptance.json").write_text("{}\n", encoding="utf-8")  # from an earlier run

    error = twin.failed("accept.failed", "accept", "--examples", "2")
    detail = error["detail"]
    assert detail["failed"] == ["data.constraints"]
    constraints = next(c for c in detail["checks"] if c["id"] == "data.constraints")
    assert constraints["evidence"]["rows_rejected"] == 1 and constraints["remedy"]
    assert (reports / "deficit.json").is_file() and (reports / "deficit.md").is_file()
    assert not (reports / "acceptance.json").exists() and not (reports / "acceptance.md").exists()
    written = json.loads((reports / "deficit.json").read_text(encoding="utf-8"))
    assert written["passed"] is False and written["failed"] == ["data.constraints"]
    assert twin.gates()["acceptance"] == "failed"
    assert twin.leaked([(reports / "deficit.md").read_text(encoding="utf-8")]) == []
