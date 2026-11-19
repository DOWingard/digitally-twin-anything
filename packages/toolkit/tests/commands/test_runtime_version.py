"""The toolkit refuses to start a stack whose runtime image is older than the manifest.

What an image reads is asked at one boundary, `runtimecheck.answer`; most tests put
the image's answer there, and one asks the real image built from this tree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import m3tk_kit as kit
import portability_kit as port
import pytest
import yaml
from dta_core.errors import DtaError
from dta_core.manifest import parse_manifest
from dta_core.manifest.models import MANIFEST_VERSION
from dta_toolkit import runtimecheck
from dta_toolkit.commands import portability
from dta_toolkit.output import Session
from dta_toolkit.stack import StackError
from dta_toolkit.workspace import Workspace

TOO_OLD = "runtime.too_old"


class Asked:
    """An image's answer, put at the boundary, counting how often it is asked."""

    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.images: list[str] = []

    def __call__(self, _stack: Any, image: str) -> dict[str, Any] | None:
        self.images.append(image)
        return self.answer


@pytest.fixture
def image_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[[int | None], Asked]:
    def put(reads: int | None) -> Asked:
        document = None if reads is None else {"version": "x", "manifest_version": reads}
        asked = Asked(document)
        monkeypatch.setattr(runtimecheck, "answer", asked)
        return asked

    return put


class _Plan:
    runtime_image = "dta-runtime:somewhere"


class _Stack:
    plan = _Plan()


def _session(tmp_path: Path) -> Session:
    return Session(workspace=Workspace(tmp_path), json_output=True, trace_id="t")


@pytest.mark.parametrize(
    ("answer", "reads"),
    [
        ({"manifest_version": 2, "version": "0.2.0"}, 2),
        ({"manifest_version": 5}, 5),
        (None, 1),  # the image knows no `version` command
        ({"version": "0.1.0"}, 1),
        ({"manifest_version": "2"}, 1),
    ],
)
def test_what_an_image_answers_is_read_as_the_versions_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: dict | None, reads: int
):
    monkeypatch.setattr(runtimecheck, "answer", Asked(answer))
    assert runtimecheck.image_reads(_session(tmp_path), _Stack()) == reads  # type: ignore[arg-type]


def test_an_image_is_asked_once_per_command(tmp_path: Path, image_reads):
    asked = image_reads(2)
    current = _session(tmp_path)
    for _ in range(3):
        runtimecheck.image_reads(current, _Stack())  # type: ignore[arg-type]
    assert asked.images == ["dta-runtime:somewhere"]
    runtimecheck.image_reads(_session(tmp_path), _Stack())  # type: ignore[arg-type]
    assert len(asked.images) == 2, "another command asks again"


def _ready_for_up(twin: kit.KitTwin) -> None:
    twin.write()
    twin.ok("env", "template")
    twin.fill_env()


def test_up_is_refused_when_the_image_reads_less_than_the_manifest(make_twin, image_reads):
    twin = make_twin()
    _ready_for_up(twin)
    asked = image_reads(1)
    error = twin.failed(TOO_OLD, "up", exit_code=2)
    assert error["detail"] == {
        "manifest_version": MANIFEST_VERSION,
        "image": kit.RUNTIME_IMAGE,
        "image_reads": 1,
        "next": f"set runtime.image to an image that reads manifest version {MANIFEST_VERSION}",
    }
    assert f"manifest version {MANIFEST_VERSION}" in error["message"]
    assert "reads versions up to 1" in error["message"] and kit.RUNTIME_IMAGE in error["message"]
    assert asked.images == [kit.RUNTIME_IMAGE]
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}


@pytest.mark.parametrize(("written", "refused"), [(1, False), (None, True), (2, True)])
def test_the_manifest_s_version_is_held_against_what_the_image_reads(
    tmp_path: Path, image_reads, written: int | None, refused: bool
):
    """A manifest that names no version reads as the newest, so an older image refuses it."""
    image_reads(1)
    document: dict[str, Any] = {"twin": {"id": "t", "name": "T"}}
    if written is not None:
        document["manifest_version"] = written
    manifest = parse_manifest(document)
    if not refused:
        runtimecheck.require_image_reads(_session(tmp_path), _Stack(), manifest)  # type: ignore[arg-type]
        return
    with pytest.raises(DtaError) as caught:
        runtimecheck.require_image_reads(_session(tmp_path), _Stack(), manifest)  # type: ignore[arg-type]
    assert caught.value.code == TOO_OLD
    assert caught.value.detail["manifest_version"] == 2


