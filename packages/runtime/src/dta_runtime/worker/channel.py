"""The two pipes between the listening process and a process that runs code.

The listening process sends jobs as pickles: it is the trusted side. A run process
answers with frames of plain bytes, a JSON header and for a result its body, and
the listening process never unpickles what it reads: the other end may be running
code the twin brought. The standard library only, so that a run process can open
its channel before it imports anything heavy.

A run process holds the pipes on descriptors of its own and points 0 at /dev/null
and 1 at its standard error, so that code reading standard input or printing does
not touch the channel.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any

# Bounds on what the listening process reads from a run process. A result larger
# than this belongs in an artifact.
MAX_HEADER_BYTES = 1024 * 1024
MAX_BODY_BYTES = 256 * 1024 * 1024

READY = "ready"
OK = "ok"
ERROR = "error"


class ChannelBroken(Exception):
    """The run process sent something that is not a frame of this protocol."""


@dataclass(frozen=True)
class Setup:
    """What a run process is started with, before any job."""

    api_url: str
    api_token: str = field(repr=False)
    # Bytes of address space the process may map.
    memory_bytes: int
    # Where code artifacts are unpacked, one directory per content hash.
    code_root: str
    # Directories searched for handler modules after the installed packages.
    handler_paths: tuple[str, ...] = ()
    log_level: str | None = None


@dataclass(frozen=True)
class Job:
    """One run: the validated request and where its handler is."""

    request: Any
    # `module:name` of the handler.
    handler: str
    # On the monotonic clock, which every process of the machine shares.
    deadline: float
    own_code: bool = False
    # Seconds of CPU time for a process of own code.
    cpu_s: int | None = None


@dataclass(frozen=True)
class Reply:
    """What a run process answered."""

    kind: str
    media_type: str | None = None
    body: bytes = b""
    code: str | None = None
    message: str = ""
    frames: list[str] = field(default_factory=list)
    # The process should not run anything more (it hit its memory limit).
    retire: bool = False
    data: dict[str, Any] = field(default_factory=dict)


# -- the run process's end -------------------------------------------------------


def take_channels() -> tuple[Connection, Connection]:
    """The job pipe and the answer pipe on private descriptors; 0 and 1 elsewhere."""
    receiver = Connection(os.dup(0), writable=False)
    sender = Connection(os.dup(1), readable=False)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    return receiver, sender


def send_header(sender: Connection, header: dict[str, Any]) -> None:
    sender.send_bytes(json.dumps(header, default=str, separators=(",", ":")).encode("utf-8"))


def send_ready(sender: Connection, **data: Any) -> None:
    send_header(sender, {"kind": READY, **data})


def send_result(sender: Connection, media_type: str, body: Any) -> None:
    send_header(sender, {"kind": OK, "media_type": media_type})
    sender.send_bytes(body)


def send_error(
    sender: Connection,
    code: str,
    message: str,
    *,
    frames: list[str] | None = None,
    retire: bool = False,
) -> None:
    send_header(
        sender,
        {"kind": ERROR, "code": code, "message": message, "frames": frames or [], "retire": retire},
    )


# -- the listening process's end ---------------------------------------------------


def read_reply(receiver: Connection) -> Reply:
    """One answer, header and body; raises EOFError when the pipe closed before it."""
    try:
        header = json.loads(receiver.recv_bytes(MAX_HEADER_BYTES))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ChannelBroken(f"an unreadable header: {type(exc).__name__}") from exc
    if not isinstance(header, dict) or header.get("kind") not in (READY, OK, ERROR):
        raise ChannelBroken("a header of no known kind")
    kind = header["kind"]
    if kind == READY:
        return Reply(kind=READY, data={k: v for k, v in header.items() if k != "kind"})
    if kind == ERROR:
        frames = header.get("frames")
        return Reply(
            kind=ERROR,
            code=str(header.get("code") or "function.failed"),
            message=str(header.get("message") or ""),
            frames=[str(frame) for frame in frames] if isinstance(frames, list) else [],
            retire=bool(header.get("retire")),
        )
    try:
        body = receiver.recv_bytes(MAX_BODY_BYTES)
    except OSError as exc:
        raise ChannelBroken(f"a result body that cannot be read: {exc}") from exc
    return Reply(kind=OK, media_type=str(header.get("media_type")), body=body)
