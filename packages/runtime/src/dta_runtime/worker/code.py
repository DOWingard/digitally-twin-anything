"""Code a twin brings: a zip archive kept as an artifact, unpacked once per content hash.

The archive is fetched through the API, checked against its hash, and unpacked into
a directory named by the hash. It is unpacked beside that directory first and moved
into place in one step, so a directory that exists is complete, and two runs of the
same code at once both end with the same directory.
"""

from __future__ import annotations

import io
import secrets
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from dta_core.errors import DtaError
from dta_core.log import Log

from ..protocol import Artifacts
from .errors import BAD_STATE

log = Log("worker.code")

# Bounds on what one archive may unpack to: code, not data.
MAX_ENTRIES = 10_000
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
INCOMING_PREFIX = ".incoming-"


def _refuse(sha256: str, message: str) -> DtaError:
    log.error("a code artifact cannot be unpacked", sha256=sha256, reason=message)
    return DtaError(f"code artifact {sha256}: {message}", code=BAD_STATE)


def _checked(archive: zipfile.ZipFile, sha256: str) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise _refuse(sha256, f"it holds more than {MAX_ENTRIES} entries")
    total = 0
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or name.startswith(("/", "\\")) or (name[1:2] == ":"):
            raise _refuse(sha256, f"entry {name!r} has an absolute path")
        if ".." in path.parts:
            raise _refuse(sha256, f"entry {name!r} leads out of the archive with '..'")
        total += entry.file_size
        if total > MAX_UNPACKED_BYTES:
            raise _refuse(sha256, f"it unpacks to more than {MAX_UNPACKED_BYTES} bytes")
    return entries


def unpack(content: bytes, sha256: str, target: Path) -> None:
    """Unpack an archive into `target`, which must not exist yet."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _checked(archive, sha256)
            target.mkdir(parents=True)
            archive.extractall(target)
    except zipfile.BadZipFile as exc:
        raise _refuse(sha256, "it is not a zip archive") from exc


def code_dir(artifacts: Artifacts, sha256: str, root: Path) -> Path:
    """The directory holding the code of an artifact, fetched and unpacked if need be."""
    target = root / sha256
    if target.is_dir():
        log.debug("code already unpacked", sha256=sha256)
        return target
    started = time.perf_counter()
    content = artifacts.get(sha256)
    incoming = root / f"{INCOMING_PREFIX}{secrets.token_hex(8)}"
    try:
        unpack(content, sha256, incoming)
        try:
            incoming.rename(target)
        except OSError:
            if not target.is_dir():
                raise
            # Another run of the same code unpacked it first.
            log.debug("code unpacked by another run meanwhile", sha256=sha256)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    log.info(
        "code unpacked",
        sha256=sha256,
        size_bytes=len(content),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return target
