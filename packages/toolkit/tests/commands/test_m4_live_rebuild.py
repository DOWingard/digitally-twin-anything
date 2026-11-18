"""Building a live twin again: its data is kept, its load guarded, its scheduler running."""

from __future__ import annotations

import m3tk_kit as kit
import m4_live as live

pytestmark = kit.needs_stack


def test_rebuilding_a_live_twin_keeps_its_data_and_its_scheduler(make_twin) -> None:
    twin = make_twin(live=True)
    twin.prepare()
    first = twin.ok("build")
    assert live.steps(first)["load"] == ("ran", "ok")
    assert live.steps(first)["sync"] == ("ran", "ok")

    # Again: install finds data written by sync, so the history is not loaded again.
    again = twin.ok("build")
    assert live.steps(again) == {
        "scaffold": ("ran", "ok"),
        "store": ("ran", "ok"),
        "install": ("ran", "ok"),
        "load": ("reused", "ok"),
        "api": ("ran", "ok"),
        "contract": ("ran", "ok"),
        "sync": ("ran", "ok"),
        **live.COMPUTE_STEPS,
        "ui": ("ran", "skipped"),
        "skill": ("ran", "ok"),
        **live.ADDON_STEPS,
    }
    load = next(entry for entry in again["steps"] if entry["step"] == "load")
    assert load["reason"] == live.LIVE_LOADED
    assert live.step_detail(again, "install")["loaded"] is True
    assert live.step_detail(again, "sync")["scheduler"] == {"state": "running"}
    assert live.services(twin)["scheduler"] == "running"
    assert twin.gates()["build"] == "passed"

    # From the load: the scheduler is stopped first, the load runs, the scheduler runs again.
    before = live.started_at(twin, "scheduler")
    from_load = twin.ok("build", "--from", "load")
    actions = live.steps(from_load)
    assert actions["load"] == ("ran", "ok") and actions["sync"] == ("ran", "ok")
    assert all(action == "ran" for action, _ in actions.values())
    writers = live.step_detail(from_load, "load")["writers"]
    assert writers["scheduler"] == "stopped" and writers["api"] == "answering"
    assert live.services(twin)["scheduler"] == "running"
    assert live.started_at(twin, "scheduler") > before, "the scheduler was not stopped"
    assert twin.gates()["build"] == "passed"

    # `down` removes the scheduler's container with the others; the data stays.
    down = twin.ok("down")
    assert down["volumes_removed"] is False
    assert kit.leftovers(twin.project)["containers"] == []
    # With the stack down, the sync commands say the API does not answer.
    error = twin.failed("api.unreachable", "sync", "status", exit_code=2)
    assert "did not get an answer" in error["message"]
    twin.failed("api.unreachable", "sync", "run", exit_code=2)

    up = twin.ok("up")
    states = {row["service"]: row["state"] for row in up["services"]}
    assert states == {"api": "running", "scheduler": "running", "store": "running"}

    # Without its data, a build loads the history again.
    twin.ok("down", "--destroy-data")
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}
    rebuilt = twin.ok("build")
    assert live.step_detail(rebuilt, "install")["loaded"] is False
    assert live.steps(rebuilt)["load"] == ("ran", "ok")
    reloaded = live.step_detail(rebuilt, "load")
    assert reloaded["versions_written"] > 0 and reloaded["writers"]["api"] == "not running"
    assert live.steps(rebuilt)["sync"] == ("ran", "ok")
    assert live.services(twin)["scheduler"] == "running"
    assert twin.gates()["build"] == "passed"

    twin.ok("down", "--destroy-data")
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}
    logs = (twin.root / ".dta" / "logs" / "toolkit.log").read_text(encoding="utf-8")
    assert twin.leaked([logs]) == []
