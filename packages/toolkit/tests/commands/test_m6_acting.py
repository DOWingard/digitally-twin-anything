"""A live twin that checks itself and acts, built and accepted on real containers: what
the build sets up and reports, the acceptance checks that judge it, the commands an
operator uses, the incidents the twin finds by itself, and what each rebuild redoes or
refuses.

One twin serves the whole module, its tests running in the order written: each leaves
the twin as the next expects it. Thresholds are fixed before measuring; the times are
printed.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import m5_recipes
import pytest
from dta_core.log import configure
from dta_core.manifest import parse_manifest
from dta_toolkit.acceptance.acting import (
    actions_registered,
    fidelity_schedule,
    fidelity_state,
    notifications_configured,
)
from dta_toolkit.acceptance.client import TwinApi
from dta_toolkit.acceptance.readiness import Context
from dta_toolkit.acceptance.result import CheckResult
from dta_toolkit.envfile import read_env
from m3tk_kit import needs_stack
from m6_kit import FROZEN, HOOK_PATH, PLANT_URL, M6Twin

from conftest import Dta

pytestmark = needs_stack

# Fixed before measuring.
FIRST_BUILD_LIMIT_S = 420.0
ACTION_REBUILD_LIMIT_S = 20.0
CHECKS_LIMIT_S = 3.0
RUNS = 3
FRESH_LIMIT_S = 180.0
RECONCILE_LIMIT_S = 150.0
INCIDENT_LIMIT_S = 600.0
READING_LIMIT_S = 240.0
CHECK_LIMIT_S = 120.0
HEALTHY_LIMIT_S = 300.0
NEW_CHECKS = (
    "fidelity.state",
    "fidelity.schedule",
    "actions.registered",
    "notifications.configured",
)
OPERATOR = "Test Operator"
ALERT_URL = "ALERT_URL"
PLANT_KEY = "PLANT_KEY"
CONTROLS_PATH = "/api/v1/controls"


@pytest.fixture(scope="module")
def twin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[M6Twin]:
    base = tmp_path_factory.mktemp("m6")
    with pytest.MonkeyPatch.context() as patch:
        (base / "home").mkdir()
        patch.setenv("HOME", str(base / "home"))
        made = M6Twin(Dta(base / "ws"), base)
        try:
            made.prepare()
            yield made
        finally:
            made.teardown()
            assert made.leaked() == []


# -- helpers ---------------------------------------------------------------------------


def steps(result: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {entry["step"]: (entry["action"], entry["status"]) for entry in result["steps"]}


def detail(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in result["steps"] if entry["step"] == name)["detail"]


def check(result: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(found for found in result["checks"] if found["id"] == ident)


def get(twin: M6Twin, path: str, **params: Any) -> dict[str, Any]:
    env = read_env(twin.env_file)
    with httpx.Client(
        base_url=env["DTA_BASE_URL"] + "/api/v1",
        headers={"Authorization": "Bearer " + env["DTA_API_TOKEN"]},
        timeout=120.0,
    ) as api:
        response = api.get(path, params=params or None)
    assert response.status_code == 200, (path, response.text)
    return response.json()


def built(twin: M6Twin, *args: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = twin.ok("build", *args)
    return result, time.perf_counter() - started


def durations(result: dict[str, Any]) -> str:
    return ", ".join(f"{e['step']} {e.get('duration_ms', 0) / 1000:.1f}" for e in result["steps"])


def api_start_recorded(twin: M6Twin) -> str:
    """The digest of what the API set up at its start, as the last build recorded it."""
    record = json.loads((twin.root / ".dta" / "build.json").read_text(encoding="utf-8"))
    return record["steps"]["api"]["detail"]["api_start"]["start_config"]


def wait_for(
    what: str,
    found: Callable[[], Any],
    limit_s: float,
    pause_s: float = 5.0,
    seen: Callable[[], Any] | None = None,
) -> Any:
    """`found()` once it is truthy; fails after `limit_s` seconds with what `seen()` says."""
    deadline = time.monotonic() + limit_s
    while True:
        value = found()
        if value:
            return value
        if time.monotonic() >= deadline:
            last = seen() if seen is not None else None
            raise AssertionError(f"{what}: not within {limit_s:.0f} s; last seen: {last}")
        time.sleep(pause_s)


def state(twin: M6Twin) -> str:
    return twin.ok("fidelity", "status")["state"]


def unresolved(twin: M6Twin, kind: str | None = None) -> list[dict[str, Any]]:
    args = ["fidelity", "incidents", "--status", "unresolved"]
    if kind:
        args += ["--kind", kind]
    return twin.ok(*args)["incidents"]


@contextmanager
def checks_context(twin: M6Twin, manifest: dict[str, Any] | None = None) -> Iterator[Context]:
    """What the acceptance checks read: the API, and the workspace's manifest or a variant."""
    # The commands run before logged to their runner's streams, closed since.
    configure()
    document = parse_manifest(manifest if manifest is not None else twin.dta.read_manifest())
    with TwinApi(document, read_env(twin.env_file)) as api:
        yield Context(api=api, manifest=document, ontology=None)  # type: ignore[arg-type]


