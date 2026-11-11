"""How long the stack's services keep an idle HTTP connection.

A server closes a connection that has been idle for its keep-alive time. A
client that reuses the connection at that moment has its request reset: it
fails with a read error although the server is healthy. Seen with a client
that polls every 5 s against a server whose keep-alive is 5 s, both the
libraries' defaults: about one request in forty was lost.

It cannot happen when the client gives an idle connection up before the
server would. The stack's servers keep one for `SERVER_KEEP_ALIVE_S`, its
clients for `CLIENT_KEEP_ALIVE_S`, and the first is longer than the second
and than httpx's own default, so a client made without these limits is safe
as well.
"""

from __future__ import annotations

import httpx

# For uvicorn's `timeout_keep_alive`, which takes whole seconds.
SERVER_KEEP_ALIVE_S = 75
CLIENT_KEEP_ALIVE_S = 30.0


def client_limits() -> httpx.Limits:
    """Limits of an httpx client that talks to a service of the stack."""
    return httpx.Limits(
        max_connections=100, max_keepalive_connections=20, keepalive_expiry=CLIENT_KEEP_ALIVE_S
    )


__all__ = ["CLIENT_KEEP_ALIVE_S", "SERVER_KEEP_ALIVE_S", "client_limits"]
