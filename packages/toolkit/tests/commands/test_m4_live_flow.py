"""A live twin from its manifest to handover, then kept in step by its own scheduler.

Real containers, the image of this tree. The twin reads one reading per sensor
and minute; profiling measures that, and the derived interval is one minute.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import m3tk_kit as kit
import m4_live as live

pytestmark = kit.needs_stack


def test_live_flow_from_manifest_to_handover_and_on(make_twin, tmp_path: Path) -> None:
    twin = make_twin(live=True)
    twin.write()
    twin.ok("ontology", "check", "--write")
    twin.ok("env", "template")
    twin.fill_env()
    twin.ok("source", "probe")
    profiled = twin.ok("profile")
    assert profiled["profiles"]["plant"]["refresh_period"] == "PT1M"
    derived = twin.ok("sync", "derive", "--write")
    assert derived["interval"] == "PT1M" and derived["written"] is True
    twin.ok("manifest", "validate")
    twin.ok("manifest", "confirm", "--all-user")
    twin.ok("manifest", "validate")
    twin.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    started = time.perf_counter()
    built = twin.ok("build")
    build_s = time.perf_counter() - started
    assert live.steps(built) == {
        "scaffold": ("ran", "ok"),
        "store": ("ran", "ok"),
        "install": ("ran", "ok"),
        "load": ("ran", "ok"),
        "api": ("ran", "ok"),
        "contract": ("ran", "ok"),
        "sync": ("ran", "ok"),
        **live.COMPUTE_STEPS,
        "ui": ("ran", "skipped"),
        "skill": ("ran", "ok"),
        **live.ADDON_STEPS,
    }
    assert [entry["step"] for entry in built["steps"]] == list(live.STEPS)
    load = live.step_detail(built, "load")
    assert load["kind"] == "backfill" and load["rows_rejected"] == 0
    assert load["writers"]["scheduler"] == "stopped" and load["writers"]["api"] == "not running"
    synced = live.step_detail(built, "sync")
    assert synced["status"] == "succeeded" and synced["kind"] == "incremental"
    assert synced["run"] and synced["rows_rejected"] == 0 and synced["failing_sources"] == []
    assert synced["clock"] == {"kind": "wall", "available": True}
    assert synced["freshness"]["staleness"] == "PT1H" and synced["freshness"]["fresh"] is True
    assert synced["scheduler"] == {"state": "running"}
    assert twin.gates()["build"] == "passed"
    assert build_s <= live.LIVE_BUILD_LIMIT_S, f"live build took {build_s:.1f} s"

    started = time.perf_counter()
    accepted = twin.ok("accept")
    accept_s = time.perf_counter() - started
    assert accepted["passed"] is True and accepted["failed"] == []
    assert {entry["id"] for entry in accepted["checks"]} == live.CHECKS
    ids = [entry["id"] for entry in accepted["checks"]]
    assert ids[-17:] == [
        "skill.current",
        "skill.endpoints",
        *live.SYNC_CHECKS,
        *live.COMPUTE_CHECKS,
        *live.ACTING_CHECKS,
        *live.ADDON_CHECKS,
    ]
    scheduler = live.check(accepted, "sync.scheduler")["evidence"]
    assert scheduler["service"]["state"] == "running" and scheduler["scheduler_jobs"] >= 1
    assert all(
        job["idempotency_key"].startswith("sync:") for job in scheduler["latest_scheduler_jobs"]
    )
    runs = live.check(accepted, "sync.runs")["evidence"]
    assert runs["last_run"]["status"] == "succeeded"
    sources = live.check(accepted, "sync.sources")["evidence"]
    assert [(s["id"], s["status"]) for s in sources["sources"]] == [("plant", "ok")]
    freshness = live.check(accepted, "sync.freshness")["evidence"]
    assert freshness["fresh"] is True and [q["id"] for q in freshness["questions"]] == ["q_level"]
    constraints = live.check(accepted, "data.constraints")["evidence"]
    assert constraints["load"]["kind"] == "backfill" and constraints["latest_run"]["uid"]
    endpoints = live.check(accepted, "skill.endpoints")
    assert endpoints["passed"] and endpoints["evidence"]["missing"] == []
    assert endpoints["evidence"]["listed"] > 20
    assert accept_s <= live.LIVE_ACCEPT_LIMIT_S, f"live accept took {accept_s:.1f} s"

    target = tmp_path / "agent-skills"
    twin.ok("install-skill", "--target", str(target))
    assert twin.gates()["handover"] == "passed"

    # New readings reach the twin with no command: the scheduler's next run takes them.
    with live.api(twin) as client:
        before = client.get("/api/v1/meta/health").json()["sync"]["last_run"]["uid"]
        known = {job["id"] for job in client.get("/api/v1/lifecycle/jobs").json()["data"]}
    appended_at = live.now() - timedelta(seconds=1)
    stamp = twin.append_readings(appended_at)
    waited_from = time.perf_counter()
    taken = None
    with live.api(twin) as client:
        while time.perf_counter() - waited_from < live.SCHEDULED_PICKUP_LIMIT_S:
            sources = client.get("/api/v1/sync/sources").json()["data"]
            watermark = sources[0]["watermark"]
            if watermark is not None and watermark >= stamp:
                taken = sources[0]
                break
            time.sleep(2.0)
        pickup_s = time.perf_counter() - waited_from
        assert taken is not None, f"the appended readings were not taken in {pickup_s:.0f} s"
        health = client.get("/api/v1/meta/health").json()
        jobs = client.get("/api/v1/lifecycle/jobs").json()["data"]
    last_run = health["sync"]["last_run"]
    assert last_run["uid"] != before and last_run["status"] == "succeeded"
    # Nothing but the scheduler asked for a run since: every new job carries its key.
    new = [job for job in jobs if job["id"] not in known]
    assert new and all(job["kind"] == "sync" for job in new)
    assert all(str(job["idempotency_key"]).startswith("sync:") for job in new), new

    status = twin.ok("sync", "status")
    assert status["clock"]["kind"] == "wall" and status["clock"]["available"] is True
    assert status["sync"]["fresh"] is True and status["sync"]["interval"] == "PT1M"
    assert [(s["id"], s["status"]) for s in status["sources"]] == [("plant", "ok")]
    assert status["sources"][0]["watermark"] >= stamp
    assert status["sources"][0]["freshness_s"] < 3600
    question = status["sync"]["questions"][0]
    assert question["id"] == "q_level" and question["fresh"] is True
    text = twin.dta.run("sync", "status")
    assert text.exit_code == 0 and "plant" in text.stdout and "fresh: yes" in text.stdout
    twin.outputs.append(text.stdout)

    started = time.perf_counter()
    code, document = live.sync_run(twin)
    run_s = time.perf_counter() - started
    assert code == 0, document
    report = document["result"]["report"]
    assert report["status"] == "succeeded" and report["kind"] == "incremental"
    assert report["sources"]["plant"]["status"] == "ok"
    assert document["result"]["job"]["state"] == "succeeded"
    assert run_s <= live.SYNC_RUN_LIMIT_S, f"dta sync run took {run_s:.1f} s"

    code, document = live.sync_run(twin, "--source", "plant")
    assert code == 0, document
    assert document["result"]["asked"] == {"sources": ["plant"]}
    assert document["result"]["job"]["params"]["sources"] == ["plant"]
    assert document["result"]["report"]["status"] == "succeeded"

    code, document = live.sync_run(twin, "--replay", "plant.readings")
    assert code == 1, document
    assert document["error"]["code"] == "sync.replay_unsupported"
    assert "state series" in document["error"]["message"]

    code, document = live.sync_run(twin, "--replay", "plant.nothing")
    assert code == 1 and document["error"]["code"] == "sync.replay_unknown", document

    code, document = live.sync_run(twin, "--no-wait")
    assert code == 0, document
    assert document["result"]["job"]["id"] and document["result"]["job"]["state"] in {
        "queued",
        "running",
        "succeeded",
    }
    text = twin.dta.run("sync", "run")
    assert text.exit_code in (0, 1)
    twin.outputs.append(text.stdout + text.stderr)

    # A skill that names an endpoint the API does not serve fails acceptance, naming it.
    api_md = twin.root / "skill" / f"twin-{twin.id}" / "reference" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    api_md.write_text(
        text.replace(
            "| `GET /sync/sources` |",
            "| `GET /sync/nowhere/{id}?x=` | made up |\n| `GET /sync/sources` |",
        ),
        encoding="utf-8",
    )
    error = twin.failed("accept.failed", "accept", "--examples", "1")
    assert error["detail"]["failed"] == ["skill.endpoints"]
    endpoints = live.check(error["detail"], "skill.endpoints")
    assert endpoints["evidence"]["missing"] == ["GET /sync/nowhere/{id}"]
    assert "the skill and the API disagree" in endpoints["remedy"]
    twin.ok("generate", "skill")
    assert twin.ok("accept", "--examples", "1")["passed"] is True

    written = [
        twin.root / ".dta" / "logs" / "toolkit.log",
        twin.root / ".dta" / "build.json",
        *sorted((twin.root / "reports").iterdir()),
    ]
    texts = [path.read_text(encoding="utf-8") for path in written if path.is_file()]
    assert twin.leaked(texts) == [], "a value of .env reached the output, a log or a report"
    print(
        f"\nlive build {build_s:.1f} s (limit {live.LIVE_BUILD_LIMIT_S:g}), "
        f"accept {accept_s:.1f} s (limit {live.LIVE_ACCEPT_LIMIT_S:g}), "
        f"sync run {run_s:.1f} s (limit {live.SYNC_RUN_LIMIT_S:g}), "
        f"scheduled pick-up {pickup_s:.1f} s (limit {live.SCHEDULED_PICKUP_LIMIT_S:g})"
    )