def judged(
    twin: M6Twin, run: Callable[[Context], CheckResult], manifest: dict[str, Any]
) -> CheckResult:
    with checks_context(twin, manifest) as ctx:
        return run(ctx)


def fresh(twin: M6Twin) -> bool:
    """Both sources have been read up to the minute before the last one that ended."""
    wanted = twin.at(twin.minute() - 2)
    for source in get(twin, "/sync/sources")["data"]:
        mark = source.get("watermark")
        if mark is None or datetime.fromisoformat(mark) < wanted:
            return False
    return True


def proposal(twin: M6Twin, site: str, value: float, *extra: str) -> dict[str, Any]:
    return twin.ok(
        "actions",
        "propose",
        "set_inflow",
        "--object",
        site,
        "--param",
        f"value={value}",
        "--source",
        "test-agent",
        *extra,
    )["data"]


# -- the first build and acceptance ----------------------------------------------------


def test_the_twin_is_built_with_its_actions_and_loops_and_accepted(twin: M6Twin) -> None:
    result, took = built(twin)
    print(f"\nfirst build {took:.1f} s: {durations(result)}")
    assert took <= FIRST_BUILD_LIMIT_S
    assert all(status in ("ok", "skipped") for _, status in steps(result).values()), result
    start = detail(result, "api")["api_start"]
    assert start["started"] is True and start["restarted"] is False
    assert start["variables"] == []
    registered = detail(result, "functions")["counts"]
    assert registered["actions"]["created"] == 1 and registered["loops"]["created"] == 1
    assert registered["functions"]["created"] == 5

    # The build never enables a loop; the executor is configured for the add-on.
    loops = get(twin, "/control/loops")["data"]
    assert [(loop["id"], loop["enabled"], loop["status"]) for loop in loops] == [
        ("l_inflow", False, "active")
    ]
    listed = get(twin, "/actions")
    assert listed["executor_configured"] is True
    assert [(a["id"], a["executable"]) for a in listed["data"]] == [("set_inflow", True)]

    # From now on the plant keeps delivering, a minute at a time.
    twin.extend()
    wait_for("both sources read up to the last minute", lambda: fresh(twin), FRESH_LIMIT_S)

    accepted = twin.ok("accept")
    assert accepted["passed"] is True and accepted["failed"] == [], accepted["failed"]
    ids = [entry["id"] for entry in accepted["checks"]]
    assert ids[-7:-3] == list(NEW_CHECKS)
    fidelity = check(accepted, "fidelity.state")["evidence"]
    assert fidelity["state"] in ("healthy", "unknown")
    if fidelity["state"] == "unknown":
        assert fidelity["warning"]
    schedule = check(accepted, "fidelity.schedule")["evidence"]
    assert set(schedule["expected"]) == {"sync", "fidelity", "reconcile", "function:f_quarter"}
    assert schedule["missing"] == [] and schedule["unexpected"] == []
    registry = check(accepted, "actions.registered")["evidence"]
    assert registry["executor_configured"] is True and registry["problems"] == {}
    notices = check(accepted, "notifications.configured")["evidence"]
    assert notices["kind"] == notices["expected"] == "log"
    endpoints = check(accepted, "skill.endpoints")["evidence"]
    assert endpoints["tier"] == "live" and endpoints["unlisted"] == []
    took_ms = sum(check(accepted, ident)["duration_ms"] for ident in NEW_CHECKS)
    print(f"the four new checks in accept: {took_ms / 1000:.2f} s")
    assert took_ms <= CHECKS_LIMIT_S * 1000

    # The skill of a twin that checks itself and acts, with both recipe files.
    skill = next((twin.root / "skill").glob("*"))
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "## Acting on the real system" in body and "## Control loops" in body
    assert "`c_inflow`" in body and "never work around a blocked action" in body.lower()
    assert (skill / "reference" / "fidelity.md").is_file()
    assert (skill / "reference" / "remodel.md").is_file()


