"""The runtime says which manifest versions it reads, and answers a newer manifest by saying
that it is the older side."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import dump_manifest
from dta_core.manifest.models import MANIFEST_VERSION
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings
from dta_runtime.cli import app
from dta_runtime.twin import MANIFEST_UID, Bundle, TwinError, install_twin
from typer.testing import CliRunner


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


def _invoke(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, list(args), env=env or {})
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    return result.exit_code, json.loads(lines[0])


def _newer(bundle: Bundle) -> str:
    """A bundle whose manifest is written in a version this runtime does not read yet."""
    manifest = yaml.safe_load(dump_manifest(bundle.manifest))
    manifest.update(manifest_version=MANIFEST_VERSION + 1, later={"field": True})
    return json.dumps({"manifest": yaml.safe_dump(manifest), "ontology": bundle.ontology_text})


def test_version_names_the_package_and_the_newest_manifest_version():
    code, document = _invoke("version")
    assert code == 0
    assert document["package"] == "dta-runtime"
    assert document["version"]
    assert document["manifest_version"] == MANIFEST_VERSION == 2


def test_install_answers_a_newer_manifest_as_a_runtime_too_old(
    water_bundle: Bundle, tmp_path: Path
):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(_newer(water_bundle), encoding="utf-8")
    # No store is reached: the bundle is refused as soon as it is read.
    code, document = _invoke("install", "--bundle", str(bundle_file))
    assert code != 0
    error = document["error"]
    assert error["code"] == "runtime.too_old"
    assert error["detail"] == {"manifest_version": 3, "runtime_reads": 2}
    assert "newer runtime image" in error["message"]


def test_probe_answers_a_newer_manifest_as_a_runtime_too_old(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"manifest_version": 3, "later": {}}), encoding="utf-8")
    code, document = _invoke("probe", "--manifest", str(path))
    assert code != 0
    assert document["error"]["code"] == "runtime.too_old"


def test_a_stored_manifest_newer_than_this_runtime_is_said_to_be_so(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    installed = install_twin(new_settings(), water_bundle)
    twin = installed.twin
    try:
        stored = yaml.safe_load(dump_manifest(water_bundle.manifest))
        stored["manifest_version"] = MANIFEST_VERSION + 1
        head = twin.store.head()
        twin.store.commit(
            ChangeSet(
                upserts={
                    "TwinManifest": [
                        {
                            "uid": MANIFEST_UID,
                            "content": yaml.safe_dump(stored),
                            "manifest_hash": "f" * 64,
                        }
                    ]
                },
                provenance=Provenance(source="manifest", kind=FactKind.assumed, t_valid=head.t),
            ),
            CommitMeta(cause="registration", actor="newer-runtime", t=head.t),
        )
        with pytest.raises(TwinError) as caught:
            twin.manifest()
        assert caught.value.code == "runtime.too_old"
    finally:
        twin.close()
