"""A static twin carried to a new workspace and a new store in one bundle, on real containers.

The twin is the command tests' small plant with a `.env` of marker values. It
is built and accepted, given an artifact and a second branch, exported while
it runs, removed with its data, imported into a new folder, built and
accepted there, and found at the very version it was exported at. The
refusals that need a store are made on the way.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import m3tk_kit as kit
import portability_kit as port
from dta_toolkit.commands import portability
from dta_toolkit.envfile import read_env

pytestmark = port.needs_stack
make_port_twin = port.make_port_twin

BLOB = b"a fitted state, as the API keeps it\n" * 64
SIDE = "m8exp-side"
NOT_CARRIED = {"access", "build", "acceptance", "handover"}


def _version_commit(root: Path, seq: int) -> str:
    for row in port.sql(root, "SELECT `commit_hash`, `message` FROM dolt_log"):
        message = row["message"]
        if message.startswith("{") and json.loads(message).get("seq") == seq:
            return row["commit_hash"]
    raise AssertionError(f"no version {seq} on the live line")


def test_static_twin_round_trip(make_port_twin, tmp_path: Path) -> None:
    twin = make_port_twin(marked=True)
    twin.prepare()
    built = twin.ok("build")
    assert all(entry["status"] in ("ok", "skipped") for entry in built["steps"])
    assert twin.ok("accept")["passed"] is True

    with port.api(twin.root) as client:
        kept = client.request(
            "POST",
            "/api/v1/artifacts",
            content=BLOB,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert kept.status_code == 201, kept.text
    blob_sha = kept.json()["sha256"]
    # A second branch, at an earlier version than the live line's head.
    port.sql(twin.root, f"CALL dolt_branch('{SIDE}', '{_version_commit(twin.root, 1)}')")
    source = port.branches(twin.root)
    assert set(source) == {"main", "_dta_build", SIDE} and source[SIDE] != source["main"]
    source_users = port.users(twin.root)
    source_artifacts = port.artifacts(twin.root)
    assert blob_sha in source_artifacts
    secrets = {
        name: value
        for name, value in port.env_values(twin.env_file).items()
        if len(value) >= port.SEARCHED_MIN
    }
    assert {"DTA_STORE_PASSWORD", "DTA_API_TOKEN", "DTA_BASE_URL", kit.DATA_ENV} <= set(secrets)
    assert all("m8exp-marker-" in secrets[name] for name in port.GENERATED + (kit.DATA_ENV,))

    # -- export, while the twin runs ---------------------------------------------------
    out = tmp_path / "bundles"
    out.mkdir()
    with port.opened_paths() as opened:
        started = time.perf_counter()
        exported = twin.ok("export", "--out", str(out))
        export_s = time.perf_counter() - started
    assert any(path.endswith("manifest.yaml") for path in opened), "the audit saw the export"
    assert [path for path in opened if Path(path).name == ".env"] == [], "export opened .env"
    assert export_s <= port.EXPORT_LIMIT_S, f"export took {export_s:.1f} s"
    bundle = Path(exported["bundle"])
    head = exported["head"]
    assert head["id"] == source["main"] and exported["branches"] == source
    assert bundle.name == f"twin-{twin.id}-{head['seq']}-{head['id'][:8]}.dtabundle"
    assert list(out.iterdir()) == [bundle]
    assert exported["size_bytes"] == bundle.stat().st_size
    assert exported["backup_attempts"] == 1
    assert [(note["code"], note["source"]) for note in exported["warnings"]] == [
        ("bundle.local_source", "plant")
    ]
    assert port.leftover_backups(twin.root) == []
    services = {row["service"]: row["state"] for row in port.stack_of(twin.root).services()}
    assert services["store"] == services["api"] == "running", "the twin kept running"
    text = twin.dta.run("export", "--out", str(tmp_path), json_output=False)
    assert text.exit_code == 0 and "warning: source plant reads a folder" in text.stdout

    contents = port.members(bundle)
    workspace_files = sorted(name for name in contents if name.startswith("workspace/"))
    assert workspace_files == [
        "workspace/.dta/build.json",
        "workspace/.dta/state.json",
        "workspace/.dta/summary.md",
        "workspace/.env.example",
        "workspace/.gitignore",
        "workspace/manifest.yaml",
        "workspace/ontology/plant.yaml",
        "workspace/reports/acceptance.json",
        "workspace/reports/acceptance.md",
    ]
    assert any(name.startswith("store/") for name in contents)
    assert f"artifacts/blobs/{blob_sha[:2]}/{blob_sha}" in contents
    document = json.loads(contents["bundle.json"])
    assert document["format"] == 1 and document["twin"]["tier"] == "static"
    assert document["head"]["seq"] == head["seq"]
    assert document["database"] == "twin_" + twin.id.replace("-", "_")
    assert document["versions"]["store"]["dolt"] and document["versions"]["toolkit"]
    carried = json.loads(contents["workspace/.dta/state.json"])
    assert {"design", "gaps"} <= set(carried["gates"]) and not set(carried["gates"]) & NOT_CARRIED
    assert carried["signatures"] and carried["installed_skill"] is None

    # No value of .env is in the bundle: not in its bytes, not in any member.
    raw = bundle.read_bytes()
    assert [name for name, value in secrets.items() if value.encode() in raw] == []
    for member, data in contents.items():
        assert [name for name, value in secrets.items() if value.encode() in data] == [], member

    # -- refused while this machine still has the twin's store ----------------------------
    elsewhere = tmp_path / "elsewhere"
    error = twin.at(elsewhere).failed(
        portability.VOLUME_EXISTS, "import", str(bundle), "--into", str(elsewhere), exit_code=2
    )
    assert "--replace-data" in error["message"] and twin.project in error["message"]
    assert not elsewhere.exists()

    twin.ok("down", "--destroy-data")
    assert port.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}

    # -- import: unpack, fill in .env, restore and verify -------------------------------
    root = tmp_path / "imported"
    moved = twin.at(root)
    started = time.perf_counter()
    first = moved.ok("import", str(bundle), "--into", str(root))
    unpack_s = time.perf_counter() - started
    assert first["status"] == "unpacked"
    assert set(first["to_fill"]) == {"DTA_API_PORT", "DTA_BASE_URL", kit.DATA_ENV}
    assert first["next"] == f"dta import {bundle} --into {root}"
    assert port.leftovers(twin.project)["containers"] == [], "nothing started before .env is filled"
    created = read_env(root / ".env")
    for name in port.GENERATED:
        assert created[name] and created[name] != secrets[name], name
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    port.fill_env(root / ".env", twin.data)
    started = time.perf_counter()
    second = moved.ok("import", str(bundle), "--into", str(root))
    restore_s = time.perf_counter() - started
    import_s = unpack_s + restore_s
    assert import_s <= port.IMPORT_LIMIT_S, f"import took {import_s:.1f} s"
    assert second["status"] == "verified" and second["head"]["id"] == head["id"]
    assert second["branches"] == source == port.branches(root)
    assert second["artifacts"] >= 1
    assert port.users(root) == source_users, "the restored store has the exported one's users"
    services = {row["service"]: row["state"] for row in port.stack_of(root).services()}
    assert services["store"] == "running" and services["api"] == "created", services
    assert port.leftover_backups(root) == []
    record = json.loads((root / ".dta" / "import.json").read_text(encoding="utf-8"))
    assert record["status"] == "verified" and record["head_verified"] is True
    assert record["imported_from"]["bundle"] == bundle.name
    assert record["branches"] == source
    gates = moved.gates()
    assert {name for name in NOT_CARRIED if gates[name] == "passed"} == set()
    assert gates["design"] == gates["gaps"] == "passed", gates
    # The restored store holds no value of the old .env either.
    tables = port.tables_text(root)
    assert [name for name, value in secrets.items() if value in tables] == []

    moved.failed(portability.TARGET_EXISTS, "import", str(bundle), "--into", str(root), exit_code=2)

    # -- build and accept on the new machine: nothing to install, no commit ----------------
    moved.ok("source", "probe")
    rebuilt = moved.ok("build")
    steps = {entry["step"]: entry for entry in rebuilt["steps"]}
    install = steps["install"]["detail"]
    assert install["actions"] == [] and install["version"]["id"] == head["id"]
    load = steps["load"]
    assert load["status"] == "ok"
    assert load["action"] == "reused" or load["detail"]["versions_written"] == 0, load
    assert moved.ok("accept")["passed"] is True
    assert port.branches(root) == source, "build and accept added no commit"
    assert port.artifacts(root) == source_artifacts
    with port.api(root) as client:
        fetched = client.get(f"/api/v1/artifacts/{blob_sha}")
        health = client.get("/api/v1/meta/health").json()
    assert fetched.status_code == 200 and fetched.content == BLOB
    assert health["head"]["id"] == head["id"] and health["head"]["seq"] == head["seq"]

    # -- export refuses a stopped store ----------------------------------------------------
    moved.ok("down")
    moved.failed(portability.STORE_NOT_RUNNING, "export", "--out", str(out), exit_code=2)
    assert list(out.iterdir()) == [bundle]

    # -- a store that does not hold what the bundle names is refused, naming the branch ------
    claimed = source["main"]

    def claim(document: dict) -> None:
        document["branches"][SIDE] = claimed

    tampered = port.with_descriptor(bundle, tmp_path / "tampered.dtabundle", claim)
    again = tmp_path / "again"
    other = twin.at(again)
    other.ok("import", str(tampered), "--into", str(again), "--replace-data")
    port.fill_env(again / ".env", twin.data)
    error = other.failed(
        portability.HEAD_MISMATCH,
        "import",
        str(tampered),
        "--into",
        str(again),
        "--replace-data",
        exit_code=1,
    )
    assert error["detail"]["branches"] == {SIDE: {"expected": claimed, "found": source[SIDE]}}
    for text in (SIDE, claimed, source[SIDE]):
        assert text in error["message"]
    failed = json.loads((again / ".dta" / "import.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"

    assert twin.leaked() == [], "a value of the first .env reached a command's output"
    assert moved.leaked() == [], "a value of the new .env reached a command's output"
    print(
        f"\nexport {export_s:.1f} s (limit {port.EXPORT_LIMIT_S:g}), import {import_s:.1f} s "
        f"(unpack {unpack_s:.1f} s, restore and verify {restore_s:.1f} s; limit "
        f"{port.IMPORT_LIMIT_S:g}), bundle {exported['size_bytes']} bytes, "
        f"timings {json.dumps(exported['timings_ms'])} / {json.dumps(second['timings_ms'])}"
    )