def test_the_four_new_checks_take_at_most_three_seconds(twin: M6Twin) -> None:
    times = []
    with checks_context(twin) as ctx:
        for _ in range(RUNS):
            started = time.perf_counter()
            found = {
                "fidelity.state": fidelity_state(ctx),
                "fidelity.schedule": fidelity_schedule(ctx),
                "actions.registered": actions_registered(ctx),
                "notifications.configured": notifications_configured(ctx),
            }
            times.append(time.perf_counter() - started)
            assert all(result.passed for result in found.values()), {
                ident: result.evidence for ident, result in found.items() if not result.passed
            }
    print(f"\nthe four new checks: {', '.join(f'{t:.2f} s' for t in times)}")
    assert max(times) <= CHECKS_LIMIT_S


# -- rebuilds -------------------------------------------------------------------------


def test_an_unchanged_rebuild_registers_nothing_and_leaves_the_api(twin: M6Twin) -> None:
    before = twin.api_container()
    result, took = built(twin)
    print(f"\nunchanged rebuild {took:.1f} s: {durations(result)}")
    assert detail(result, "functions")["changed_anything"] is False
    assert detail(result, "api")["api_start"]["restarted"] is False
    assert detail(result, "models")["evaluated"] == []
    assert steps(result)["load"] == ("reused", "ok")
    assert twin.api_container() == before


def test_a_changed_action_is_registered_and_nothing_else_is_redone(twin: M6Twin) -> None:
    before = twin.api_container()
    times = []
    for run in range(RUNS):

        def described(data: dict[str, Any], run: int = run) -> None:
            data["actions"]["set_inflow"]["description"] = f"Set the inflow, revision {run + 1}"

        twin.change(described, sign=False)
        result, took = built(twin)
        times.append(took)
        print(f"\nrebuild with one action changed {took:.1f} s: {durations(result)}")
        counts = detail(result, "functions")["counts"]
        assert counts["actions"]["changed"] == 1, counts
        others = {
            section: found
            for section, found in counts.items()
            if section != "actions" and (found["created"] or found["changed"])
        }
        assert others == {}, counts
        assert detail(result, "models")["evaluated"] == []
        assert detail(result, "api")["api_start"]["restarted"] is False
    print(f"rebuilds with one action changed: {', '.join(f'{t:.1f} s' for t in times)}")
    assert max(times) <= ACTION_REBUILD_LIMIT_S
    assert twin.api_container() == before
    action = get(twin, "/actions/set_inflow")["data"]
    assert action["description"] == f"Set the inflow, revision {RUNS}"


def test_a_changed_settle_needs_no_new_signature(twin: M6Twin) -> None:
    def settle(data: dict[str, Any]) -> None:
        data["actions"]["set_inflow"]["settle"] = "PT6S"

    twin.change(settle, sign=False)
    result, _ = built(twin)
    assert detail(result, "functions")["counts"]["actions"]["changed"] == 1
    assert get(twin, "/actions/set_inflow")["data"]["settle"] == "PT6S"


def test_a_variable_missing_from_env_stops_the_build_and_names_it(twin: M6Twin) -> None:
    before = twin.api_container()
    previous = api_start_recorded(twin)

    def keyed(data: dict[str, Any]) -> None:
        executor = data["actions"]["set_inflow"]["executor"]
        executor["env"] = [PLANT_KEY]
        executor["headers"] = {"X-Plant-Key": f"${{{PLANT_KEY}}}"}
        data["env"][PLANT_KEY] = {"description": "Key of the plant's control interface"}

    twin.change(keyed, sign=False)
    error = twin.failed("build.failed", "build")
    assert error["detail"]["step"] == "scaffold" and PLANT_KEY in error["message"]
    assert error["detail"]["evidence"]["missing"] == [PLANT_KEY]
    assert twin.api_container() == before, "a container was started anew"
    code, text = twin.text("build")
    assert code == 1 and PLANT_KEY in text

    twin.set_env(PLANT_KEY, "plant-key-" + "7" * 16)
    result, _ = built(twin)
    start = detail(result, "api")["api_start"]
    assert start["restarted"] is True and start["variables"] == [PLANT_KEY]
    assert start["start_config"] != previous
    assert twin.api_container() != before
    assert detail(result, "functions")["counts"]["actions"]["changed"] == 1


