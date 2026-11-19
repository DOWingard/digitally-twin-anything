"""The version of the manifest's format: named by the manifest, read up to the newest this
software knows, and never lower than a field the manifest sets."""

from __future__ import annotations

import json

import pytest
import yaml
from dta_core.bundle import Bundle, BundleError
from dta_core.errors import ManifestError, ManifestTooNew
from dta_core.manifest import contract_hash, dump_manifest, manifest_hash, parse_manifest
from dta_core.manifest.checks import run_checks
from dta_core.manifest.io import newer_fields
from dta_core.manifest.models import MANIFEST_VERSION, Manifest, since_of

TWIN = {"twin": {"id": "demo", "name": "Demo"}}
BACKOFF = {"fidelity": {"correction": {"retry_backoff": "PT30M"}}}


def test_the_newest_version_is_2():
    assert MANIFEST_VERSION == 2


@pytest.mark.parametrize("version", [1, 2])
def test_every_version_up_to_the_newest_is_read(version: int):
    manifest = parse_manifest({"manifest_version": version, **TWIN})
    assert manifest.manifest_version == manifest.version == version
    assert "manifest_version" not in {f.path for f in run_checks(manifest)}
    assert yaml.safe_load(dump_manifest(manifest))["manifest_version"] == version


def test_a_newer_version_is_refused_naming_both_numbers():
    with pytest.raises(ManifestTooNew) as caught:
        # Its fields are unknown here; the version is what is reported.
        parse_manifest({"manifest_version": 3, **TWIN, "contract": {"later": True}})
    assert caught.value.code == "manifest.too_new"
    assert caught.value.detail["manifest_version"] == 3
    assert caught.value.detail["reads"] == MANIFEST_VERSION
    assert "version 3" in caught.value.message and "up to 2" in caught.value.message


@pytest.mark.parametrize("value", [0, True, "two", 2.5])
def test_a_version_that_is_no_version_is_a_schema_problem(value):
    with pytest.raises(ManifestError) as caught:
        parse_manifest({"manifest_version": value, **TWIN})
    assert caught.value.code == "manifest.schema"


def test_a_manifest_without_a_version_reads_as_the_newest_with_a_warning():
    manifest = parse_manifest({**TWIN, **BACKOFF})
    assert manifest.manifest_version is None and manifest.version == MANIFEST_VERSION
    (warning,) = [f for f in run_checks(manifest) if f.code == "manifest.version_unset"]
    assert (warning.severity.value, warning.path) == ("warning", "manifest_version")
    assert "manifest_version: 2" in warning.message
    assert "manifest_version" not in yaml.safe_load(dump_manifest(manifest))


def test_a_field_newer_than_the_version_named_is_refused():
    with pytest.raises(ManifestError) as caught:
        parse_manifest({"manifest_version": 1, **TWIN, **BACKOFF})
    error = caught.value
    assert error.code == "manifest.version_too_low"
    assert error.detail["fields"] == [{"path": "fidelity.correction.retry_backoff", "since": 2}]
    assert error.detail["needed"] == 2
    assert "fidelity.correction.retry_backoff needs manifest version 2" in error.message
    assert parse_manifest({"manifest_version": 2, **TWIN, **BACKOFF}).version == 2


def test_only_the_outermost_newer_field_is_named():
    document = {
        "manifest_version": 1,
        **TWIN,
        "contract": {"controls": {"c": {"on_failure": {"mode": "hold", "after_stale": "PT1H"}}}},
    }
    with pytest.raises(ManifestError) as caught:
        parse_manifest(document)
    assert [field["path"] for field in caught.value.detail["fields"]] == [
        "contract.controls.c.on_failure"
    ]


def test_every_field_carries_the_version_that_introduced_it():
    fields = Manifest.model_fields
    assert since_of(fields["twin"]) == 1
    from dta_core.manifest.models import Control, Correction, OnFailure

    assert since_of(Control.model_fields["on_failure"]) == 2
    assert since_of(Correction.model_fields["retry_backoff"]) == 2
    assert {since_of(info) for info in OnFailure.model_fields.values()} == {2}
    assert newer_fields(parse_manifest({"manifest_version": 2, **TWIN, **BACKOFF})) == []


def test_the_version_is_in_no_hash():
    """It says how the manifest is written: a manifest of version 1 hashes as it did."""
    one = parse_manifest({"manifest_version": 1, **TWIN})
    two = parse_manifest({"manifest_version": 2, **TWIN})
    unset = parse_manifest(TWIN)
    assert manifest_hash(one) == manifest_hash(two) == manifest_hash(unset)
    assert contract_hash(one) == contract_hash(two)


def test_a_bundle_of_version_1_is_one_a_runtime_from_before_versions_reads():
    """Such a runtime knows no manifest_version field; the version is left out for it."""
    one = Bundle(manifest=parse_manifest({"manifest_version": 1, **TWIN}), ontology_text="")
    sent = yaml.safe_load(json.loads(one.to_json())["manifest"])
    assert "manifest_version" not in sent
    assert manifest_hash(Bundle.from_json(one.to_json()).manifest) == manifest_hash(one.manifest)
    two = Bundle(manifest=parse_manifest({"manifest_version": 2, **TWIN}), ontology_text="")
    assert yaml.safe_load(json.loads(two.to_json())["manifest"])["manifest_version"] == 2


def test_a_bundle_newer_than_this_software_is_refused_as_too_new():
    manifest_text = yaml.safe_dump({"manifest_version": 3, **TWIN, "later": {}})
    text = json.dumps({"manifest": manifest_text, "ontology": "id: x"})
    with pytest.raises(ManifestTooNew):
        Bundle.from_json(text)
    with pytest.raises(BundleError) as caught:
        Bundle.from_json(json.dumps({"manifest": yaml.safe_dump({"later": {}}), "ontology": ""}))
    assert caught.value.code == "twin.bundle_invalid"
