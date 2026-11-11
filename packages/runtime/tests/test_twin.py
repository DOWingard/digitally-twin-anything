"""Installing, opening and revising a twin whose manifest and ontology live in the graph."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from dta_core.bundle import BundleError
from dta_core.manifest import manifest_hash
from dta_core.store import MigrationRequired, NotInstalled, StoreSettings
from dta_runtime.twin import (
    MANIFEST_UID,
    Bundle,
    TwinError,
    install_twin,
    open_twin,
)
from runtime_testkit import grow_ontology, shrink_ontology

T0 = datetime(2026, 1, 5, tzinfo=UTC)


def test_install_writes_version_zero_then_the_manifest(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    with pytest.raises(NotInstalled):
        open_twin(settings)
    installed = install_twin(settings, water_bundle, t=T0)
    try:
        assert installed.actions == ("installed", "registered")
        log = installed.twin.store.versions()
        assert [(v.seq, v.cause, v.t) for v in log] == [(0, "install", T0), (1, "registration", T0)]
        assert log[1].extra == {
            "manifest_hash": manifest_hash(water_bundle.manifest),
            "revision": 1,
        }
        record = installed.twin.manifest_record()
        assert record["twin_id"] == "water"
        assert record["tier"] == "static"
        assert installed.twin.manifest() == water_bundle.manifest
    finally:
        installed.twin.close()


def test_a_service_opens_the_twin_knowing_only_the_store(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    install_twin(settings, water_bundle, t=T0).twin.close()
    twin = open_twin(settings)
    try:
        assert twin.ontology.has_type("Tank")
        assert twin.manifest().twin.id == "water"
        # The row checks generated from the stored ontology are in force.
        problems = twin.store.validator.validate(
            "Tank", [{"uid": "node:T1", "name": "T1", "elevation": 1.0, "min_level": 0.0}]
        )
        assert [(p.property, p.rule) for p in problems] == [("max_level", "required")]
    finally:
        twin.close()


def test_installing_again_changes_nothing(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    install_twin(settings, water_bundle, t=T0).twin.close()
    again = install_twin(settings, water_bundle)
    try:
        assert again.actions == ()
        assert again.version.seq == 1
    finally:
        again.twin.close()


def test_a_changed_manifest_is_recorded_as_a_new_revision(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    install_twin(settings, water_bundle, t=T0).twin.close()
    revised = water_bundle.manifest.model_copy(deep=True)
    revised.twin.revision = 2
    revised.twin.name = "Water network, revised"
    result = install_twin(settings, dataclasses.replace(water_bundle, manifest=revised))
    try:
        assert result.actions == ("registered",)
        assert result.version.seq == 2
        assert result.twin.manifest().twin.name == "Water network, revised"
        # The earlier revision is still what the twin held at the earlier version.
        assert result.twin.manifest(at="#1").twin.name == "Water network"
        history = result.twin.store.history(MANIFEST_UID, properties=["revision"])
        assert history.column("revision").to_pylist() == [1, 2]
    finally:
        result.twin.close()


def test_sources_of_the_manifest_become_objects_and_a_dropped_one_is_disabled(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    with_sources = water_bundle.manifest.model_copy(deep=True)
    with_sources.sources = {
        "registry": {"connector": "sql", "location": "postgresql://${REG_USER}@db/registry"},
        "telemetry": {"connector": "file", "location": "file://${RECORDING_DIR}/telemetry"},
    }
    first = install_twin(settings, dataclasses.replace(water_bundle, manifest=with_sources), t=T0)
    try:
        rows = {row["uid"]: row for row in first.twin.store.table("TwinSource").to_pylist()}
        assert set(rows) == {"twin_source:registry", "twin_source:telemetry"}
        assert rows["twin_source:registry"]["connector"] == "sql"
        assert rows["twin_source:registry"]["status"] == "ok"
        assert rows["twin_source:telemetry"]["location"] == "file://${RECORDING_DIR}/telemetry"
    finally:
        first.twin.close()
    fewer = with_sources.model_copy(deep=True)
    del fewer.sources["telemetry"]
    fewer.twin.revision = 2
    second = install_twin(settings, dataclasses.replace(water_bundle, manifest=fewer))
    try:
        rows = {row["uid"]: row for row in second.twin.store.table("TwinSource").to_pylist()}
        assert rows["twin_source:telemetry"]["status"] == "disabled"
        assert rows["twin_source:registry"]["status"] == "ok"
    finally:
        second.twin.close()


def test_an_ontology_that_only_adds_is_migrated_and_one_that_removes_is_refused(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    settings = new_settings()
    install_twin(settings, water_bundle, t=T0).twin.close()
    grown = dataclasses.replace(
        water_bundle, ontology_text=grow_ontology(water_bundle.ontology_text)
    )
    result = install_twin(settings, grown)
    try:
        assert result.actions == ("migrated",)
        assert result.twin.ontology.type("Pipe").has_field("material")
        assert result.twin.store.versions()[-1].cause == "migration"
    finally:
        result.twin.close()
    shrunk = dataclasses.replace(
        water_bundle, ontology_text=shrink_ontology(water_bundle.ontology_text)
    )
    with pytest.raises(MigrationRequired):
        install_twin(settings, shrunk)


def test_an_ontology_whose_types_differ_from_the_confirmed_ones_is_refused(
    new_settings: Callable[[], StoreSettings], water_bundle: Bundle
):
    unconfirmed = water_bundle.manifest.model_copy(deep=True)
    unconfirmed.ontology.type_signature = "0" * 64
    with pytest.raises(TwinError) as caught:
        install_twin(new_settings(), dataclasses.replace(water_bundle, manifest=unconfirmed), t=T0)
    assert caught.value.code == "twin.ontology_unconfirmed"


def test_a_bundle_round_trips_and_a_malformed_one_is_refused(water_bundle: Bundle):
    again = Bundle.from_json(water_bundle.to_json())
    assert again.manifest == water_bundle.manifest
    assert again.ontology_text == water_bundle.ontology_text
    for text in (
        "not json",
        "{}",
        '{"manifest": 1, "ontology": ""}',
        '{"manifest": "a: [", "ontology": ""}',
    ):
        with pytest.raises(BundleError) as caught:
            Bundle.from_json(text)
        assert caught.value.code == "twin.bundle_invalid"