def test_import_unpacks_nothing_of_a_bundle_newer_than_this_toolkit(tmp_path: Path, image_reads):
    asked = image_reads(MANIFEST_VERSION)

    def newer(manifest: dict[str, Any]) -> None:
        manifest["manifest_version"] = MANIFEST_VERSION + 1
        manifest["contract"]["later"] = {"field": True}

    bundle = port.synthetic_bundle(tmp_path, change=newer)
    target = tmp_path / "new"
    code, document = port.Dta(target).json("import", str(bundle), "--into", str(target))
    assert code == 2, document
    error = document["error"]
    assert error["code"] == "manifest.too_new"
    assert error["detail"]["manifest_version"] == MANIFEST_VERSION + 1
    assert error["detail"]["reads"] == MANIFEST_VERSION
    assert not target.exists(), "nothing is unpacked"
    assert asked.images == [], "no image is asked for a manifest this toolkit cannot read"


def test_the_second_run_of_import_is_refused_before_it_starts_the_store(
    tmp_path: Path, image_reads
):
    bundle = port.synthetic_bundle(tmp_path)
    target = tmp_path / "new"
    dta = port.Dta(target)
    asked = image_reads(1)
    code, document = dta.json("import", str(bundle), "--into", str(target))
    assert code == 0 and document["result"]["status"] == portability.UNPACKED, document
    assert asked.images == [], "the first run starts nothing, so it asks nothing"
    data = tmp_path / "data"
    data.mkdir()
    port.fill_env(target / ".env", data)
    code, document = dta.json("import", str(bundle), "--into", str(target))
    assert code == 2, document
    assert document["error"]["code"] == TOO_OLD
    assert document["error"]["detail"]["image"] == port.RUNTIME_IMAGE
    assert asked.images == [port.RUNTIME_IMAGE]
    twin_id = yaml.safe_load((target / "manifest.yaml").read_text("utf-8"))["twin"]["id"]
    assert port.leftovers(f"dta-{twin_id}") == {"containers": [], "volumes": [], "networks": []}


@kit.needs_stack
def test_the_real_image_says_it_reads_this_manifest_version(make_twin, monkeypatch):
    """The image named by DTA_TEST_RUNTIME_IMAGE is built from this tree: asked for real,
    once within the command, it reads what the toolkit writes, and the build goes on."""
    asked: list[tuple[str, dict[str, Any] | None]] = []

    def ask(stack: Any, image: str) -> dict[str, Any] | None:
        answered = runtimecheck.ask_image(stack, image)
        asked.append((image, answered))
        return answered

    monkeypatch.setattr(runtimecheck, "answer", ask)
    twin = make_twin()
    twin.prepare()
    twin.ok("build")
    [(image, answered)] = asked
    assert image == kit.RUNTIME_IMAGE
    assert answered is not None and answered["manifest_version"] == MANIFEST_VERSION


class _NoVersionCommand:
    """A stack whose image answers as a runtime built before the version command did."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        self.exit_code, self.stderr = exit_code, stderr

    def run_image(self, image: str, *args: str, timeout: float) -> dict[str, Any]:
        error = StackError(
            "the command printed no document",
            detail={"exit_code": self.exit_code, "stderr": self.stderr},
        )
        error.code = "stack.bad_output"
        raise error


def test_an_image_that_knows_no_version_command_reads_1_and_any_other_failure_is_raised():
    usage = "Usage: dta-runtime [OPTIONS] COMMAND [ARGS]...\nError: No such command 'version'."
    assert runtimecheck.ask_image(_NoVersionCommand(2, usage), "old:image") is None
    for exit_code, stderr in ((1, usage), (2, "Error: Missing option"), (125, "no such image")):
        with pytest.raises(StackError):
            runtimecheck.ask_image(_NoVersionCommand(exit_code, stderr), "old:image")
