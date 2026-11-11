"""Fixtures of the sync tests: twins whose manifest declares sources, a way to load them
and a way to run them at the time a clock tells."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

# Helper modules next to this file are imported by name from the tests.
sys.path.append(str(Path(__file__).parent))

from dta_core.log import trace  # noqa: E402
from dta_runtime.clock import Clock  # noqa: E402
from dta_runtime.settings import RuntimeSettings  # noqa: E402
from dta_runtime.sync import LoadReport, RunReport, load  # noqa: E402
from dta_runtime.sync import run as run_once  # noqa: E402
from dta_runtime.twin import Bundle, Twin, install_twin  # noqa: E402
from sync_testkit import T0, TRACE, Sources, runtime_settings, water_manifest  # noqa: E402


@pytest.fixture
def sync_twin(make_twin: Callable[..., Twin], water_ontology_text: str) -> Callable[..., Twin]:
    """A fresh twin of the water network whose manifest holds the given sources."""

    def make(
        sources: Mapping[str, Any],
        sync: Mapping[str, Any] | None = None,
        ontology_text: str | None = None,
        t: datetime = T0,
    ) -> Twin:
        text = ontology_text or water_ontology_text
        sections: dict[str, Any] = {"sources": dict(sources)}
        if sync is not None:
            sections["sync"] = dict(sync)
        bundle = Bundle(manifest=water_manifest(text, **sections), ontology_text=text)
        return make_twin(bundle, t=t)

    return make


@pytest.fixture
def revise(water_ontology_text: str) -> Callable[..., None]:
    """Give a twin that is running a new revision of its manifest: other sources, other sync."""

    def go(twin: Twin, sources: Mapping[str, Any], sync: Mapping[str, Any] | None = None) -> None:
        sections: dict[str, Any] = {"sources": dict(sources)}
        if sync is not None:
            sections["sync"] = dict(sync)
        manifest = water_manifest(water_ontology_text, **sections)
        installed = install_twin(
            twin.store.settings, Bundle(manifest=manifest, ontology_text=water_ontology_text)
        )
        assert installed.actions == ("registered",)
        # The twin of the test goes on with its own connection to the same store.
        installed.twin.close()

    return go


@pytest.fixture
def settings_of(tmp_path: Path) -> Callable[[Twin], RuntimeSettings]:
    def make(twin: Twin) -> RuntimeSettings:
        return runtime_settings(twin.store.settings, tmp_path)

    return make


@pytest.fixture
def run(settings_of: Callable[[Twin], RuntimeSettings]) -> Callable[..., LoadReport]:
    """Load a twin from in-memory sources, under a fixed trace so run names repeat."""

    def go(twin: Twin, sources: Sources, trace_id: str = TRACE) -> LoadReport:
        with trace(trace_id):
            return load(twin, settings_of(twin), {}, connect=sources)

    return go


@pytest.fixture
def tick(settings_of: Callable[[Twin], RuntimeSettings]) -> Callable[..., RunReport]:
    """One run of a live twin at the time its clock tells, under a fixed trace."""

    def go(
        twin: Twin,
        clock: Clock,
        trace_id: str = TRACE,
        env: Mapping[str, str] | None = None,
        **options: Any,
    ) -> RunReport:
        with trace(trace_id):
            return run_once(twin, settings_of(twin), env or {}, clock, **options)

    return go