def test_a_changed_bound_stops_the_build_until_it_is_signed(twin: M6Twin) -> None:
    data = twin.dta.read_manifest()
    data["contract"]["controls"]["c_inflow"]["bounds"]["maximum"] = 12
    twin.dta.write_manifest(data)
    twin.ok("manifest", "validate")
    error = twin.failed("build.not_ready", "build")
    assert [gate["stage"] for gate in error["detail"]["gates"]] == [5]

    twin.change(lambda data: None, sign=True)
    result, _ = built(twin)
    assert detail(result, "functions")["counts"]["actions"]["changed"] == 1
    assert detail(result, "api")["api_start"]["restarted"] is False
    assert get(twin, "/actions/set_inflow")["data"]["bounds"]["maximum"] == 12


def test_a_webhook_starts_the_api_anew_and_the_build_says_so(twin: M6Twin) -> None:
    before = twin.api_container()
    previous = api_start_recorded(twin)
    variant = twin.dta.read_manifest()
    variant["contract"]["notification"] = {"kind": "webhook", "webhook_env": ALERT_URL}
    # The API notifies by log until it is started for the webhook.
    found = judged(twin, notifications_configured, variant)
    assert not found.passed and found.evidence["kind"] == "log"

    def webhook(data: dict[str, Any]) -> None:
        data["contract"]["notification"] = {"kind": "webhook", "webhook_env": ALERT_URL}
        data["env"][ALERT_URL] = {"description": "Where the twin's notifications are posted"}

    twin.change(webhook, sign=True)
    twin.set_env(ALERT_URL, f"{PLANT_URL}{HOOK_PATH}")
    result, _ = built(twin)
    start = detail(result, "api")["api_start"]
    assert start["restarted"] is True and start["start_config"] != previous
    assert set(start["variables"]) == {PLANT_KEY, ALERT_URL}
    assert twin.api_container() != before
    assert twin.ok("notifications")["kind"] == "webhook"
    code, text = twin.text("notifications")
    assert code == 0 and "Kind: webhook" in text


# -- the commands -----------------------------------------------------------------------


def test_a_proposal_is_executed_at_the_plant_once_approved(twin: M6Twin) -> None:
    assert twin.ok("actions", "list")["executor_configured"] is True
    code, text = twin.text("actions", "list")
    assert code == 0 and "set_inflow" in text and "Executor configured: yes" in text

    proposed = proposal(twin, "site:north", 5, "--reason", "the test asks for more water")
    assert proposed["status"] == "proposed" and proposed["source"] == "test-agent"
    assert twin.standin.received(CONTROLS_PATH) == []

    approved = twin.ok("actions", "approve", proposed["uid"], "--by", OPERATOR, "--note", "agreed")
    record = approved["data"]
    assert record["status"] == "executed", record
    assert record["approval"]["by"] == OPERATOR
    calls = twin.standin.received(CONTROLS_PATH)
    assert len(calls) == 1
    call = calls[0]
    assert call["path"] == f"{CONTROLS_PATH}/north" and call["body"] == {"setpoint": 5}
    assert call["headers"]["idempotency-key"] == record["id"]
    assert call["headers"]["x-plant-key"] == read_env(twin.env_file)[PLANT_KEY]

    # The same key and proposal: the record the key made, nothing made again.
    first = proposal(twin, "site:south", 1, "--key", "k-south")
    again = proposal(twin, "site:south", 1, "--key", "k-south")
    assert first["uid"] == again["uid"] and first["status"] == "proposed"

    rejected = twin.ok("actions", "reject", first["uid"], "--by", OPERATOR, "--note", "not now")
    assert rejected["data"]["status"] == "rejected"
    error = twin.failed("action.not_pending", "actions", "approve", first["uid"], "--by", OPERATOR)
    assert error["detail"]["status"] == 409

    # Outside the bounds: refused with every reason, and recorded.
    refused = twin.failed(
        "action.rejected",
        "actions",
        "propose",
        "set_inflow",
        "--object",
        "site:north",
        "--param",
        "value=50",
    )
    assert refused["detail"]["status"] == 422 and refused["detail"]["errors"]
    assert refused["detail"]["record"].startswith("twin_action_record:")
    code, text = twin.text(
        "actions", "propose", "set_inflow", "--object", "site:north", "--param", "value=60"
    )
    assert code == 1 and "action.rejected" in text
    assert len(twin.standin.received(CONTROLS_PATH)) == 1

    records = twin.ok("actions", "records")["records"]
    statuses = {found["uid"]: found["status"] for found in records}
    assert statuses[record["uid"]] == "executed"
    assert statuses[refused["detail"]["record"]] == "rejected"
    executed = twin.ok("actions", "records", "--status", "executed")["records"]
    assert [found["uid"] for found in executed] == [record["uid"]]
    one = twin.ok("actions", "record", record["uid"])["data"]
    assert one["execution"]["http_status"] == 200
    code, text = twin.text("actions", "record", record["uid"])
    assert code == 0 and record["uid"] in text
    twin.failed("action.record_not_found", "actions", "record", "twin_action_record:nothing")


