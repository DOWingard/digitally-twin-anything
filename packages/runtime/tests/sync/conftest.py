"""Fixtures of the sync tests: twins whose manifest declares sources, and a way to load them."""

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
from dta_runtime.settings import RuntimeSettings  # noqa: E402
from dta_runtime.sync import LoadReport, load  # noqa: E402
from dta_runtime.twin import Bundle, Twin  # noqa: E402
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
