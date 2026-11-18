"""A small static twin from the sample workspace to handover, through the launcher alone.

Every command the toolkit's own flow test uses, up to `build`, `accept` and
`install-skill`, then `down --destroy-data`: run as the plugin runs them, by
`plugin/bin/dta` on the toolkit image, never by the toolkit in this
environment. Its limits are fixed here before any measurement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import launcher_kit as kit
from sample_twin import DATA_ENV, Twin, expect

pytestmark = [kit.needs_image, kit.needs_runtime]

FLOW_LIMIT_S = 300.0


def test_static_twin_to_handover_through_the_launcher(
    make_twin, tmp_path: Path, home: Path
) -> None:
    twin: Twin = make_twin()
    started = time.perf_counter()
    created = twin.ok("init", "--id", twin.id)
    assert created["created"] is True and created["root"] == str(twin.root)
    twin.write()
    assert twin.gates()["intake"] == "passed"

    checked = twin.ok("ontology", "check", "--write")
    assert checked["written"] is True
    template = twin.ok("env", "template")
    assert sorted(template["generated"]) == ["DTA_API_TOKEN", "DTA_STORE_PASSWORD"]
    assert set(template["to_fill"]) == {"DTA_API_PORT", "DTA_BASE_URL", DATA_ENV}
    twin.fill_env()
    assert twin.ok("env", "check")["missing"] == []

    # The sources are read from the twin's own containers, which the host's daemon
    # gives the data folder: the toolkit's container never needs it mounted.
    probe = twin.ok("source", "probe")
    assert probe["ok"] is True and probe["failing"] == []
    profiled = twin.ok("profile")
    assert profiled["written"] == ["plant"] and profiled["failed"] == {}
    twin.ok("manifest", "validate")
    twin.ok("manifest", "confirm", "--all-user")
    twin.ok("manifest", "validate")
    twin.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")
    assert twin.gates() == expect(gaps="passed")
    # A person at a terminal signs too: the launcher gives the container a terminal.
    code, shown = kit.on_terminal(
        ["manifest", "sign"],
        cwd=twin.root,
        env=twin.env,
        reply=(b"type the twin id", twin.id.encode() + b"\r"),
    )
    assert code == 0, shown[-2000:]
    state = json.loads((twin.root / ".dta" / "state.json").read_text(encoding="utf-8"))
    assert [entry["mode"] for entry in state["signatures"]] == ["non_interactive", "interactive"]
    assert twin.gates() == expect(gaps="passed")

    built, seen, container = twin.build_watched()
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

    # The toolkit ran docker compose from its container: none of it carried a secret.
    assert container is not None and seen
    secrets_in_env = kit.env_values(twin.root / ".env")
    assert secrets_in_env
    assert not [value for value in secrets_in_env if any(value in text for text in seen)]
    assert not [value for value in secrets_in_env if value in json.dumps(container)]
    assert any("docker compose" in text for text in seen), "the compose calls were not seen"

    accepted = twin.ok("accept")
    assert accepted["passed"] is True and accepted["failed"] == []
    assert twin.gates() == expect(acceptance="passed")

    skill = twin.ok("generate", "skill")
    assert skill["name"] == f"twin-{twin.id}" and skill["written"] == []

    target = tmp_path / "agent-skills"
    installed = twin.ok("install-skill", "--target", str(target))
    destination = target / skill["name"]
    assert installed["installed"] == str(destination)
    assert sorted(p.name for p in destination.iterdir()) == sorted(
        {Path(name).parts[0] for name in skill["files"]}
    )
    handover = (twin.root / "reports" / "handover.md").read_text(encoding="utf-8")
    assert twin.id in handover and "Nothing is provisional." in handover
    assert twin.gates() == expect(handover="passed")
    assert not (home / ".claude").exists(), "a --target install must not touch HOME"

    # The default, personal scope lands in the caller's HOME: here a temporary one.
    personal = twin.ok("install-skill")
    assert personal["installed"] == str(home / ".claude" / "skills" / skill["name"])
    assert (home / ".claude" / "skills" / skill["name"] / "SKILL.md").is_file()

    written = [
        *twin.root.rglob("*"),
        *target.rglob("*"),
        *(home / ".claude").rglob("*"),
    ]
    assert kit.owned_by_caller(written) == []

    down = twin.ok("down", "--destroy-data")
    assert down["volumes_removed"] is True
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}

    flow_s = time.perf_counter() - started
    texts = twin.outputs + [
        path.read_text(encoding="utf-8")
        for path in [
            twin.root / ".dta" / "logs" / "toolkit.log",
            *sorted((twin.root / "reports").iterdir()),
            *sorted(p for p in destination.rglob("*") if p.is_file()),
        ]
        if path.is_file()
    ]
    assert not [value for value in secrets_in_env if any(value in text for text in texts)]
    print(f"\nflow through the launcher {flow_s:.1f} s (limit {FLOW_LIMIT_S:g})")
    assert flow_s <= FLOW_LIMIT_S
