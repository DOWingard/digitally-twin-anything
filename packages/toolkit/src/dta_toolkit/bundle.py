"""A twin in one file: the bundle `dta export` writes and `dta import` reads.

A bundle is an uncompressed tar archive. Its first member, `bundle.json`,
describes the twin at the version the bundle holds and records the SHA-256 and
the size of every other member, so that a damaged or altered bundle is refused
before anything of it is unpacked. Every other member lies under one of three
folders:

    workspace/   the files the twin is made from and its record of what was agreed
    store/       the backup of the twin's database, as Dolt wrote it
    artifacts/   the files of the twin's artifact volume

Nothing else may be in it: no link, no device, no absolute or upward path, no
name twice. The archive is written under a temporary name beside its target
and renamed when it is complete, so a bundle that exists is a whole one.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal

from dta_core.errors import DtaError
from dta_core.log import Log
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = Log("bundle")

KIND = "dta-twin-bundle"
FORMAT = 1
KNOWN_FORMATS = (FORMAT,)
SUFFIX = ".dtabundle"
DESCRIPTOR = "bundle.json"
WORKSPACE = "workspace"
STORE = "store"
ARTIFACTS = "artifacts"
SECTIONS = (WORKSPACE, STORE, ARTIFACTS)
_BLOCK = 1 << 20
_PARTIAL = ".partial"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A bundle describes itself in a few kilobytes; anything larger is not one.
_DESCRIPTOR_MAX = 16 * 1024 * 1024

NOT_FOUND = "bundle.not_found"
UNREADABLE = "bundle.unreadable"
UNKNOWN_FORMAT = "bundle.unknown_format"
CHECKSUM_MISMATCH = "bundle.checksum_mismatch"
UNSAFE_MEMBER = "bundle.unsafe_member"


class BundleError(DtaError):
    """The file is not a bundle this toolkit can read, or it is not whole."""

    code = UNREADABLE


# -- what bundle.json says -------------------------------------------------------------


class _Part(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TwinRef(_Part):
    id: str
    name: str | None = None
    tier: str | None = None


class Head(_Part):
    """A version of a branch: its commit, its number on the branch and its time."""

    branch: str
    id: str
    seq: int
    t: datetime


class Member(_Part):
    sha256: str = Field(pattern=_SHA256.pattern)
    size: int = Field(ge=0)


class Note(_Part):
    """Something the reader of the bundle has to know, with a code to branch on."""

    code: str
    message: str
    source: str | None = None


class Versions(_Part):
    store: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    toolkit: str | None = None


class Descriptor(_Part):
    kind: Literal["dta-twin-bundle"] = KIND
    format: Literal[1] = FORMAT
    twin: TwinRef
    manifest_hash: str
    contract_hash: str | None = None
    # The database the backup is of; import restores it under this name.
    database: str
    head: Head
    # Every branch of the database with the commit it is at, as the backup holds them.
    branches: dict[str, str]
    versions: Versions = Field(default_factory=Versions)
    # Wall-clock time of the export: when the file was made, not a time of the twin.
    exported_at: datetime
    # How the heads were read: the backups made until no branch moved during one.
    backup: dict[str, Any] = Field(default_factory=dict)
    members: dict[str, Member]
    directories: list[str]
    warnings: list[Note] = Field(default_factory=list)

    def to_bytes(self) -> bytes:
        return (self.model_dump_json(indent=2) + "\n").encode("utf-8")


def bundle_name(twin_id: str, head: Head) -> str:
    """`twin-<id>-<seq>-<first 8 characters of the head>.dtabundle`."""
    return f"twin-{twin_id}-{head.seq}-{head.id[:8]}{SUFFIX}"


# -- writing ----------------------------------------------------------------------------


def sha256_of(handle: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: handle.read(_BLOCK), b""):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def scan(staging: Path) -> tuple[dict[str, Member], list[str]]:
    """Checksums of every file under the sections of a staging folder, and its folders.

    A section that is not there is made empty, so every bundle has all three.
    """
    members: dict[str, Member] = {}
    directories: list[str] = []
    for section in SECTIONS:
        root = staging / section
        root.mkdir(parents=True, exist_ok=True)
        for path in [root, *sorted(root.rglob("*"))]:
            name = path.relative_to(staging).as_posix()
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                log.error("the staging folder holds what a bundle may not", member=name)
                raise BundleError(
                    f"{name} is neither a regular file nor a folder; a bundle holds no other",
                    code=UNSAFE_MEMBER,
                    detail={"member": name},
                )
            if path.is_dir():
                directories.append(name)
                continue
            with path.open("rb") as handle:
                sha256, size = sha256_of(handle)
            members[name] = Member(sha256=sha256, size=size)
    log.debug("staging folder scanned", files=len(members), directories=len(directories))
    return members, directories


def _normalised(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Who owned a file here means nothing on the next machine; whether it runs does."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.isdir():
        info.mode = 0o755
    else:
        info.mode = 0o755 if info.mode & 0o100 else 0o644
    return info


def _order(descriptor: Descriptor) -> list[str]:
    """Folders before what they hold, sections in their order, names sorted within."""
    names = [*descriptor.directories, *descriptor.members]
    rank = {section: index for index, section in enumerate(SECTIONS)}
    return sorted(names, key=lambda name: (rank[name.split("/", 1)[0]], name))


def write(staging: Path, descriptor: Descriptor, target: Path) -> int:
    """Write the bundle of a staging folder to `target`; its size in bytes.

    The archive is written under a temporary name in the target's folder and
    renamed over the target only once it is complete and on disk; whatever goes
    wrong before, no file is left under either name.
    """
    started = time.perf_counter()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=_PARTIAL)
    try:
        with os.fdopen(fd, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as archive:
                data = descriptor.to_bytes()
                info = tarfile.TarInfo(DESCRIPTOR)
                info.size = len(data)
                info.mtime = int(descriptor.exported_at.timestamp())
                archive.addfile(_normalised(info), io.BytesIO(data))
                for name in _order(descriptor):
                    archive.add(staging / name, arcname=name, recursive=False, filter=_normalised)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException as exc:
        log.error("the bundle could not be written; nothing is left", exc=exc, target=str(target))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    size = target.stat().st_size
    log.info(
        "bundle written",
        target=str(target),
        size_bytes=size,
        files=len(descriptor.members),
        duration_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return size


# -- reading ----------------------------------------------------------------------------


def _check_name(name: str) -> None:
    """Refuse a member name that could land outside the folder it is unpacked into."""
    path = PurePosixPath(name)
    parts = path.parts
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
        or path.as_posix() != name
        or (name != DESCRIPTOR and parts[0] not in SECTIONS)
    ):
        log.error("the bundle holds a member with a name it may not have", member=name)
        raise BundleError(
            f"the bundle holds a member named {name!r}, which a bundle may not; it was not "
            "written by dta export or it was altered",
            code=UNSAFE_MEMBER,
            detail={"member": name},
        )


def _mismatch(name: str, reason: str) -> BundleError:
    log.error("the bundle does not match its own description", member=name, reason=reason)
    return BundleError(
        f"member {name} of the bundle {reason}; the bundle is damaged or was altered: "
        "export it again",
        code=CHECKSUM_MISMATCH,
        detail={"member": name, "reason": reason},
    )


@dataclass(frozen=True)
class Verified:
    files: int
    bytes: int
    duration_ms: float


class Reader:
    """An open bundle: its description at once, its members once verified."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._archive = tarfile.open(path, mode="r:")
        except FileNotFoundError as exc:
            log.error("no bundle at this path", path=str(path))
            raise BundleError(
                f"no bundle at {path}", code=NOT_FOUND, detail={"path": str(path)}
            ) from exc
        except (tarfile.TarError, OSError) as exc:
            log.error("the file is not a tar archive", exc=exc, path=str(path))
            raise BundleError(
                f"{path} is not a bundle: it cannot be read as a tar archive",
                code=UNREADABLE,
                detail={"path": str(path), "error": type(exc).__name__},
            ) from exc
        try:
            self.raw = self._read_descriptor()
            self.descriptor = self._parse(self.raw)
        except BaseException:
            self._archive.close()
            raise
        self.digest = hashlib.sha256(self.raw).hexdigest()

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    def _read_descriptor(self) -> bytes:
        try:
            info = self._archive.getmember(DESCRIPTOR)
        except KeyError as exc:
            log.error("the archive holds no bundle.json", path=str(self.path))
            raise BundleError(
                f"{self.path} is not a bundle: it holds no {DESCRIPTOR}",
                code=UNREADABLE,
                detail={"path": str(self.path)},
            ) from exc
        if not info.isfile() or info.size > _DESCRIPTOR_MAX:
            raise BundleError(
                f"{DESCRIPTOR} of {self.path} is not a file of a bundle's description",
                code=UNREADABLE,
                detail={"path": str(self.path), "size": info.size},
            )
        handle = self._archive.extractfile(info)
        assert handle is not None
        return handle.read()

    def _parse(self, raw: bytes) -> Descriptor:
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.error("bundle.json is not JSON", exc=exc, path=str(self.path))
            raise BundleError(
                f"{DESCRIPTOR} of {self.path} is not JSON", detail={"path": str(self.path)}
            ) from exc
        kind = document.get("kind") if isinstance(document, dict) else None
        if kind != KIND:
            log.error("bundle.json does not describe a twin bundle", path=str(self.path))
            raise BundleError(
                f"{self.path} is not a twin bundle: its {DESCRIPTOR} names another kind",
                detail={"path": str(self.path), "kind": kind},
            )
        found = document.get("format")
        if found not in KNOWN_FORMATS:
            log.error("bundle format unknown to this toolkit", found=found, known=KNOWN_FORMATS)
            raise BundleError(
                f"the bundle is of format {found!r}, which this toolkit does not know (it reads "
                f"{', '.join(str(known) for known in KNOWN_FORMATS)}); import it with the "
                "toolkit version that exported it, or a newer one",
                code=UNKNOWN_FORMAT,
                detail={"format": found, "known": list(KNOWN_FORMATS)},
            )
        try:
            return Descriptor.model_validate(document)
        except ValidationError as exc:
            problems = [
                {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}
                for error in exc.errors(include_url=False, include_input=False)
            ]
            log.error("bundle.json does not have the shape of its format", problems=problems)
            raise BundleError(
                f"{DESCRIPTOR} of {self.path} does not have the shape of format {found}",
                detail={"path": str(self.path), "problems": problems},
            ) from exc

    def verify(self) -> Verified:
        """Check every member against `bundle.json`: names, kinds, sizes and checksums.

        Raises naming the first member that differs; nothing is unpacked before.
        """
        started = time.perf_counter()
        expected = self.descriptor.members
        directories = set(self.descriptor.directories)
        seen: set[str] = set()
        seen_directories: set[str] = set()
        total = 0
        for info in self._archive:
            name = info.name
            _check_name(name)
            if info.isdir() and name != DESCRIPTOR:
                if name not in directories:
                    raise _mismatch(name, "is a folder bundle.json does not list")
                seen_directories.add(name)
                continue
            if not info.isfile():
                log.error("the bundle holds a link or a device", member=name)
                raise BundleError(
                    f"member {name} of the bundle is a link or a device; a bundle holds "
                    "regular files and folders only",
                    code=UNSAFE_MEMBER,
                    detail={"member": name},
                )
            if name in seen:
                log.error("the bundle holds a member twice", member=name)
                raise BundleError(
                    f"the bundle holds {name} twice", code=UNSAFE_MEMBER, detail={"member": name}
                )
            seen.add(name)
            if name == DESCRIPTOR:
                continue
            listed = expected.get(name)
            if listed is None:
                raise _mismatch(name, "is not listed in bundle.json")
            if info.size != listed.size:
                raise _mismatch(name, f"has {info.size} bytes, bundle.json records {listed.size}")
            handle = self._archive.extractfile(info)
            assert handle is not None
            sha256, size = sha256_of(handle)
            if sha256 != listed.sha256 or size != listed.size:
                raise _mismatch(name, "does not match its checksum in bundle.json")
            total += size
        for name in sorted(set(expected) - seen):
            raise _mismatch(name, "is listed in bundle.json but is not in the bundle")
        for name in sorted(directories - seen_directories):
            raise _mismatch(name, "is a folder listed in bundle.json but not in the bundle")
        elapsed = round((time.perf_counter() - started) * 1000.0, 1)
        log.info("bundle verified", files=len(expected), bytes=total, duration_ms=elapsed)
        return Verified(files=len(expected), bytes=total, duration_ms=elapsed)

    def files(self, section: str) -> list[str]:
        """Names of the files of one section, in the bundle's order."""
        prefix = f"{section}/"
        return [name for name in self.descriptor.members if name.startswith(prefix)]

    def read(self, name: str) -> bytes:
        """The content of one listed file; the bundle must have been verified."""
        if name not in self.descriptor.members:
            raise BundleError(f"the bundle holds no file {name}", detail={"member": name})
        handle = self._archive.extractfile(name)
        assert handle is not None
        return handle.read()

    def mode(self, name: str) -> int:
        return self._archive.getmember(name).mode

    def iter_files(self, section: str) -> Iterator[tuple[str, bytes, int]]:
        """(name below the section, content, mode) of each file of a section."""
        prefix = f"{section}/"
        for name in self.files(section):
            yield name[len(prefix) :], self.read(name), self.mode(name)

    def extract(self, section: str, destination: Path) -> Path:
        """Unpack one section below `destination`; the folder it is in.

        The archive's own safety filter for data is applied as well, so even a
        member that got past verification cannot land outside `destination`.
        """
        prefix = f"{section}/"
        chosen = [
            info
            for info in self._archive.getmembers()
            if info.name == section or info.name.startswith(prefix)
        ]
        self._archive.extractall(destination, members=chosen, filter="data")
        log.debug("bundle section unpacked", section=section, members=len(chosen))
        return destination / section
