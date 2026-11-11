"""Reading directory trees under `tasks/` without ever following a link.

Task directories are written by other agents, so nothing in them is trusted:
a name may be a link to somewhere else, a pipe that blocks a reader, or a
second name for a file outside the directory. Every walk here looks at each
entry itself (never at what it points to) and reports what it found, so the
callers can refuse anything that is not a plain file or directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dta_core.log import Log

log = Log("tasks.tree")


@dataclass
class Scan:
    """What one directory tree contains. Paths are relative, with `/` separators."""

    exists: bool = True  # the root is a real directory, not a link and not missing
    files: dict[str, str] = field(default_factory=dict)  # path -> sha256 of content
    dirs: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # path -> where it points
    special: list[str] = field(default_factory=list)  # pipes, sockets, devices, unreadable
    shared: list[str] = field(default_factory=list)  # files that have another name elsewhere

    @property
    def empty(self) -> bool:
        return not (self.files or self.dirs or self.links or self.special)

    @property
    def digest(self) -> str:
        """Identifies the tree: every name, what kind of thing it is, and file contents."""
        if not self.exists:
            return "absent"
        listing = (
            [[path, "file", digest] for path, digest in self.files.items()]
            + [[path, "dir", ""] for path in self.dirs]
            + [[path, "link", target] for path, target in self.links.items()]
            + [[path, "special", ""] for path in self.special]
        )
        canonical = json.dumps(sorted(listing), separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def spell_name(name: str) -> str:
    """A file name as it is recorded and reported.

    A name is bytes, and not every name is text. One that is not is spelled out
    in hex, so that records hold exactly what is on disk and can always be
    written. No real name contains a slash, so the two cannot be confused.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "hex/" + os.fsencode(name).hex()
    return name


def is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _open_plain(path: Path) -> int | None:
    """Open a regular file for reading, or return None if the name is anything else.

    Never follows a link and never blocks on a pipe.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def read_plain_file(path: Path, limit: int | None = None) -> bytes | None:
    """Content of a regular file, or None if the name is not one or cannot be read.

    With `limit`, reading stops just past it: a result longer than the limit
    tells the caller the file is too large without holding all of it.
    """
    fd = _open_plain(path)
    if fd is None:
        return None
    try:
        chunks = []
        held = 0
        while block := os.read(fd, 1 << 20):
            chunks.append(block)
            held += len(block)
            if limit is not None and held > limit:
                break
        data = b"".join(chunks)
        return data if limit is None else data[: limit + 1]
    except OSError:
        return None
    finally:
        os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_plain_file(path: Path) -> str | None:
    fd = _open_plain(path)
    if fd is None:
        return None
    try:
        digest = hashlib.sha256()
        while block := os.read(fd, 1 << 20):
            digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def scan(root: Path) -> Scan:
    """Walk a directory tree and describe it. A root that is not a real directory is absent."""
    if not is_real_dir(root):
        return Scan(exists=False)
    found = Scan()
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            log.warn("directory cannot be listed", problem=str(exc), path=str(directory))
            found.special.append(prefix.rstrip("/") or ".")
            continue
        for entry in entries:
            relative = f"{prefix}{spell_name(entry.name)}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                found.special.append(relative)
                continue
            if relative != f"{prefix}{entry.name}":
                # Still walked and hashed like any other entry, so the digest
                # covers it, but nothing with such a name is ever accepted.
                found.special.append(relative)
            if stat.S_ISLNK(info.st_mode):
                try:
                    found.links[relative] = os.readlink(entry.path)
                except OSError:
                    found.links[relative] = ""
            elif stat.S_ISDIR(info.st_mode):
                found.dirs.append(relative)
                pending.append((Path(entry.path), f"{relative}/"))
            elif stat.S_ISREG(info.st_mode):
                digest = _hash_plain_file(Path(entry.path))
                if digest is None:
                    found.special.append(relative)
                    continue
                found.files[relative] = digest
                if info.st_nlink > 1:
                    found.shared.append(relative)
            else:
                found.special.append(relative)
    found.dirs.sort()
    found.special = sorted(set(found.special))
    found.shared.sort()
    found.files = dict(sorted(found.files.items()))
    found.links = dict(sorted(found.links.items()))
    return found


def entry_digest(path: Path) -> str:
    """Identifies whatever is at `path`: a tree, a file, a link, or something else."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return "absent"
    if stat.S_ISDIR(mode):
        return f"dir:{scan(path).digest}"
    if stat.S_ISLNK(mode):
        return f"link:{os.readlink(path)}"
    if stat.S_ISREG(mode):
        return f"file:{_hash_plain_file(path)}"
    return "special"


def _make_removable(root: Path) -> None:
    """Give the owner full access to every directory of a tree.

    A directory left read-only (some build tools do this to their caches) would
    otherwise keep its contents from being removed.
    """
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            os.chmod(directory, stat.S_IRWXU)
            with os.scandir(directory) as entries:
                pending.extend(
                    Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)
                )
        except OSError as exc:
            log.warn("directory cannot be made removable", problem=str(exc), path=str(directory))


def remove_entry(path: Path) -> None:
    """Delete whatever is at `path` itself. A link is removed, never followed."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        _make_removable(path)
        shutil.rmtree(path)
    else:
        path.unlink()
