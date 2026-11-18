"""What export and import refuse before they change anything, each with its own code.

Most refusals need no stack: they are judged on the bundle and the target
folder alone. The store-volume and head refusals need a store and are in the
round-trip tests.
"""

from __future__ import annotations

from pathlib import Path

import portability_kit as port
from dta_toolkit import bundle as bundles
from dta_toolkit.commands import portability


def _listing(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".dta/logs" not in str(path) and path.name != "lock"
    }


def test_a_bundle_that_is_not_there_is_refused(tmp_path: Path) -> None:
    dta = port.Dta(tmp_path / "ws")
    code, document = dta.json("import", str(tmp_path / "nowhere.dtabundle"))
    assert code == 2, document
    assert document["error"]["code"] == portability.BUNDLE_NOT_FOUND
    assert "nowhere.dtabundle" in document["error"]["message"]
    assert not (tmp_path / "ws").exists()


def test_a_damaged_bundle_is_refused_naming_the_member(tmp_path: Path) -> None:
    bundle = port.synthetic_bundle(tmp_path)

    def damage(name: str, data: bytes) -> bytes:
        return (
            data.replace(b"Sensor", b"Sinsor") if name == "workspace/ontology/plant.yaml" else data
        )

    damaged = port.rewrite(bundle, tmp_path / "damaged.dtabundle", damage)
    target = tmp_path / "new"
    code, document = port.Dta(target).json("import", str(damaged), "--into", str(target))
    assert code == 2, document
    error = document["error"]
    assert error["code"] == bundles.CHECKSUM_MISMATCH
    assert error["detail"]["member"] == "workspace/ontology/plant.yaml"
    assert "workspace/ontology/plant.yaml" in error["message"]
    assert not target.exists(), "nothing is unpacked from a damaged bundle"


def test_a_format_this_toolkit_does_not_know_is_refused(tmp_path: Path) -> None:
    bundle = port.synthetic_bundle(tmp_path)

    def later(document: dict) -> None:
        document["format"] = 2

    newer = port.with_descriptor(bundle, tmp_path / "v2.dtabundle", later)
    target = tmp_path / "new"
    code, document = port.Dta(target).json("import", str(newer), "--into", str(target))
    assert code == 2, document
    assert document["error"]["code"] == bundles.UNKNOWN_FORMAT
    assert document["error"]["detail"] == {"format": 2, "known": [1]}
    assert not target.exists()


def test_a_folder_that_holds_a_workspace_is_refused(tmp_path: Path) -> None:
    bundle = port.synthetic_bundle(tmp_path)
    target = tmp_path / "taken"
    dta = port.Dta(target)
    code, document = dta.json("init", "--id", "m8exp-taken")
    assert code == 0, document
    before = _listing(target)
    code, document = dta.json("import", str(bundle), "--into", str(target))
    assert code == 2, document
    assert document["error"]["code"] == portability.TARGET_EXISTS
    assert str(target) in document["error"]["message"]
    assert _listing(target) == before, "the workspace is left as it was"

    # A folder holding files of a workspace, without its state, is refused as well.
    half = tmp_path / "half"
    half.mkdir()
    (half / "manifest.yaml").write_text("twin: {id: other}\n", encoding="utf-8")
    code, document = port.Dta(half).json("import", str(bundle), "--into", str(half))
    assert code == 2 and document["error"]["code"] == portability.TARGET_EXISTS, document
    assert document["error"]["detail"]["files"] == ["manifest.yaml"]
    assert (half / "manifest.yaml").read_text(encoding="utf-8") == "twin: {id: other}\n"


@port.needs_stack
def test_export_without_a_running_store_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    twin = port.PortTwin(tmp_path)
    twin.write()
    twin.ok("ontology", "check", "--write")
    twin.ok("env", "template")
    twin.fill_env()
    out = tmp_path / "bundles"
    out.mkdir()
    error = twin.failed(portability.STORE_NOT_RUNNING, "export", "--out", str(out), exit_code=2)
    assert "dta up" in error["message"]
    assert list(out.iterdir()) == [], "no bundle and no partial file"
    assert port.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}
