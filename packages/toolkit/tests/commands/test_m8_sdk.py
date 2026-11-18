"""The typed client package of the repository's sample twins: generated from their ontology
and from the API's own document, installed alone into a fresh environment."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import load_ontology
from dta_runtime.mcp import api_document
from dta_toolkit.sdk import ensure_sdk, marker_path, remove_sdk, render_sdk, sdk_dir
from dta_toolkit.workspace import Workspace
from m8_kit import install_into_fresh_venv, run_in

TESTBED = Path(__file__).parents[4] / "testbed"
ONTOLOGY = TESTBED / "twin" / "ontology" / "water_utility.yaml"
SAMPLES = {
    "water-static": TESTBED / "twin" / "manifest.yaml",
    "water-live": TESTBED / "twin-live" / "manifest.yaml",
}
GENERATION_LIMIT_S = 60.0
TOKEN = "sentinel-token-that-must-appear-nowhere-0123456789"

# Run in the fresh environment: what a user of the package can rely on without a twin.
INSPECT = r"""
import json, sys
package = __import__(sys.argv[1])
expected = json.loads(sys.argv[2])
report = {"types": sorted(package.TYPES), "questions": package.QUESTIONS}
try:
    package.Twin(environ={})
except package.TwinConfigError as exc:
    report["missing"] = exc.missing
twin = package.Twin(environ={"DTA_BASE_URL": "http://127.0.0.1:9", "DTA_API_TOKEN": "t0ken-x"})
report["repr"] = repr(twin)
report["accessors"] = {
    name: twin.objects(name).model is model and getattr(twin, attribute).type_name == name
    for name, model in package.TYPES.items()
    for attribute in [expected["attributes"][name]]
}
report["question_methods"] = [
    callable(getattr(twin.questions, method)) for method in expected["methods"]
]
model = next(iter(package.TYPES.values()))
try:
    model.model_validate({"uid": "x", "no_such_property": 1})
    report["strict"] = False
except Exception:
    report["strict"] = True
print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return api_document()


def _sample(name: str) -> Manifest:
    data = yaml.safe_load(SAMPLES[name].read_text(encoding="utf-8"))
    return parse_manifest(data, origin=str(SAMPLES[name]))


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_a_sample_twins_package_installs_alone_and_offers_each_type_and_question(
    name: str, spec: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _sample(name)
    ontology = load_ontology(ONTOLOGY)
    # Values in the environment never reach the generated files.
    monkeypatch.setenv("DTA_API_TOKEN", TOKEN)
    started = time.perf_counter()
    rendered = render_sdk(manifest, ontology, spec)
    elapsed = time.perf_counter() - started
    print(f"{name}: generated in {elapsed:.2f} s, {len(rendered.files)} files", rendered.timings_ms)
    assert elapsed <= GENERATION_LIMIT_S
    package_dir = tmp_path / "sdk"
    for relative, data in rendered.files.items():
        assert TOKEN.encode() not in data, relative
        target = package_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert rendered.package == "twin_" + manifest.twin.id.replace("-", "_")
    assert {"pyproject.toml", "README.md", f"{rendered.package}/py.typed"} <= set(rendered.files)

    python = install_into_fresh_venv(package_dir, tmp_path / "client")
    types = [t.name for t in ontology.domain_types()]
    readme = rendered.files["README.md"].decode("utf-8")
    twin_code = rendered.files[f"{rendered.package}/twin.py"].decode("utf-8")
    attributes = {
        t: line.split(":")[0].strip().removeprefix("self.")
        for t in types
        for line in twin_code.splitlines()
        if line.strip().startswith("self.") and f"Objects[types.{t}]" in line
    }
    questions = sorted((manifest.contract.questions or {}).keys())
    methods = [q.replace("-", "_") for q in questions]
    expected = {"attributes": attributes, "methods": methods}
    done = run_in(python, INSPECT, {}, rendered.package, json.dumps(expected))
    assert done.returncode == 0, done.stderr[-3000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["types"] == sorted(types)
    assert report["questions"] == questions
    assert report["missing"] == ["DTA_BASE_URL", "DTA_API_TOKEN"]
    assert "t0ken-x" not in report["repr"]
    assert all(report["accessors"].values()), report["accessors"]
    assert all(report["question_methods"]) and len(report["question_methods"]) == len(questions)
    assert report["strict"] is True
    for t in types:
        assert f"`twin.{attributes[t]}`" in readme, t


def test_the_package_is_generated_again_only_when_what_it_is_made_from_changed(
    spec: dict[str, Any], tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "ws")
    manifest = _sample("water-static")
    ontology_file = tmp_path / "water.yaml"
    ontology_file.write_text(ONTOLOGY.read_text(encoding="utf-8"), encoding="utf-8")

    first = ensure_sdk(workspace, manifest, ontology_file, spec)
    assert first["action"] == "generated" and first["removed"] == []
    assert ensure_sdk(workspace, manifest, ontology_file, spec)["action"] == "current"

    changed = json.loads(json.dumps(spec))
    changed["info"]["description"] = "another API"
    assert ensure_sdk(workspace, manifest, ontology_file, changed)["action"] == "generated"
    assert ensure_sdk(workspace, manifest, ontology_file, changed)["action"] == "current"

    ontology_file.write_text(
        ontology_file.read_text(encoding="utf-8") + "\n# a comment changes the file\n",
        encoding="utf-8",
    )
    assert ensure_sdk(workspace, manifest, ontology_file, changed)["action"] == "generated"

    edited = sdk_dir(workspace) / "README.md"
    with workspace.exclusive():
        workspace.atomic_write(edited, "edited by hand\n")
    again = ensure_sdk(workspace, manifest, ontology_file, changed)
    assert again["action"] == "generated" and again["written"] == 1

    removed = remove_sdk(workspace)
    assert "README.md" in removed
    assert not (workspace.root / "sdk").exists() and not marker_path(workspace).exists()
