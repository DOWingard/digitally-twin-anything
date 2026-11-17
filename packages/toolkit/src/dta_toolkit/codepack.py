"""The twin's own code as one zip archive: the same bytes for the same files.

The archive is what the worker unpacks, so it is held to the worker's limits
and refused before anything is uploaded when it would break one. What goes in
is every regular file under the directory except hidden ones, `__pycache__`
and compiled bytecode; a symbolic link is never followed, so nothing outside
the directory can come in. Names are sorted, every entry has one fixed time
and one fixed mode, and entries are stored without compression, so the bytes
(and the hash the twin knows the code by) depend on the files' names and
contents alone, whatever machine or zlib packed them.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dta_core.errors import CheckFailed
from dta_core.families import FAMILIES
from dta_core.functions import REGISTERED_ENTRY
from dta_core.log import Log
from dta_core.manifest import Manifest

log = Log("codepack")

# The worker's bounds on what one archive may unpack to, and the API's on one artifact.
MAX_ENTRIES = 10_000
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# The earliest time a zip entry can carry; any fixed time would do.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644
_UNIX = 3
_SKIPPED_DIRS = frozenset({"__pycache__"})
_SKIPPED_SUFFIXES = (".pyc", ".pyo")
_EXTENSION = re.compile(r"^(?P<name>[^.]+)(\.[^.]+)*\.(so|pyd)$")

CODE_MISSING = "code.missing"
CODE_TOO_LARGE = "code.too_large"
CODE_NO_MODULE = "code.module_missing"


@dataclass(frozen=True)
class Packed:
    content: bytes
    sha256: str
    # Paths inside the archive, in the order they were written.
    files: tuple[str, ...]
    unpacked_bytes: int
    # Links found and left out: a link is never followed.
    skipped_links: tuple[str, ...] = field(default=())

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def _hidden(name: str) -> bool:
    return name.startswith(".")


def _too_large(message: str, **detail: object) -> CheckFailed:
    log.error("the twin's code is refused before upload", reason=message, **detail)
    return CheckFailed(message, code=CODE_TOO_LARGE, detail=dict(detail))


def collect(directory: Path) -> tuple[list[tuple[str, Path]], list[str], int]:
    """(archive name, path) of every file that goes in, sorted; the links left out; and
    the bytes they unpack to. Refuses as soon as a limit of the worker is passed."""
    if not directory.is_dir():
        log.error("the code directory does not exist", directory=str(directory))
        raise CheckFailed(
            f"runtime.code names {directory}, which is not a directory",
            code=CODE_MISSING,
            detail={"directory": str(directory)},
        )
    found: list[tuple[str, Path]] = []
    links: list[str] = []
    total = 0
    for current, dirs, files in os.walk(directory, followlinks=False):
        base = Path(current)
        kept_dirs = []
        for name in sorted(dirs):
            if _hidden(name) or name in _SKIPPED_DIRS:
                continue
            if (base / name).is_symlink():
                links.append((base / name).relative_to(directory).as_posix())
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if _hidden(name) or name.endswith(_SKIPPED_SUFFIXES):
                continue
            path = base / name
            relative = path.relative_to(directory).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                links.append(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            found.append((relative, path))
            total += info.st_size
            if len(found) > MAX_ENTRIES:
                raise _too_large(
                    f"the code directory holds more than {MAX_ENTRIES} files, the most the "
                    "worker unpacks; keep only the twin's code in it",
                    limit="entries",
                    entries=MAX_ENTRIES,
                )
            if total > MAX_UNPACKED_BYTES:
                raise _too_large(
                    f"the code directory holds more than {MAX_UNPACKED_BYTES} bytes, the most "
                    "the worker unpacks; keep data out of it",
                    limit="unpacked_bytes",
                    unpacked_bytes=MAX_UNPACKED_BYTES,
                )
    found.sort(key=lambda item: item[0])
    return found, sorted(links), total


def pack(directory: Path) -> Packed:
    """The archive of a code directory; refused, naming the limit, when it is too large."""
    started = time.perf_counter()
    files, links, total = collect(directory)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, path in files:
            entry = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            entry.create_system = _UNIX
            entry.external_attr = (stat.S_IFREG | FILE_MODE) << 16
            entry.compress_type = zipfile.ZIP_STORED
            archive.writestr(entry, path.read_bytes())
    content = buffer.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise _too_large(
            f"the packed code is {len(content)} bytes; an artifact holds at most "
            f"{MAX_ARCHIVE_BYTES}",
            limit="archive_bytes",
            archive_bytes=MAX_ARCHIVE_BYTES,
        )
    if links:
        log.warn("links in the code directory are left out of the archive", links=links)
    packed = Packed(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        files=tuple(name for name, _ in files),
        unpacked_bytes=total,
        skipped_links=tuple(links),
    )
    log.info(
        "code packed",
        directory=str(directory),
        files=len(packed.files),
        size_bytes=packed.size_bytes,
        sha256=packed.sha256,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return packed


# -- what the manifest names in the code ------------------------------------------------


def code_entries(manifest: Manifest) -> dict[str, str]:
    """Every `module:name` the manifest expects in its code, by where it is named."""
    found: dict[str, str] = {}
    for function_id, function in sorted((manifest.functions or {}).items()):
        if function.kind == "registered" and function.entry:
            found[f"functions.{function_id}.entry"] = function.entry
    for spec_id, spec in sorted((manifest.models or {}).items()):
        for candidate_id, candidate in sorted((spec.candidates or {}).items()):
            family = candidate.family or ""
            if family not in FAMILIES and REGISTERED_ENTRY.match(family):
                found[f"models.{spec_id}.candidates.{candidate_id}.family"] = family
    return found


def _module_files(module: str) -> tuple[str, str]:
    path = module.replace(".", "/")
    return f"{path}.py", f"{path}/__init__.py"


def _has_module(module: str, files: Iterable[str]) -> bool:
    names = set(files)
    plain, package = _module_files(module)
    if plain in names or package in names:
        return True
    folder, _, leaf = module.replace(".", "/").rpartition("/")
    for name in names:
        head, _, tail = name.rpartition("/")
        if head == folder:
            match = _EXTENSION.match(tail)
            if match is not None and match.group("name") == leaf:
                return True
    return False


def missing_modules(manifest: Manifest, files: Iterable[str]) -> list[dict[str, str]]:
    """The entries whose module is not among the packed files: where each is named,
    the entry and the files it was looked for as."""
    names = list(files)
    missing = []
    for where, entry in code_entries(manifest).items():
        module = entry.split(":", 1)[0]
        if not _has_module(module, names):
            plain, package = _module_files(module)
            missing.append(
                {
                    "path": where,
                    "entry": entry,
                    "module": module,
                    "looked_for": f"{plain}, {package}",
                }
            )
    return missing


__all__ = [
    "CODE_MISSING",
    "CODE_NO_MODULE",
    "CODE_TOO_LARGE",
    "MAX_ARCHIVE_BYTES",
    "MAX_ENTRIES",
    "MAX_UNPACKED_BYTES",
    "Packed",
    "code_entries",
    "collect",
    "missing_modules",
    "pack",
]
