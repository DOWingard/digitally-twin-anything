"""What building, accepting and syncing a live twin report when something is wrong.

Each fault is made alone, on real containers, and only the checks it concerns fail.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import m3tk_kit as kit
import m4_live as live

pytestmark = kit.needs_stack


def test_a_stopped_scheduler_and_a_failing_source_fail_only_their_checks(make_twin) -> None:
    twin = make_twin(live=True)
    twin.prepare()
    twin.ok("build")
    readings = twin.data / "readings.csv"

    live.stop_service(twin, "scheduler")
    error = twin.failed("accept.failed", "accept", "--examples", "1")
    assert error["detail"]["failed"] == ["sync.scheduler"]
    scheduler = live.check(error["detail"], "sync.scheduler")
    assert scheduler["evidence"]["service"]["state"] == "exited"
    assert "the scheduler container is exited" in scheduler["evidence"]["reason"]
    assert "dta up scheduler" in scheduler["remedy"]
    twin.ok("up", "scheduler")

    # The source cannot be read: the run fails and names it; the twin keeps what it had.
    os.chmod(readings, 0)
    code, document = live.sync_run(twin)
    assert code == 1, document
    assert document["error"]["code"] == "sync.source_failed"
    assert "plant" in document["error"]["message"]
    failing = document["error"]["detail"]["report"]["sources"]["plant"]
    assert failing["status"] == "failing" and failing["error"]["code"]
    status = twin.ok("sync", "status")
    assert [(s["id"], s["status"]) for s in status["sources"]] == [("plant", "failing")]
    assert status["sync"]["last_run"]["status"] == "failed"

    error = twin.failed("accept.failed", "accept", "--examples", "1")
    assert error["detail"]["failed"] == ["sync.runs", "sync.sources"]
    runs = live.check(error["detail"], "sync.runs")
    assert runs["evidence"]["last_run"]["status"] == "failed" and runs["remedy"]
    sources = live.check(error["detail"], "sync.sources")
    assert sources["evidence"]["failing"] == ["plant"]
    assert sources["evidence"]["sources"][0]["last_error"]

    # A build that reaches `sync` with the source unreadable fails there, naming it.
    error = twin.failed("build.failed", "build")
    detail = error["detail"]
    assert detail["step"] == "sync"
    assert [(e["step"], e["action"], e["status"]) for e in detail["steps"]] == [
        ("scaffold", "ran", "ok"),
        ("image", "ran", "skipped"),
        ("store", "ran", "ok"),
        ("install", "ran", "ok"),
        ("load", "reused", "ok"),
        ("api", "ran", "ok"),
        ("contract", "ran", "ok"),
    ]
    assert detail["not_attempted"] == ["code", "functions", "models", "questions", "ui", "skill"]
    evidence = detail["evidence"]
    assert evidence["code"] == "sync.source_failed"
    assert [entry["source"] for entry in evidence["failing_sources"]] == ["plant"]
    assert evidence["failing_sources"][0]["code"] and evidence["failing_sources"][0]["message"]
    assert "source plant (" in evidence["message"] and "source plant (" in error["message"]
    record = json.loads((twin.root / ".dta" / "build.json").read_text(encoding="utf-8"))
    assert record["steps"]["sync"]["status"] == "failed"
    assert twin.gates()["build"] == "failed"

    os.chmod(readings, 0o644)
    mended = twin.ok("build")
    assert live.steps(mended)["sync"] == ("ran", "ok")
    assert live.steps(mended)["skill"] == ("ran", "ok")
    assert twin.gates()["build"] == "passed"
    assert twin.ok("accept", "--examples", "1")["passed"] is True
    logs = (twin.root / ".dta" / "logs" / "toolkit.log").read_text(encoding="utf-8")
    assert twin.leaked([logs]) == []


def test_readings_a_day_old_fail_only_freshness(make_twin) -> None:
    twin = make_twin(live=True, readings_end=live.now() - timedelta(days=1))
    twin.prepare()
    built = twin.ok("build")
    synced = live.step_detail(built, "sync")
    # The build passes; how fresh the twin is, is for acceptance to judge.
    assert synced["status"] == "succeeded" and synced["freshness"]["fresh"] is False

    error = twin.failed("accept.failed", "accept", "--examples", "1")
    assert error["detail"]["failed"] == ["sync.freshness"]
    freshness = live.check(error["detail"], "sync.freshness")
    evidence = freshness["evidence"]
    assert evidence["fresh"] is False and evidence["clock"]["available"] is True
    [question] = evidence["questions"]
    assert question["id"] == "q_level" and question["fresh"] is False
    assert question["source"] == "plant" and question["freshness_s"] > 86_000
    assert evidence["sources"][0]["freshness_s"] > 86_000
    reason = evidence["reason"]
    assert "q_level" in reason and "limit PT1H" in reason and "source plant" in reason
    assert "/sync/sources" in freshness["remedy"] and "dta sync run" in freshness["remedy"]
    assert "staleness" in freshness["remedy"]
    deficit = (twin.root / "reports" / "deficit.md").read_text(encoding="utf-8")
    assert twin.leaked([deficit]) == []


def test_a_static_twin_does_not_sync(make_twin) -> None:
    twin = make_twin()
    twin.prepare()
    built = twin.ok("build")
    assert live.steps(built)["sync"] == ("ran", "skipped")
    assert live.step_detail(built, "sync")["reason"].startswith("contract.tier is static")

    error = twin.failed("twin.not_live", "sync", "run")
    assert error["detail"]["status"] == 409
    status = twin.ok("sync", "status")
    assert status["tier"] == "static" and status["sync"] is None
    assert [s["id"] for s in status["sources"]] == ["plant"]
    text = twin.dta.run("sync", "status")
    assert text.exit_code == 0 and "the manifest sets no sync interval" in text.stdout

    accepted = twin.ok("accept", "--examples", "1")
    assert accepted["passed"] is True
    for ident in live.SYNC_CHECKS:
        result = live.check(accepted, ident)
        assert result["passed"] and result["evidence"]["applicable"] is False
    assert live.check(accepted, "skill.endpoints")["evidence"]["missing"] == []
    assert twin.leaked([text.stdout]) == []
