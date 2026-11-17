"""Files kept by the hash of their content, through the API and on disk."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from dta_runtime.artifacts import (
    ArtifactEmpty,
    ArtifactMissing,
    ArtifactStore,
    ArtifactTooLarge,
)
from live_api_testkit import problem_code

ARTIFACTS = "/api/v1/artifacts"
CONTENT = b"def low_pressure(twin, threshold):\n    return 0\n" * 400


def test_a_file_is_kept_once_under_the_hash_of_its_content(api):
    sha256 = hashlib.sha256(CONTENT).hexdigest()
    head = api.ok("/api/v1/meta/health")["head"]

    first = api.post(ARTIFACTS, content=CONTENT)
    again = api.post(ARTIFACTS, content=CONTENT)

    assert (first.status_code, again.status_code) == (201, 200)
    assert (
        first.json()
        == again.json()
        == {
            "sha256": sha256,
            "size_bytes": len(CONTENT),
            "location": f"blobs/{sha256[:2]}/{sha256}",
        }
    )
    fetched = api.get(f"{ARTIFACTS}/{sha256}")
    assert fetched.status_code == 200 and fetched.content == CONTENT
    assert fetched.headers["content-type"] == "application/octet-stream"
    # Keeping a file changes nothing in the graph.
    assert api.ok("/api/v1/meta/health")["head"] == head


def test_what_cannot_be_kept_or_found_is_refused_with_its_code(api, app_client):
    empty = api.post(ARTIFACTS, content=b"")
    assert (empty.status_code, problem_code(empty)) == (400, "artifact.empty")
    for name in ("0" * 64, "0" * 63, "g" * 64):
        missing = api.get(f"{ARTIFACTS}/{name}")
        assert (missing.status_code, problem_code(missing)) == (404, "artifact.not_found"), name
    # A name that is a path is no artifact's name: it never reaches the files.
    assert api.get(f"{ARTIFACTS}/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert app_client.post(ARTIFACTS, content=CONTENT).status_code == 401
    assert app_client.get(f"{ARTIFACTS}/{'0' * 64}").status_code == 401


def test_the_store_keeps_whole_files_only(tmp_path: Path):
    store = ArtifactStore(tmp_path, max_bytes=1000)
    kept = store.put(b"abc")
    assert kept.created and store.read(kept.sha256) == b"abc"
    assert not store.put(b"abc").created
    assert store.find(kept.sha256) == tmp_path / kept.location

    with pytest.raises(ArtifactTooLarge) as large:
        store.put(b"x" * 1001)
    assert large.value.code == "artifact.too_large" and large.value.detail == {"limit_bytes": 1000}
    with pytest.raises(ArtifactEmpty):
        store.put(b"")
    upload = store.upload()
    upload.write(b"half")
    upload.discard()
    upload.discard()
    # Nothing half written stays, and only the one file is kept.
    files = sorted(path.name for path in tmp_path.rglob("*") if path.is_file())
    assert files == [kept.sha256]
    for name in ("0" * 64, "../../etc/passwd", ""):
        with pytest.raises(ArtifactMissing):
            store.read(name)
