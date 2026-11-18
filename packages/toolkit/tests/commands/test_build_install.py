"""The install step of a build when a live twin's sync commits while it runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dta_core.manifest import parse_manifest
from dta_toolkit.commands import build
from dta_toolkit.stack import StackError
from m3tk_kit import ONTOLOGY, manifest

INSTALLED = {"version": {"id": "abc", "seq": 7, "t": "2026-01-01T00:00:00Z"}, "loaded": True}


class Runtime:
    """Answers `install` with the given errors first, then with a version."""

    def __init__(self, *codes: str) -> None:
        self.codes = list(codes)
        self.calls = 0

    def runtime(self, *args: str, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self.codes:
            code = self.codes.pop(0)
            raise StackError("version time is earlier than the head's", code=code, detail={})
        return INSTALLED


def step(tmp_path: Path, runtime: Runtime) -> SimpleNamespace:
    ontology = tmp_path / "plant.yaml"
    ontology.write_text(ONTOLOGY, encoding="utf-8")
    return SimpleNamespace(
        manifest=parse_manifest(manifest("m6tk-unit")),
        ontology_file=ontology,
        stack=runtime,
        install_head=None,
        install_loaded=False,
    )


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "INSTALL_PAUSE_S", 0.0)


def test_install_runs_again_when_a_sync_committed_in_between(tmp_path: Path) -> None:
    runtime = Runtime(build.TIME_REGRESSION)
    found = build.Build.install(step(tmp_path, runtime))  # type: ignore[arg-type]
    assert runtime.calls == 2 and found["attempts"] == 2
    assert found["version"]["seq"] == 7 and found["loaded"] is True


def test_install_gives_up_after_its_tries(tmp_path: Path) -> None:
    runtime = Runtime(*[build.TIME_REGRESSION] * build.INSTALL_TRIES)
    with pytest.raises(StackError) as raised:
        build.Build.install(step(tmp_path, runtime))  # type: ignore[arg-type]
    assert raised.value.code == build.TIME_REGRESSION and runtime.calls == build.INSTALL_TRIES


def test_any_other_failure_of_install_is_not_tried_again(tmp_path: Path) -> None:
    runtime = Runtime("twin.ontology_unconfirmed")
    with pytest.raises(StackError):
        build.Build.install(step(tmp_path, runtime))  # type: ignore[arg-type]
    assert runtime.calls == 1
