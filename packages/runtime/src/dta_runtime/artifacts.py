"""Files the twin keeps outside the graph, named by the SHA-256 of their content.

Code the twin brings, fitted states of models and tables too large for the
graph are kept here. A file is written once and never changed: the same
content always has the same name, so storing it twice stores it once, and what
a record in the graph refers to cannot change under it. The graph's
`TwinArtifact` records say what a file is; this module only keeps the bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from dta_core.errors import DtaError
from dta_core.log import Log

log = Log("runtime.artifacts")

BLOBS_DIR = "blobs"
INCOMING_DIR = ".incoming"
# The largest file kept. A fitted state or a code archive is far smaller; the limit keeps
# one request from filling the volume.
MAX_BYTES = 512 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactMissing(DtaError):
    code = "artifact.not_found"


class ArtifactTooLarge(DtaError):
    code = "artifact.too_large"


class ArtifactEmpty(DtaError):
    code = "artifact.empty"


@dataclass(frozen=True)
class Stored:
    sha256: str
    size_bytes: int
    # Path inside the artifact volume, as a `TwinArtifact` records it.
    location: str
    # False when the same content was already kept.
    created: bool


class Upload:
    """One file on its way in: written piece by piece, kept under its hash at the end."""

    def __init__(self, store: ArtifactStore, limit: int) -> None:
        self._store = store
        self._limit = limit
        self._digest = hashlib.sha256()
        self._size = 0
        incoming = store.root / BLOBS_DIR / INCOMING_DIR
        incoming.mkdir(parents=True, exist_ok=True)
        self._path = incoming / secrets.token_hex(16)
        self._file = self._path.open("wb")

    def write(self, chunk: bytes) -> None:
        self._size += len(chunk)
        if self._size > self._limit:
            log.warn("an artifact is larger than the limit; refused", limit_bytes=self._limit)
            raise ArtifactTooLarge(
                f"an artifact holds at most {self._limit} bytes",
                detail={"limit_bytes": self._limit},
            )
        self._digest.update(chunk)
        self._file.write(chunk)

    def commit(self) -> Stored:
        """Keep what was written; the file's name is the hash of its content."""
        if self._size == 0:
            self.discard()
            raise ArtifactEmpty("an artifact with no content cannot be kept")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        sha256 = self._digest.hexdigest()
        target = self._store.path(sha256)
        created = not target.exists()
        if created:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic on one filesystem: a reader sees the whole file or none.
            os.replace(self._path, target)
        else:
            self._path.unlink(missing_ok=True)
        log.info(
            "artifact kept" if created else "artifact already kept",
            sha256=sha256,
            size_bytes=self._size,
        )
        return Stored(sha256, self._size, self._store.location(sha256), created)

    def discard(self) -> None:
        """Drop what was written. Safe to call at any point, more than once."""
        if not self._file.closed:
            self._file.close()
        self._path.unlink(missing_ok=True)


class ArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self.root = root
        self._max_bytes = max_bytes

    @staticmethod
    def location(sha256: str) -> str:
        return f"{BLOBS_DIR}/{sha256[:2]}/{sha256}"

    def path(self, sha256: str) -> Path:
        if not SHA256.match(sha256):
            raise ArtifactMissing(
                "an artifact is named by 64 hexadecimal characters", detail={"sha256": sha256}
            )
        return self.root / self.location(sha256)

    def find(self, sha256: str) -> Path:
        """The file kept under a hash."""
        path = self.path(sha256)
        if not path.is_file():
            log.debug("an artifact that is not kept was asked for", sha256=sha256)
            raise ArtifactMissing(f"no artifact {sha256} is kept", detail={"sha256": sha256})
        return path

    def read(self, sha256: str) -> bytes:
        return self.find(sha256).read_bytes()

    def upload(self) -> Upload:
        return Upload(self, self._max_bytes)

    def put(self, content: bytes) -> Stored:
        upload = self.upload()
        try:
            upload.write(content)
            return upload.commit()
        except BaseException:
            upload.discard()
            raise
