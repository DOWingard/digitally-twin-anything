"""The whole way from a written manifest to a handed-over twin, on real containers.

The twin is small (two types, three CSV files, four hours of readings), so the
run is bounded; its limits are fixed here before any measurement.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path

import m3tk_kit as kit

pytestmark = kit.needs_stack

FLOW_LIMIT_S = 240.0
BUILD_LIMIT_S = 120.0

GATES = (
    "intake",
    "contract",
    "access",
    "profiling",
    "design",
    "gaps",
    "build",
    "acceptance",
    "handover",
)
CHECKS = {
    "api.contract",
    "readiness.R1",
    "readiness.R2",
    "readiness.R3",
    "readiness.R4",
    "readiness.R5",
    "data.constraints",
    "data.coverage",
    "ui.reachable",
    "skill.current",
    "skill.endpoints",
    "sync.scheduler",
    "sync.runs",
    "sync.sources",
    "sync.freshness",
    "questions.answered",
    "questions.error",
    "models.accepted",
    "compute.worker",
    "fidelity.state",
    "fidelity.schedule",
    "actions.registered",
    "notifications.configured",
    "sdk.usable",
    "mcp.reachable",
    "sql.readonly",
}


def expect(**results: str) -> dict[str, str]:
    """Every gate's expected result: the named ones as given, earlier ones passed, later unset."""
    last = max(GATES.index(name) for name in results)
    expected = {}
    for index, name in enumerate(GATES):
        default = "passed" if index <= last else "not_evaluated"
        expected[name] = results.get(name, default)
    return expected