def test_an_effect_not_observed_opens_an_incident_and_fails_acceptance(twin: M6Twin) -> None:
    # The meter keeps reading about 2 at the north works, not the 5 that was set.
    def opened() -> list[dict[str, Any]]:
        twin.ok("actions", "reconcile")
        return unresolved(twin, "reconcile")

    incident = wait_for("a reconcile incident", opened, RECONCILE_LIMIT_S)[0]
    assert state(twin) == "correcting"
    reconciled = twin.ok("actions", "records", "--status", "executed")["records"][0]
    assert reconciled["reconciliation"] == "mismatch"

    error = twin.failed("accept.failed", "accept")
    assert "fidelity.state" in error["detail"]["failed"]
    failing = check(error["detail"], "fidelity.state")["evidence"]
    assert failing["state"] == "correcting" and incident["uid"] in failing["reason"]
    code, text = twin.text("fidelity", "incidents", "--status", "unresolved")
    assert code == 0 and incident["uid"] in text

    refused = twin.failed(
        "fidelity.bad_request", "fidelity", "retry", incident["uid"], "--by", OPERATOR
    )
    assert refused["detail"]["status"] == 400
    resolved = twin.ok(
        "fidelity", "resolve", incident["uid"], "--by", OPERATOR, "--note", "checked on site"
    )
    assert resolved["data"]["status"] == "resolved"
    shown = twin.ok("fidelity", "incident", incident["uid"])["data"]
    assert shown["steps"][-1]["action"] == "resolve" and shown["steps"][-1]["by"] == OPERATOR
    assert unresolved(twin) == []
    assert state(twin) == "healthy"
    twin.failed("fidelity.conflict", "fidelity", "resolve", incident["uid"], "--by", OPERATOR)

    # The owner was told by the webhook.
    types = {hook["body"]["type"] for hook in twin.standin.received(HOOK_PATH)}
    assert {
        "twin.action.approval_pending",
        "twin.action.mismatch",
        "twin.fidelity.incident",
    } <= types, types
    listed = twin.ok("notifications")["data"]
    assert any(notice["status"] == "delivered" for notice in listed)


def test_a_loop_is_enabled_by_hand_and_run(twin: M6Twin) -> None:
    listed = twin.ok("control", "list")["data"]
    assert [(loop["id"], loop["enabled"]) for loop in listed] == [("l_inflow", False)]
    refused = twin.failed("action.loop_disabled", "control", "run", "l_inflow")
    assert refused["detail"]["status"] == 409

    enabled = twin.ok("control", "enable", "l_inflow", "--by", OPERATOR)["data"]
    assert enabled["enabled"] is True
    entries = {entry["id"] for entry in get(twin, "/lifecycle/schedule")["entries"]}
    assert "loop:l_inflow" in entries
    with checks_context(twin) as ctx:
        assert fidelity_schedule(ctx).passed

    ran = twin.ok("control", "run", "l_inflow")
    run = ran["run"]
    assert ran["job"]["state"] == "succeeded" and run["status"] == "succeeded", run
    assert run["proposed"] == 1 and run["awaiting"] == 1 and run["executed"] == 0
    made = run["records"][0]
    assert made["action"] == "set_inflow" and made["status"] == "proposed"
    assert twin.ok("actions", "record", made["uid"])["data"]["source"].endswith("l_inflow")
    twin.ok("actions", "reject", made["uid"], "--by", OPERATOR)

    disabled = twin.ok("control", "disable", "l_inflow", "--by", OPERATOR)["data"]
    assert disabled["enabled"] is False
    code, text = twin.text("control", "list")
    assert code == 0 and "l_inflow" in text
    twin.failed("action.loop_not_found", "control", "enable", "l_nowhere", "--by", OPERATOR)


