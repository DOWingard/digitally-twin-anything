"""A live twin exported while it commits, imported, built, and kept in step by its scheduler.

Real containers, the image of this tree. A sync run is made to commit on the
live line between the first backup and the read of the heads after it: the
export has to back up again and name the heads of the second backup.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import m4_live as live
import portability_kit as port
from dta_toolkit.commands import portability, syncjobs
from dta_toolkit.stack import Stack

pytestmark = port.needs_stack
make_port_twin = port.make_port_twin
# Sync runs asked for until one of them commits on the live line.
COMMIT_TRIES = 3


def _commit_on_main(twin: port.PortTwin, stack: Stack, database: str) -> dict[str, str]:
    """Append readings and run a sync until the live line has a new head; old and new head."""
    before = portability.heads(stack, database)[portability.LIVE_BRANCH]
    for _ in range(COMMIT_TRIES):
        twin.append_readings(datetime.now(UTC) - timedelta(seconds=1))
        with port.api(twin.root) as client:
            started = syncjobs.start_run(client, syncjobs.run_body(None, None))
            job = syncjobs.wait_job(client, str(started.job["id"]))
        assert job["state"] == "succeeded", job
        after = portability.heads(stack, database)[portability.LIVE_BRANCH]
        if after != before:
            return {"before": before, "after": after}
    raise AssertionError(f"{COMMIT_TRIES} sync runs made no commit on the live line")


def test_live_twin_exported_while_it_commits(make_port_twin, tmp_path: Path, monkeypatch) -> None:
    twin = make_port_twin(live=True)
    twin.prepare()
    built = twin.ok("build")
    assert live.step_detail(built, "sync")["scheduler"] == {"state": "running"}

    # -- export, with a commit on the live line during the first backup ----------------
    original = portability._sync_backup
    committed: list[dict[str, str]] = []

    def sync_then_commit(stack: Stack, database: str, url: str) -> None:
        original(stack, database, url)
        if not committed:
            committed.append(_commit_on_main(twin, stack, database))

    monkeypatch.setattr(portability, "_sync_backup", sync_then_commit)
    out = tmp_path / "bundles"
    out.mkdir()
    started = time.perf_counter()
    exported = twin.ok("export", "--out", str(out))
    export_s = time.perf_counter() - started
    monkeypatch.undo()
    assert len(committed) == 1
    assert exported["backup_attempts"] >= 2
    head = exported["head"]
    assert head["id"] != committed[0]["before"]
    bundle = Path(exported["bundle"])
    document = port.descriptor(bundle)
    assert portability.LIVE_BRANCH in document["backup"]["attempts"][0]["moved"]
    assert document["branches"] == exported["branches"]
    assert exported["branches"][portability.LIVE_BRANCH] == head["id"]
    # The bundle names the heads its backup holds: the commit made during the export is in it.
    if head["id"] != committed[0]["after"]:
        # The scheduler committed again in between; the commit made here precedes the head.
        rows = port.sql(twin.root, f"SELECT `commit_hash` FROM dolt_log('{head['id']}')")
        assert committed[0]["after"] in {row["commit_hash"] for row in rows}
    assert port.leftover_backups(twin.root) == []

    twin.ok("down", "--destroy-data")

    # -- import: the store holds exactly the heads the bundle names ----------------------
    root = tmp_path / "imported"
    moved = twin.at(root)
    unpacked = moved.ok("import", str(bundle), "--into", str(root))
    assert unpacked["status"] == "unpacked"
    port.fill_env(root / ".env", twin.data)
    started = time.perf_counter()
    imported = moved.ok("import", str(bundle), "--into", str(root))
    import_s = time.perf_counter() - started
    assert imported["status"] == "verified"
    assert imported["head"] == head
    assert imported["branches"] == exported["branches"] == port.branches(root)
    services = {row["service"]: row["state"] for row in port.stack_of(root).services()}
    assert "scheduler" not in services or services["scheduler"] != "running", services

    # -- build on the new machine: its scheduler takes new readings again ------------------
    moved.ok("source", "probe")
    rebuilt = moved.ok("build")
    install = live.step_detail(rebuilt, "install")
    assert install["actions"] == [] and install["version"]["id"] == head["id"]
    synced = live.step_detail(rebuilt, "sync")
    assert synced["status"] == "succeeded" and synced["scheduler"] == {"state": "running"}

    with port.api(root) as client:
        known = {job["id"] for job in client.get("/api/v1/lifecycle/jobs").json()["data"]}
    stamp = twin.append_readings(datetime.now(UTC) - timedelta(seconds=1))
    waited_from = time.perf_counter()
    taken = None
    with port.api(root) as client:
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
    new = [job for job in jobs if job["id"] not in known]
    # Nothing but the scheduler asked for a job since: each carries the key of its schedule.
    keys = [str(job.get("idempotency_key")) for job in new]
    assert any(key.startswith("sync:") for key in keys), keys
    assert all(key.startswith(("sync:", "fidelity:")) for key in keys), keys
    assert health["head"]["seq"] > head["seq"], "the scheduler committed on the new machine"
    assert twin.leaked() == [] and moved.leaked() == []
    print(
        f"\nlive export {export_s:.1f} s with {exported['backup_attempts']} backups, "
        f"import {import_s:.1f} s, bundle {exported['size_bytes']} bytes, "
        f"scheduled pick-up after import {pickup_s:.1f} s, "
        f"attempts {json.dumps(document['backup']['attempts'])}"
    )