def test_flow_from_manifest_to_handover(make_twin, tmp_path: Path, home: Path) -> None:
    started = time.perf_counter()
    twin = make_twin()

    twin.write()
    gates = twin.gates()
    assert gates["intake"] == "passed"
    assert gates["contract"] == "failed"  # nothing confirmed yet
    assert gates["access"] == "not_evaluated"

    checked = twin.ok("ontology", "check", "--write")
    # The report describes what was found: no signature yet, so the file's one was written.
    assert checked["written"] is True and checked["agree"] is False
    assert checked["manifest_signature"] is None
    assert sorted(entry["name"] for entry in checked["types"]["objects"]) == ["Sensor", "Site"]
    assert twin.dta.read_manifest()["ontology"]["type_signature"] == checked["file_signature"]

    template = twin.ok("env", "template")
    assert sorted(template["written"]) == [".env", ".env.example"]
    assert sorted(template["generated"]) == ["DTA_API_TOKEN", "DTA_STORE_PASSWORD"]
    assert set(template["to_fill"]) == {"DTA_API_PORT", "DTA_BASE_URL", kit.DATA_ENV}
    assert stat.S_IMODE(twin.env_file.stat().st_mode) == 0o600
    twin.fill_env()

    env = twin.ok("env", "check")
    assert env["missing"] == [] and env["warnings"] == []

    probe = twin.ok("source", "probe")
    assert probe["ok"] is True and probe["failing"] == [] and probe["gate_recorded"] is True
    datasets = {d["dataset"]: d["ok"] for d in probe["sources"][0]["datasets"]}
    assert datasets == {"sites": True, "sensors": True, "readings": True}
    assert twin.gates()["access"] == "passed"

    profiled = twin.ok("profile")
    assert sorted(profiled["profiles"]) == ["plant"] and profiled["failed"] == {}
    assert profiled["written"] == ["plant"]
    written = twin.dta.read_manifest()
    assert written["sources"]["plant"]["profile"] is not None
    assert written["field_status"]["sources.plant.profile"] == "derived"
    assert twin.gates()["profiling"] == "passed"

    twin.ok("manifest", "validate")
    assert twin.gates()["design"] == "passed"

    twin.ok("manifest", "confirm", "--all-user")
    gates = twin.gates()
    assert gates["contract"] == "passed"
    assert gates["design"] == "stale"  # confirming changed the manifest

    twin.ok("manifest", "validate")
    twin.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")
    assert twin.gates() == expect(gaps="passed")

    build_started = time.perf_counter()
    built = twin.ok("build")
    build_s = time.perf_counter() - build_started
    steps = {entry["step"]: (entry["action"], entry["status"]) for entry in built["steps"]}
    assert steps == {
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
    load = next(entry for entry in built["steps"] if entry["step"] == "load")["detail"]
    assert load["versions_written"] == 4 and load["rows_rejected"] == 0
    assert twin.gates() == expect(build="passed")
    assert build_s <= BUILD_LIMIT_S, f"build took {build_s:.1f} s"

    accepted = twin.ok("accept")
    assert accepted["passed"] is True and accepted["failed"] == []
    assert {check["id"] for check in accepted["checks"]} == CHECKS
    contract = next(check for check in accepted["checks"] if check["id"] == "api.contract")
    assert contract["evidence"]["operations"] > 0 and contract["evidence"]["failures"] == 0
    # Accepting a twin only reads it: no generated request goes to an operation that writes.
    exercised = [entry["operation"] for entry in contract["evidence"]["per_operation"]]
    assert exercised and all(operation.startswith("GET ") for operation in exercised)
    assert all(not name.startswith("GET ") for name in contract["evidence"]["not_exercised"])
    # A static twin does not sync; its skill lists only what the API serves.
    for check in accepted["checks"]:
        if check["id"].startswith("sync."):
            assert check["passed"] and check["evidence"]["applicable"] is False
    endpoints = next(check for check in accepted["checks"] if check["id"] == "skill.endpoints")
    assert endpoints["passed"] and endpoints["evidence"]["missing"] == []
    reports = twin.root / "reports"
    assert (reports / "acceptance.json").is_file() and (reports / "acceptance.md").is_file()
    assert not (reports / "deficit.json").exists() and not (reports / "deficit.md").exists()
    assert twin.gates() == expect(acceptance="passed")

    skill = twin.ok("generate", "skill")
    assert skill["name"] == f"twin-{twin.id}"
    assert skill["written"] == [] and skill["removed"] == []  # the build rendered it already
    assert twin.gates() == expect(acceptance="passed")

    target = tmp_path / "agent-skills"
    installed = twin.ok("install-skill", "--target", str(target))
    destination = target / skill["name"]
    assert installed["installed"] == str(destination)
    assert sorted(p.name for p in destination.iterdir()) == sorted(
        {Path(name).parts[0] for name in skill["files"]}
    )
    assert twin.dta.state()["installed_skill"] == str(destination)
    handover = (twin.root / "reports" / "handover.md").read_text(encoding="utf-8")
    assert twin.id in handover and "Nothing is provisional." in handover
    assert not any(home.iterdir()), "nothing may be written to HOME"
    assert twin.gates() == expect(handover="passed")

    again = twin.ok("build")
    actions = {entry["step"]: entry["action"] for entry in again["steps"]}
    # Every step checks what is there now; only the load is not repeated, because
    # install found the twin where the last build left it.
    assert actions == {
        "scaffold": "ran",
        "image": "ran",
        "store": "ran",
        "install": "ran",
        "load": "reused",
        "api": "ran",
        "contract": "ran",
        "sync": "ran",
        "code": "ran",
        "functions": "ran",
        "models": "ran",
        "questions": "ran",
        "ui": "ran",
        "mcp": "ran",
        "skill": "ran",
        "sdk": "ran",
    }
    assert twin.gates() == expect(handover="passed")

    up = twin.ok("up")
    services = {row["service"]: (row["state"], row["health"]) for row in up["services"]}
    assert services == {"store": ("running", "healthy"), "api": ("running", "healthy")}
    logs = twin.ok("logs", "api", "--tail", "50")
    assert logs["service"] == "api" and logs["logs"]

    down = twin.ok("down", "--destroy-data")
    assert down["volumes_removed"] is True and down["gates_reset"] == [6, 7]
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}
    # Without its data the twin is neither built nor accepted; the skill still describes it.
    assert twin.gates() == expect(build="failed", acceptance="failed", handover="passed")

    # A build on the emptied stack installs and loads again without being told to.
    rebuilt = twin.ok("build")
    actions = {entry["step"]: entry["action"] for entry in rebuilt["steps"]}
    assert actions["install"] == "ran" and actions["load"] == "ran"
    reloaded = next(entry for entry in rebuilt["steps"] if entry["step"] == "load")["detail"]
    assert reloaded["versions_written"] == 4
    assert twin.gates() == expect(build="passed", acceptance="failed", handover="passed")
    twin.ok("down", "--destroy-data")
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}

    flow_s = time.perf_counter() - started
    assert flow_s <= FLOW_LIMIT_S, f"the flow took {flow_s:.1f} s"

    written_files = [
        twin.root / ".dta" / "logs" / "toolkit.log",
        twin.root / ".dta" / "build.json",
        twin.root / ".dta" / "state.json",
        *sorted((twin.root / "reports").iterdir()),
        *sorted(p for p in (twin.root / "skill").rglob("*") if p.is_file()),
        *sorted(p for p in destination.rglob("*") if p.is_file()),
    ]
    texts = [path.read_text(encoding="utf-8") for path in written_files if path.is_file()]
    assert twin.leaked(texts) == [], "a value of .env reached the output, a log or a report"
    print(f"\nflow {flow_s:.1f} s (limit {FLOW_LIMIT_S:g}), build {build_s:.1f} s")


def test_twin_with_the_browser_interface_is_accepted(make_twin) -> None:
    twin = make_twin(ui=True)
    twin.prepare()
    built = twin.ok("build")
    assert next(e for e in built["steps"] if e["step"] == "ui")["status"] == "ok"
    accepted = twin.ok("accept", "--examples", "2")
    reachable = next(check for check in accepted["checks"] if check["id"] == "ui.reachable")
    assert reachable["passed"] is True and reachable["evidence"]["status"] == 200