def test_each_new_check_fails_for_its_own_reason(twin: M6Twin) -> None:
    manifest = twin.dta.read_manifest()

    # A loop the manifest no longer declares while the twin still has it active.
    without_loop = copy.deepcopy(manifest)
    without_loop.pop("control_loops")
    found = judged(twin, actions_registered, without_loop)
    assert not found.passed and list(found.evidence["problems"]) == ["orphaned"]
    assert "l_inflow" in found.evidence["reason"]

    # A function the schedule runs that the manifest no longer triggers on an interval.
    on_request = copy.deepcopy(manifest)
    on_request["functions"]["f_quarter"].pop("trigger")
    on_request["functions"]["f_quarter"].pop("interval")
    found = judged(twin, fidelity_schedule, on_request)
    assert not found.passed and found.evidence["unexpected"] == ["function:f_quarter"]

    # The manifest asks for the log while the API posts to the webhook.
    logged = copy.deepcopy(manifest)
    logged["contract"]["notification"] = {"kind": "log"}
    found = judged(twin, notifications_configured, logged)
    assert not found.passed and found.evidence["kind"] == "webhook"


# -- incidents the twin finds by itself ------------------------------------------------


def test_a_stuck_sensor_and_a_stale_question_are_found_and_put_right(twin: M6Twin) -> None:
    # The forecast of the inflow may rest on data two minutes old, no more: a contract
    # change.
    def strict(data: dict[str, Any]) -> None:
        data["contract"]["questions"]["q_flow"]["staleness"] = "PT2M"

    twin.change(strict, sign=True)
    built(twin)
    assert state(twin) == "healthy"
    # Two syncs on from the rebuild before the meter stops, so that its last minutes
    # are read as on any other day.
    after = twin.at(twin.minute() + 2)

    def read_on() -> bool:
        marks = [source["watermark"] for source in get(twin, "/sync/sources")["data"]]
        return all(mark and datetime.fromisoformat(mark) >= after for mark in marks)

    wait_for("two syncs after the rebuild", read_on, FRESH_LIMIT_S, 3.0)

    frozen_from = twin.freeze()
    started = time.monotonic()
    print(f"\ns1 frozen at {FROZEN} from {twin.at(frozen_from).isoformat()}; the meter stops")

    # Stale at three checks in a row (the retry budget), the stale incident fails, and
    # with it the twin; s1 is found stuck after three intervals without a change.
    def both() -> dict[str, dict[str, Any]] | None:
        found = {incident["kind"]: incident for incident in unresolved(twin)}
        stale, signal = found.get("stale"), found.get("signal")
        ready = stale is not None and stale["status"] == "failed" and signal is not None
        return found if ready else None

    def seen() -> dict[str, Any]:
        status = twin.ok("fidelity", "status")
        sources = get(twin, "/sync/sources")["data"]
        return {
            "state": status["state"],
            "incidents": status.get("incidents"),
            "questions": status.get("questions"),
            "watermarks": {source["id"]: source["watermark"] for source in sources},
            "checks": [
                {key: found.get(key) for key in ("uid", "at", "state")}
                for found in twin.ok("fidelity", "checks", "--limit", "3")["checks"]
            ],
        }

    incidents = wait_for("a failed stale and a signal incident", both, INCIDENT_LIMIT_S, 10.0, seen)
    print(f"both incidents after {time.monotonic() - started:.0f} s")
    stale, signal_incident = incidents["stale"], incidents["signal"]
    assert stale["subject"] == "q_flow"
    assert state(twin) == "failed"
    code, text = twin.text("fidelity", "status")
    assert code == 0 and "failed" in text

    signals = twin.ok("fidelity", "signals", "--status", "suspect")["signals"]
    flagged = [(s["object"], s["reason"]) for s in signals]
    if flagged != [("sensor:s1", "stuck")]:
        for s in signals:
            print({key: s.get(key) for key in ("object", "since", "value_at_flag", "detail")})
        runs = get(twin, "/sync/runs", limit=20)["data"]
        for run in runs:
            print(
                {
                    key: run.get(key)
                    for key in (
                        "uid",
                        "kind",
                        "started_at",
                        "finished_at",
                        "clock_at",
                        "rows_written",
                        "rows_late",
                    )
                }
            )
        print(twin.text("sync", "status")[1])
    assert flagged == [("sensor:s1", "stuck")]
    suspect = signals[0]
    assert suspect["uid"] == signal_incident["subject"] and suspect["value_at_flag"] == FROZEN

    # Acceptance names both while they are not resolved.
    with checks_context(twin) as ctx:
        found = fidelity_state(ctx)
    assert not found.passed and found.evidence["state"] == "failed"
    assert stale["uid"] in found.evidence["reason"]
    assert signal_incident["uid"] in found.evidence["reason"]

    # A failed twin executes nothing: an approval is blocked and nothing is sent.
    sent = len(twin.standin.received(CONTROLS_PATH))
    waiting = proposal(twin, "site:south", 2)
    blocked = twin.ok("actions", "approve", waiting["uid"], "--by", OPERATOR)["data"]
    assert blocked["status"] == "blocked" and blocked["reasons"]
    assert len(twin.standin.received(CONTROLS_PATH)) == sent

    # The meter is mended: the stale incident is put back to its start; a signal
    # incident is not retried.
    retried = twin.ok("fidelity", "retry", stale["uid"], "--by", OPERATOR, "--note", "meter back")
    assert retried["data"]["status"] == "open"
    assert retried["data"]["steps"][-1]["action"] == "retry"
    twin.restore()
    assert state(twin) == "correcting"
    refused = twin.failed(
        "fidelity.bad_request", "fidelity", "retry", signal_incident["uid"], "--by", OPERATOR
    )
    assert refused["detail"]["status"] == 400
    twin.failed("fidelity.not_found", "fidelity", "clear", "twin_signal:nothing", "--by", OPERATOR)

    # s1 changes again; it is cleared by hand once its readings show it. The sync that
    # read the changed reading withheld it, so the check of the same boundary still finds
    # s1 unchanged: the clear comes once that check is done, before the next sync writes
    # the readings again (and before three sound checks would clear it by themselves).
    def changing() -> dict[str, Any] | None:
        rows = twin.ok("fidelity", "signals", "--status", "suspect")["signals"]
        changed = rows and rows[0]["reading"] is not None and rows[0]["reading"] != FROZEN
        return rows[0] if changed else None

    row = wait_for("a reading of s1 that changed", changing, READING_LIMIT_S, 3.0)
    boundary = datetime.fromisoformat(row["reading_at"]) + timedelta(minutes=1)

    def checked_after() -> bool:
        newest = twin.ok("fidelity", "checks", "--limit", "1")["checks"]
        return bool(newest) and datetime.fromisoformat(newest[0]["at"]) >= boundary

    wait_for("the check of the boundary that read it", checked_after, CHECK_LIMIT_S, 2.0)
    cleared = twin.ok("fidelity", "clear", suspect["uid"], "--by", OPERATOR, "--note", "replaced")
    assert cleared["data"]["status"] == "ok"
    twin.failed("fidelity.conflict", "fidelity", "clear", suspect["uid"], "--by", OPERATOR)
    shown = twin.ok("fidelity", "incident", signal_incident["uid"])["data"]
    assert shown["status"] == "resolved"

    wait_for("the twin healthy again", lambda: state(twin) == "healthy", HEALTHY_LIMIT_S, 10.0)
    stale_now = twin.ok("fidelity", "incident", stale["uid"])["data"]
    assert stale_now["status"] == "resolved"

    # The next check finds it healthy as well: s1 is not found stuck again.
    last = twin.ok("fidelity", "checks", "--limit", "1")["checks"][0]["at"]

    def next_check() -> dict[str, Any] | None:
        newest = twin.ok("fidelity", "checks", "--limit", "5")["checks"]
        return newest[0] if newest and newest[0]["at"] != last else None

    assert wait_for("the next check", next_check, CHECK_LIMIT_S, 5.0)["state"] == "healthy"
    assert unresolved(twin) == [] and state(twin) == "healthy"

    # What the checks measured, and the errors of the forecast question.
    errors = twin.ok("fidelity", "errors", "q_ahead")
    assert errors["question"] == "q_ahead" and errors["errors"]
    checked = twin.ok("fidelity", "check")
    assert checked["job"]["state"] == "succeeded" and checked["fidelity"]["state"] == "healthy"


