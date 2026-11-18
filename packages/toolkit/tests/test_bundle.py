"""The bundle's format: what is written, what is read back, and every way a file is refused."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dta_toolkit import bundle as bundles

T = datetime(2026, 1, 1, 3, tzinfo=UTC)
HEAD = bundles.Head(branch="main", id="umh5rpmc8tkgqp8lmtt8f5vtqbft6oi4", seq=8, t=T)


def _staging(root: Path) -> Path:
    staging = root / "staging"
    (staging / "workspace" / "ontology").mkdir(parents=True)
    (staging / "workspace" / "manifest.yaml").write_text("twin:\n  id: x\n", encoding="utf-8")
    (staging / "workspace" / "ontology" / "plant.yaml").write_text("id: o\n", encoding="utf-8")
    script = staging / "workspace" / "code" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    (staging / "store" / "oldgen").mkdir(parents=True)
    (staging / "store" / "manifest").write_bytes(b"\x00\x01dolt manifest")
    (staging / "store" / "oldgen" / "LOCK").write_bytes(b"")
    (staging / "artifacts" / "blobs" / "51").mkdir(parents=True)
    (staging / "artifacts" / "blobs" / "51" / "51bc").write_bytes(b"hello artifact\n")
    return staging


def _descriptor(staging: Path, **changes: object) -> bundles.Descriptor:
    members, directories = bundles.scan(staging)
    values = {
        "twin": bundles.TwinRef(id="m8exp-unit", name="Unit", tier="static"),
        "manifest_hash": "a" * 64,
        "contract_hash": "b" * 64,
        "database": "twin_m8exp_unit",
        "head": HEAD,
        "branches": {"main": HEAD.id, "_dta_build": HEAD.id},
        "exported_at": datetime(2026, 11, 18, 19, tzinfo=UTC),
        "members": members,
        "directories": directories,
        "warnings": [bundles.Note(code="bundle.local_source", message="m", source="plant")],
    }
    values.update(changes)
    return bundles.Descriptor(**values)


@pytest.fixture
def written(tmp_path: Path) -> Path:
    staging = _staging(tmp_path)
    target = tmp_path / "out" / bundles.bundle_name("m8exp-unit", HEAD)
    bundles.write(staging, _descriptor(staging), target)
    return target


def _copy(source: Path, target: Path, change, *, extra: list[tuple[tarfile.TarInfo, bytes]] = ()):
    """A copy of a bundle with `change(name, data)` applied to each file; None drops it."""
    with tarfile.open(source) as old, tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as new:
        for info in old.getmembers():
            if info.isfile():
                data = change(info.name, old.extractfile(info).read())
                if data is None:
                    continue
                info.size = len(data)
                new.addfile(info, io.BytesIO(data))
            else:
                new.addfile(info)
        for info, data in extra:
            new.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return target


def _refused(path: Path) -> bundles.BundleError:
    with pytest.raises(bundles.BundleError) as caught:
        with bundles.Reader(path) as reader:
            reader.verify()
    return caught.value


def test_the_name_says_twin_version_and_head() -> None:
    assert bundles.bundle_name("cykelbo", HEAD) == "twin-cykelbo-8-umh5rpmc.dtabundle"


def test_written_bundle_reads_back_whole(written: Path, tmp_path: Path) -> None:
    with tarfile.open(written) as archive:
        infos = archive.getmembers()
    names = [info.name for info in infos]
    # The description comes first, then each section with its folders before their files.
    assert names[0] == "bundle.json"
    assert names[1:] == [
        "workspace",
        "workspace/code",
        "workspace/code/run.sh",
        "workspace/manifest.yaml",
        "workspace/ontology",
        "workspace/ontology/plant.yaml",
        "store",
        "store/manifest",
        "store/oldgen",
        "store/oldgen/LOCK",
        "artifacts",
        "artifacts/blobs",
        "artifacts/blobs/51",
        "artifacts/blobs/51/51bc",
    ]
    # Who owned a file on the exporting machine is not carried; whether it runs is.
    assert {(info.uid, info.gid, info.uname, info.gname) for info in infos} == {(0, 0, "", "")}
    modes = {info.name: info.mode for info in infos}
    assert modes["workspace/code/run.sh"] == 0o755
    assert modes["workspace/manifest.yaml"] == 0o644 and modes["store"] == 0o755

    with bundles.Reader(written) as reader:
        assert reader.descriptor.head == HEAD
        assert reader.descriptor.branches == {"main": HEAD.id, "_dta_build": HEAD.id}
        assert reader.descriptor.warnings[0].source == "plant"
        verified = reader.verify()
        assert verified.files == 6
        assert reader.files("store") == ["store/manifest", "store/oldgen/LOCK"]
        assert reader.read("artifacts/blobs/51/51bc") == b"hello artifact\n"
        unpacked = reader.extract("store", tmp_path / "unpacked")
        assert (unpacked / "manifest").read_bytes() == b"\x00\x01dolt manifest"
        # An empty folder of the backup is carried as it is.
        assert (unpacked / "oldgen" / "LOCK").is_file()
        assert [name for name, _data, _mode in reader.iter_files("workspace")] == [
            "code/run.sh",
            "manifest.yaml",
            "ontology/plant.yaml",
        ]
        digest = reader.digest
    with bundles.Reader(written) as again:
        assert again.digest == digest
    assert not [p for p in written.parent.iterdir() if p != written], "nothing left beside it"


def test_a_write_that_fails_leaves_no_file(tmp_path: Path, monkeypatch) -> None:
    staging = _staging(tmp_path)
    descriptor = _descriptor(staging)
    target = tmp_path / "out" / "twin.dtabundle"
    calls = []

    def failing(self, name, arcname=None, recursive=True, *, filter=None):
        calls.append(arcname)
        if len(calls) == 5:
            raise OSError(28, "No space left on device")
        return original(self, name, arcname, recursive, filter=filter)

    original = tarfile.TarFile.add
    monkeypatch.setattr(tarfile.TarFile, "add", failing)
    with pytest.raises(OSError):
        bundles.write(staging, descriptor, target)
    assert len(calls) == 5
    assert list(target.parent.iterdir()) == [], "neither the bundle nor its partial file is left"


def test_a_member_whose_bytes_changed_is_named(written: Path, tmp_path: Path) -> None:
    def flip(name: str, data: bytes) -> bytes:
        return data.replace(b"hello", b"jello") if name == "artifacts/blobs/51/51bc" else data

    error = _refused(_copy(written, tmp_path / "flipped.dtabundle", flip))
    assert error.code == bundles.CHECKSUM_MISMATCH
    assert error.detail["member"] == "artifacts/blobs/51/51bc"
    assert "artifacts/blobs/51/51bc" in error.message and "checksum" in error.message


def test_a_member_of_another_size_is_named(written: Path, tmp_path: Path) -> None:
    def grow(name: str, data: bytes) -> bytes:
        return data + b"#" if name == "workspace/manifest.yaml" else data

    error = _refused(_copy(written, tmp_path / "grown.dtabundle", grow))
    assert error.code == bundles.CHECKSUM_MISMATCH
    assert error.detail["member"] == "workspace/manifest.yaml"


def test_a_missing_or_unlisted_member_is_named(written: Path, tmp_path: Path) -> None:
    def drop(name: str, data: bytes) -> bytes | None:
        return None if name == "store/manifest" else data

    error = _refused(_copy(written, tmp_path / "short.dtabundle", drop))
    assert error.code == bundles.CHECKSUM_MISMATCH and error.detail["member"] == "store/manifest"
    assert "listed in bundle.json but is not in the bundle" in error.message

    extra = tarfile.TarInfo("artifacts/blobs/51/smuggled")
    extra.size = 3
    error = _refused(
        _copy(written, tmp_path / "long.dtabundle", lambda n, d: d, extra=[(extra, b"abc")])
    )
    assert error.code == bundles.CHECKSUM_MISMATCH
    assert error.detail["member"] == "artifacts/blobs/51/smuggled"

    folder = tarfile.TarInfo("store/unlisted")
    folder.type = tarfile.DIRTYPE
    error = _refused(
        _copy(written, tmp_path / "folder.dtabundle", lambda n, d: d, extra=[(folder, b"")])
    )
    assert error.code == bundles.CHECKSUM_MISMATCH and error.detail["member"] == "store/unlisted"


@pytest.mark.parametrize(
    "name",
    ["../escape", "/etc/passwd", "workspace/../../escape", "elsewhere/file", "workspace/./x"],
)
def test_a_member_that_could_land_outside_is_refused(
    written: Path, tmp_path: Path, name: str
) -> None:
    info = tarfile.TarInfo(name)
    info.size = 1
    error = _refused(
        _copy(written, tmp_path / "unsafe.dtabundle", lambda n, d: d, extra=[(info, b"x")])
    )
    assert error.code == bundles.UNSAFE_MEMBER


def test_a_link_or_a_repeated_name_is_refused(written: Path, tmp_path: Path) -> None:
    link = tarfile.TarInfo("workspace/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    error = _refused(
        _copy(written, tmp_path / "link.dtabundle", lambda n, d: d, extra=[(link, b"")])
    )
    assert error.code == bundles.UNSAFE_MEMBER and error.detail["member"] == "workspace/link"

    again = tarfile.TarInfo("store/manifest")
    again.size = 1
    error = _refused(
        _copy(written, tmp_path / "twice.dtabundle", lambda n, d: d, extra=[(again, b"x")])
    )
    assert error.code == bundles.UNSAFE_MEMBER and "twice" in error.message


def _with_descriptor(written: Path, target: Path, change) -> Path:
    def edit(name: str, data: bytes) -> bytes:
        if name != "bundle.json":
            return data
        document = json.loads(data)
        change(document)
        return json.dumps(document).encode("utf-8")

    return _copy(written, target, edit)


def test_an_unknown_format_is_refused_before_anything_else(written: Path, tmp_path: Path) -> None:
    def newer(document: dict) -> None:
        document["format"] = 2
        document["members"] = "a shape this toolkit has never seen"

    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(_with_descriptor(written, tmp_path / "v2.dtabundle", newer))
    assert caught.value.code == bundles.UNKNOWN_FORMAT
    assert caught.value.detail == {"format": 2, "known": [1]}


def test_what_is_not_a_bundle_is_refused(written: Path, tmp_path: Path) -> None:
    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(tmp_path / "nowhere.dtabundle")
    assert caught.value.code == bundles.NOT_FOUND

    text = tmp_path / "notes.dtabundle"
    text.write_text("not an archive at all\n" * 100, encoding="utf-8")
    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(text)
    assert caught.value.code == bundles.UNREADABLE

    empty = tmp_path / "empty.dtabundle"
    with tarfile.open(empty, "w") as archive:
        info = tarfile.TarInfo("workspace")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(empty)
    assert caught.value.code == bundles.UNREADABLE and "bundle.json" in caught.value.message

    def other(document: dict) -> None:
        document["kind"] = "something-else"

    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(_with_descriptor(written, tmp_path / "other.dtabundle", other))
    assert caught.value.code == bundles.UNREADABLE

    def broken(document: dict) -> None:
        del document["head"]

    with pytest.raises(bundles.BundleError) as caught:
        bundles.Reader(_with_descriptor(written, tmp_path / "broken.dtabundle", broken))
    assert caught.value.code == bundles.UNREADABLE
    assert caught.value.detail["problems"][0]["loc"] == "head"


def test_a_staging_folder_with_a_link_is_not_written(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    (staging / "artifacts" / "link").symlink_to("/etc/passwd")
    with pytest.raises(bundles.BundleError) as caught:
        bundles.scan(staging)
    assert caught.value.code == bundles.UNSAFE_MEMBER
    assert caught.value.detail["member"] == "artifacts/link"


def test_the_toolkit_imports_no_store_driver_and_names_the_store_as_the_store_does():
    # The toolkit's image holds no store driver: importing the store package there fails.
    import subprocess
    import sys

    code = (
        "import sys, dta_toolkit.cli; "
        "sys.exit(1 if any(m == 'pymysql' or m.startswith('dta_core.store') for m in sys.modules) "
        "else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0

    from dta_core.store.model import LIVE_BRANCH
    from dta_core.store.settings import DATABASE_NAME
    from dta_toolkit.commands import portability

    assert portability.LIVE_BRANCH == LIVE_BRANCH
    assert portability.DATABASE_NAME.pattern == DATABASE_NAME.pattern
