"""`dta-runtime install`: what it reports about the twin it brought in line."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings
from dta_runtime.cli import app
from dta_runtime.twin import Bundle, open_twin
from typer.testing import CliRunner

T0 = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _logging_kept() -> Iterator[None]:
    """The command sets logging up for a process of its own; later tests get theirs back."""
    root = logging.getLogger("dta")
    handlers, propagate, level = list(root.handlers), root.propagate, root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in handlers:
            handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.propagate = propagate
    root.setLevel(level)


def _install(settings: StoreSettings, bundle_file: Path, *extra: str) -> dict[str, Any]:
    env = {
        "DTA_STORE_HOST": settings.host,
        "DTA_STORE_PORT": str(settings.port),
        "DTA_STORE_USER": settings.user,
        "DTA_STORE_PASSWORD": settings.password,
        "DTA_STORE_DATABASE": settings.database,
    }
    result = CliRunner().invoke(app, ["install", "--bundle", str(bundle_file), *extra], env=env)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert result.exit_code == 0 and len(lines) == 1, result.output
    return json.loads(lines[0])


def test_install_says_whether_sync_has_written_anything_yet(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle, tmp_path: Path
):
    settings = new_settings()
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(water_bundle.to_json(), encoding="utf-8")

    first = _install(settings, bundle_file, "--t0", T0.isoformat())
    assert first["actions"] == ["installed", "registered"]
    assert first["loaded"] is False

    # What a sync run leaves behind: a version of its own, holding its record.
    twin = open_twin(settings)
    try:
        twin.store.commit(
            ChangeSet(
                upserts={
                    "TwinSyncRun": [
                        {
                            "uid": "twin_sync_run:first",
                            "kind": "backfill",
                            "status": "succeeded",
                            "started_at": T0,
                        }
                    ]
                },
                provenance=Provenance(source="sync", kind=FactKind.inferred, t_valid=T0),
            ),
            CommitMeta(cause="sync", actor="sync", t=T0, run="twin_sync_run:first"),
        )
    finally:
        twin.close()

    again = _install(settings, bundle_file)
    assert again["actions"] == []
    assert again["loaded"] is True