# -- without the executors add-on --------------------------------------------------------


def test_without_the_add_on_the_twin_executes_nothing(twin: M6Twin) -> None:
    def advisory(data: dict[str, Any]) -> None:
        data["contract"]["addons"] = {"executors": False}
        data["contract"]["controls"]["c_inflow"]["autonomy"] = "advisory"
        action = data["actions"]["set_inflow"]
        action["binding"] = "advisory"
        action.pop("executor")
        data["env"].pop(PLANT_KEY)

    twin.change(advisory, sign=True)
    result, _ = built(twin)
    start = detail(result, "api")["api_start"]
    assert start["restarted"] is True and start["variables"] == [ALERT_URL]
    assert get(twin, "/actions")["executor_configured"] is False
    accepted = twin.ok("accept")
    assert accepted["passed"] is True, accepted["failed"]
    assert "reconcile" not in check(accepted, "fidelity.schedule")["evidence"]["expected"]

    # A manifest that binds the action to an executor wants one the API lacks.
    bound = twin.dta.read_manifest()
    bound["contract"]["addons"] = {"executors": True}
    bound["actions"]["set_inflow"]["binding"] = "executor"
    bound["actions"]["set_inflow"]["executor"] = {
        "kind": "http",
        "url": f"{PLANT_URL}{CONTROLS_PATH}/{{id}}",
        "body": {"setpoint": "{value}"},
    }
    found = judged(twin, actions_registered, bound)
    assert not found.passed and list(found.evidence["problems"]) == ["executor"]
    assert "none configured" in found.evidence["reason"]


