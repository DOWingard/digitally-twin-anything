"""The parts of the fidelity, schedule, action and notification checks that need no twin."""

from __future__ import annotations

import copy
from typing import Any

import m3tk_kit as kit
import pytest
from dta_core.manifest import parse_manifest
from dta_toolkit.acceptance import Context
from dta_toolkit.acceptance.acting import (
    expected_entries,
    expected_kind,
    fidelity_state,
    registry_problems,
    unknown_verdict,
)
from dta_toolkit.acceptance.endpoints import required_endpoints

API = "/api/v1"


def _acting() -> dict[str, Any]:
    data = copy.deepcopy(kit.live_manifest("m3tk-00000000"))
    data["sync"]["interval"] = "PT1M"
    contract = data["contract"]
    contract["addons"] = {"executors": True}
    contract["controls"] = {
        "c_level": {
            "description": "Keep the level",
            "target": "Sensor.level",
            "bounds": {"minimum": 0, "maximum": 5},
            "autonomy": "supervised",
            "action": "set_level",
        }
    }
    data["actions"] = {
        "set_level": {
            "control": "c_level",
            "binding": "executor",
            "executor": {"kind": "http", "url": "http://plant/{id}", "env": []},
            "parameters": {"value": {"type": "number"}},
        }
    }
    data["functions"]["f_policy"] = {"kind": "registered", "entry": "plant:policy"}
    data["functions"]["f_tick"] = {
        "kind": "registered",
        "entry": "plant:tick",
        "trigger": "interval",
        "interval": "PT1H",
    }
    data["control_loops"] = {
        "l_level": {"controls": ["c_level"], "policy": "f_policy", "interval": "PT1H"}
    }
    return data


ACTION = {
    "id": "set_level",
    "control": "c_level",
    "binding": "executor",
    "autonomy": "supervised",
    "origin": "manifest",
    "status": "active",
}
LOOP = {"id": "l_level", "origin": "manifest", "status": "active", "enabled": False}


def test_a_twin_that_has_not_checked_yet_passes_only_while_a_check_can_come() -> None:
    assert unknown_verdict([], scheduled=True)[0] is True
    passed, said = unknown_verdict([{"id": "j1", "state": "running"}], scheduled=True)
    assert passed and "running" in said
    failed = {"id": "j2", "state": "failed", "error": {"code": "worker.unavailable"}}
    passed, said = unknown_verdict([failed, {"id": "j1", "state": "succeeded"}], scheduled=True)
    assert not passed and "worker.unavailable" in said
    passed, said = unknown_verdict([], scheduled=False)
    assert not passed and "schedule" in said


def test_the_schedule_holds_sync_fidelity_reconcile_enabled_loops_and_interval_functions() -> None:
    manifest = parse_manifest(_acting())
    expected = expected_entries(manifest, loops=[LOOP], actions=[ACTION], executor_configured=True)
    assert set(expected) == {"sync", "fidelity", "reconcile", "function:f_tick"}
    enabled = {**LOOP, "enabled": True}
    assert "loop:l_level" in expected_entries(
        manifest, loops=[enabled], actions=[ACTION], executor_configured=True
    )
    # Nothing is reconciled while the API makes no call.
    assert "reconcile" not in expected_entries(
        manifest, loops=[], actions=[ACTION], executor_configured=False
    )
    static = kit.manifest("m3tk-00000000")
    unsynced = parse_manifest(static)
    assert expected_entries(unsynced, loops=[], actions=[], executor_configured=False) == {}
    # A static twin that keeps a sync interval for its models' grid is synced on it.
    gridded = parse_manifest({**static, "sync": {"backfill_interval": "PT1H", "interval": "PT1H"}})
    assert expected_entries(gridded, loops=[], actions=[], executor_configured=False) == {
        "sync": "sync.interval"
    }


def test_the_registry_is_compared_with_the_manifest_for_its_own_reasons() -> None:
    manifest = parse_manifest(_acting())
    assert (
        registry_problems(manifest, actions=[ACTION], loops=[LOOP], executor_configured=True) == {}
    )
    # A loop the manifest no longer declares: orphaned while it is still active, history
    # once a registration disabled it.
    stray = {**LOOP, "id": "l_old"}
    found = registry_problems(
        manifest, actions=[ACTION], loops=[LOOP, stray], executor_configured=True
    )
    assert list(found) == ["orphaned"] and "l_old" in found["orphaned"][0]
    disabled = {**stray, "status": "disabled"}
    assert (
        registry_problems(
            manifest, actions=[ACTION], loops=[LOOP, disabled], executor_configured=True
        )
        == {}
    )
    # The executor: configured exactly when the add-on is on and an action needs one.
    found = registry_problems(manifest, actions=[ACTION], loops=[LOOP], executor_configured=False)
    assert list(found) == ["executor"]
    # Missing records, a disabled one, and a control its action does not carry.
    found = registry_problems(manifest, actions=[], loops=[], executor_configured=True)
    assert set(found) == {"unregistered", "control"}
    other = {**ACTION, "control": "c_other"}
    found = registry_problems(manifest, actions=[other], loops=[LOOP], executor_configured=True)
    assert list(found) == ["control"]
    autonomous = {**ACTION, "autonomy": "autonomous"}
    found = registry_problems(
        manifest, actions=[autonomous], loops=[LOOP], executor_configured=True
    )
    assert "supervised" in found["control"][0]


def test_the_notification_kind_is_the_manifests_or_log() -> None:
    assert expected_kind(parse_manifest(kit.manifest("m3tk-00000000"))) == "log"
    data = _acting()
    data["contract"]["notification"] = {"kind": "webhook", "webhook_env": "HOOK"}
    assert expected_kind(parse_manifest(data)) == "webhook"


def test_a_live_skill_must_list_the_new_routes_and_a_static_one_the_lifecycle_only() -> None:
    served = {
        ("GET", f"{API}/fidelity"),
        ("POST", f"{API}/fidelity/incidents/{{}}/retry"),
        ("GET", f"{API}/actions"),
        ("POST", f"{API}/control/loops/{{}}/run"),
        ("GET", f"{API}/lifecycle/schedule"),
        ("GET", f"{API}/lifecycle/notifications"),
        ("POST", f"{API}/lifecycle/register"),
        ("GET", f"{API}/lifecycle/jobs"),
        ("GET", f"{API}/objects/{{}}"),
    }
    live = required_endpoints(served, live=True)
    assert len(live) == 7
    assert ("GET", f"{API}/lifecycle/jobs") not in live
    static = required_endpoints(served, live=False)
    assert {path.rsplit("/", 1)[1] for _, path in static} == {
        "schedule",
        "notifications",
        "register",
    }


@pytest.mark.parametrize("tier", ["static", "live-without-fidelity"])
def test_fidelity_state_does_not_apply_without_checks(tier: str) -> None:
    data = kit.manifest("m3tk-00000000") if tier == "static" else _acting()
    if tier != "static":
        data.pop("fidelity")
    ctx = Context(api=None, manifest=parse_manifest(data), ontology=None)  # type: ignore[arg-type]
    result = fidelity_state(ctx)
    assert result.passed and result.evidence["applicable"] is False
