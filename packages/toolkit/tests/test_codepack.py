"""The twin's code as an archive, the worker image's tag and which model specifications
a build evaluates: all decided without containers."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from dta_core.errors import CheckFailed
from dta_core.manifest import Manifest
from dta_runtime.worker import code as worker_code
from dta_toolkit import codepack, workerimage
from dta_toolkit.commands.computeops import SpecState, job_key, needs_evaluating


def _tree(root: Path) -> Path:
    (root / "plant").mkdir(parents=True)
    (root / "plant" / "__init__.py").write_text("")
    (root / "plant" / "report.py").write_text("def report(twin, uid):\n    return []\n")
    (root / "plant" / "data.csv").write_text("a,b\n1,2\n")
    return root


def test_the_same_files_pack_to_the_same_bytes(tmp_path: Path) -> None:
    first = codepack.pack(_tree(tmp_path / "one"))
    second_root = _tree(tmp_path / "two")
    # Other times and modes on disk change nothing.
    for path in second_root.rglob("*"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
        if path.is_file():
            path.chmod(0o600)
    second = codepack.pack(second_root)
    assert first.content == second.content
    assert first.sha256 == second.sha256
    assert first.files == ("plant/__init__.py", "plant/data.csv", "plant/report.py")


def test_a_changed_file_changes_the_hash(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = codepack.pack(root).sha256
    (root / "plant" / "report.py").write_text("def report(twin, uid):\n    return [1]\n")
    assert codepack.pack(root).sha256 != before


def test_entries_have_a_fixed_time_and_mode(tmp_path: Path) -> None:
    packed = codepack.pack(_tree(tmp_path))
    with zipfile.ZipFile(io.BytesIO(packed.content)) as archive:
        for entry in archive.infolist():
            assert entry.date_time == codepack.FIXED_TIME
            assert (entry.external_attr >> 16) & 0o777 == 0o644
            assert entry.compress_type == zipfile.ZIP_STORED


def test_bytecode_hidden_files_and_links_are_left_out(tmp_path: Path) -> None:
    root = _tree(tmp_path / "code")
    (root / "plant" / "__pycache__").mkdir()
    (root / "plant" / "__pycache__" / "report.cpython-312.pyc").write_bytes(b"\0")
    (root / "plant" / "stale.pyc").write_bytes(b"\0")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x")
    (root / ".env").write_text("SECRET=1")
    (root / "plant" / ".hidden.py").write_text("x = 1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "plant" / "alias.py").symlink_to(root / "plant" / "report.py")
    packed = codepack.pack(root)
    assert packed.files == ("plant/__init__.py", "plant/data.csv", "plant/report.py")
    assert packed.skipped_links == ("linked", "plant/alias.py")


def test_a_directory_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CheckFailed) as caught:
        codepack.pack(tmp_path / "absent")
    assert caught.value.code == codepack.CODE_MISSING


def test_the_limits_are_the_workers() -> None:
    assert codepack.MAX_ENTRIES == worker_code.MAX_ENTRIES
    assert codepack.MAX_UNPACKED_BYTES == worker_code.MAX_UNPACKED_BYTES


def test_too_many_files_are_refused_before_anything_is_packed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    monkeypatch.setattr(codepack, "MAX_ENTRIES", 2)
    with pytest.raises(CheckFailed) as caught:
        codepack.pack(root)
    assert caught.value.code == codepack.CODE_TOO_LARGE
    assert caught.value.detail["limit"] == "entries"
    assert "more than 2 files" in caught.value.message


def test_too_many_bytes_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    monkeypatch.setattr(codepack, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(CheckFailed) as caught:
        codepack.pack(root)
    assert caught.value.detail["limit"] == "unpacked_bytes"


def test_too_large_an_archive_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    monkeypatch.setattr(codepack, "MAX_ARCHIVE_BYTES", 100)
    with pytest.raises(CheckFailed) as caught:
        codepack.pack(root)
    assert caught.value.detail["limit"] == "archive_bytes"


def test_the_worker_unpacks_what_is_packed(tmp_path: Path) -> None:
    packed = codepack.pack(_tree(tmp_path / "code"))
    target = tmp_path / "unpacked"
    worker_code.unpack(packed.content, packed.sha256, target)
    assert (target / "plant" / "report.py").read_text().startswith("def report")


def _manifest(entry: str, family: str) -> Manifest:
    return Manifest.model_validate(
        {
            "functions": {"f": {"kind": "registered", "entry": entry}},
            "models": {
                "level": {
                    "candidates": {
                        "a": {"family": "theta"},
                        "b": {"family": family},
                    }
                }
            },
        }
    )


def test_modules_the_manifest_names_are_found_or_reported(tmp_path: Path) -> None:
    files = ("plant/__init__.py", "plant/report.py", "drift.py")
    assert codepack.missing_modules(_manifest("plant.report:report", "drift:balance"), files) == []
    missing = codepack.missing_modules(_manifest("plant.flows:report", "nowhere:fit"), files)
    assert [entry["entry"] for entry in missing] == ["plant.flows:report", "nowhere:fit"]
    assert missing[0]["path"] == "functions.f.entry"
    assert missing[1]["path"] == "models.level.candidates.b.family"


def test_the_worker_tag_names_the_requirements_and_the_runtime() -> None:
    tag = workerimage.worker_tag("m5tk-a", b"six==1.17.0\n", "sha256:abc")
    assert tag.startswith("dta-worker-m5tk-a:")
    assert len(tag.split(":", 1)[1]) == 12
    assert tag == workerimage.worker_tag("m5tk-a", b"six==1.17.0\n", "sha256:abc")
    assert tag != workerimage.worker_tag("m5tk-a", b"six==1.16.0\n", "sha256:abc")
    assert tag != workerimage.worker_tag("m5tk-a", b"six==1.17.0\n", "sha256:abd")


def test_the_worker_dockerfile_installs_wheels_only_and_ends_as_the_twin_user() -> None:
    text = workerimage.render_dockerfile("m5tk-a", "dta-runtime:test")
    assert text.splitlines()[11] == "FROM dta-runtime:test"
    assert "--only-binary=:all:" in text and "--no-cache-dir" in text
    assert "compileall" in text
    assert text.rstrip().endswith("USER twin")


_ACTIVE = SpecState("level", {"m/a": "active", "m/b": "candidate"}, frozenset({"m/a", "m/b"}))
_NEW = SpecState("level", {"m/a": "candidate", "m/b": "candidate"}, frozenset())
_TRIED = SpecState("level", {"m/a": "candidate", "m/b": "candidate"}, frozenset({"m/a", "m/b"}))
_HALF = SpecState("level", {"m/a": "candidate", "m/b": "candidate"}, frozenset({"m/a"}))


@pytest.mark.parametrize(
    ("state", "touched", "every", "evaluated"),
    [
        (_ACTIVE, set(), False, False),
        (_ACTIVE, {"m/b"}, False, True),
        (_ACTIVE, {"other/x"}, False, False),
        (_ACTIVE, set(), True, True),
        (_NEW, set(), False, True),
        (_HALF, set(), False, True),
        # Every candidate was tried and none accepted: only a change evaluates it again.
        (_TRIED, set(), False, False),
        (_TRIED, {"m/a"}, False, True),
    ],
)
def test_which_specifications_a_build_evaluates(
    state: SpecState, touched: set[str], every: bool, evaluated: bool
) -> None:
    assert (needs_evaluating(state, touched, every) is not None) is evaluated


def test_an_evaluation_key_names_the_manifest_the_spec_and_the_data() -> None:
    key = job_key("evaluate", "hash", "level", "head-1")
    assert key.startswith("dta-build:evaluate:")
    assert key == job_key("evaluate", "hash", "level", "head-1")
    assert key != job_key("evaluate", "hash", "level", "head-2")
    assert key != job_key("evaluate", "hash", "flow", "head-1")
    assert 1 <= len(key) <= 200 and key.isascii()