def test_the_skill_recipes_run_as_written(twin: M6Twin, monkeypatch: pytest.MonkeyPatch) -> None:
    env = read_env(twin.env_file)
    monkeypatch.setenv("DTA_BASE_URL", env["DTA_BASE_URL"])
    monkeypatch.setenv("DTA_API_TOKEN", env["DTA_API_TOKEN"])
    skill = next((twin.root / "skill").glob("*"))
    recipes = m5_recipes.sections((skill / "reference" / "recipes.md").read_text("utf-8"))
    fidelity = (skill / "reference" / "fidelity.md").read_text("utf-8")
    propose = m5_recipes.python_of(recipes["Propose an action"])[0]
    for old, new in {
        "<action>": "set_inflow",
        '"<uid>"': '"site:north"',
        '"<number>"': "4",
        '"<why>"': '"the recipe asks"',
        '"<your name>"': '"recipe-agent"',
    }.items():
        assert old in propose, old
        propose = propose.replace(old, new)
    assert '"<' not in propose and "/<" not in propose, propose
    codes = [
        ("Setup", m5_recipes.python_of(recipes["Setup"])[0]),
        ("fidelity", m5_recipes.python_of(fidelity)[0]),
        ("Propose an action", propose),
    ]
    namespace: dict[str, Any] = {"__name__": "recipes"}
    for title, code in codes:
        exec(compile(code, f"<recipe {title}>", "exec"), namespace)  # noqa: S102
    # The proposal recipe reads the state the interlock judges by into `fidelity`.
    assert namespace["fidelity"] == "healthy"
    record = namespace["record"]
    assert record["status"] == "proposed" and record["source"] == "recipe-agent"

    # A correction by hand, on recent data, leaves the twin healthy.
    corrected = twin.ok("fidelity", "correct", "q_ahead", "--rung", "recalibrate")
    assert corrected["job"]["state"] == "succeeded" and corrected["job"]["kind"] == "correction"
    twin.failed("fidelity.not_found", "fidelity", "correct", "q_nowhere", "--rung", "recalibrate")

    logs = (twin.root / ".dta" / "logs" / "toolkit.log").read_text(encoding="utf-8")
    assert twin.leaked([logs]) == []
