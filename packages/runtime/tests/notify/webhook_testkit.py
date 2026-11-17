"""A real webhook for the notification tests: an HTTP server that keeps what it was sent
and answers as the test says; and the ports the tests' own servers listen on."""

from __future__ import annotations

import contextlib
import json
import random
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# The ports this package's tests may listen on.
PORTS = range(18960, 18980)


def own_port(taken: set[int] | None = None) -> int:
    """A port of the range that nothing listens on now."""
    candidates = list(PORTS)
    random.shuffle(candidates)
    for port in candidates:
        if taken and port in taken:
            continue
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {PORTS.start}..{PORTS.stop - 1}")


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def event(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class Receiver:
    """What the webhook was sent, and how it answers: `statuses` are the answers of the
    next requests, in order; 204 once they are used up."""

    port: int
    received: list[Received] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)
    answered: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hooks/twin"

    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [found.event for found in self.received]

    def wait_for(self, count: int, timeout_s: float = 30.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_s
        pause = threading.Event()
        while True:
            found = self.events()
            if len(found) >= count:
                return found
            assert time.monotonic() < deadline, f"{len(found)} of {count} events arrived"
            pause.wait(0.02)


def _handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            with receiver.lock:
                status = receiver.statuses.pop(0) if receiver.statuses else 204
                receiver.answered.append(status)
                if 200 <= status < 300:
                    receiver.received.append(
                        Received(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                    )
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return None

    return Handler


@contextlib.contextmanager
def webhook(port: int | None = None, statuses: list[int] | None = None) -> Iterator[Receiver]:
    """A webhook listening on a port of the range until the block ends."""
    receiver = Receiver(port=port or own_port(), statuses=list(statuses or []))
    server = ThreadingHTTPServer(("127.0.0.1", receiver.port), _handler(receiver))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="webhook", daemon=True)
    thread.start()
    try:
        yield receiver
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)
